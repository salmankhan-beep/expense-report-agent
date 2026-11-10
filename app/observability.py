# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tracing, trace-correlated logs, and metrics for the expense workflow.

ADK already traces each workflow run, node, LLM call, and tool call. This
module adds the business view on top:

- **Spans** for routing, memory recall/write, and decisions, nested under ADK's
  spans so one trace shows *why* a claim went where it did.
- **Structured JSON logs** carrying the active trace and span IDs, so Cloud
  Logging links each log line to its trace.
- **Metrics** (OpenTelemetry counters/histograms) for decisions and routing.
  They are no-ops unless a meter provider is configured.

PII never leaves in telemetry: spans and logs carry a keyed pseudonymous
submitter ID instead of the email, and every log line is scrubbed by
``app.pii`` (regex, or Cloud DLP when enabled) before it is written.
"""

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.trace import Span, Status, StatusCode

from app import pii

_INSTRUMENTATION = "expense_agent"


def _tracer() -> trace.Tracer:
    # Resolved per call so a provider installed after import (by ADK's server
    # setup, or by tests) is picked up.
    return trace.get_tracer(_INSTRUMENTATION)


_meter = metrics.get_meter(_INSTRUMENTATION)
decisions_counter = _meter.create_counter(
    "expense.decisions",
    description="Expense decisions, by status and decided_by.",
)
routing_counter = _meter.create_counter(
    "expense.routing",
    description="Routing outcomes, by route and reason.",
)
amount_histogram = _meter.create_histogram(
    "expense.amount",
    unit="USD",
    description="Claimed amounts, by route.",
)


def submitter_id(submitter: str) -> str:
    """Stable, keyed, non-reversible ID for a submitter (spans, logs, memory)."""
    return pii.pseudonymize(submitter)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    """Start a child span with ``expense.*`` attributes; record errors on it."""
    with _tracer().start_as_current_span(name) as current:
        set_attributes(current, **attributes)
        try:
            yield current
        except Exception as exc:
            current.record_exception(exc)
            current.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def set_attributes(target: Span, **attributes: Any) -> None:
    for key, value in attributes.items():
        if value is None:
            continue
        if not isinstance(value, (str, bool, int, float)):
            value = str(value)
        target.set_attribute(f"expense.{key}", value)


def _trace_fields() -> dict[str, Any]:
    """Cloud Logging fields that link a log entry to the current span."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    trace_id = format(context.trace_id, "032x")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    return {
        "logging.googleapis.com/trace": (
            f"projects/{project}/traces/{trace_id}" if project else trace_id
        ),
        "logging.googleapis.com/spanId": format(context.span_id, "016x"),
        "logging.googleapis.com/trace_sampled": context.trace_flags.sampled,
    }


def log_event(event: str, message: str, severity: str = "INFO", **fields: Any) -> None:
    """Write one structured JSON log line to stdout, correlated with the trace.

    Agent Runtime and Cloud Run turn JSON on stdout into structured Cloud
    Logging entries, so ``event`` and the other fields are filterable and can
    drive log-based metrics and alerts.

    PII is removed before writing: a ``submitter`` field is replaced by its
    pseudonymous ``submitter_id``, and every string (including ``message``) is
    scrubbed.
    """
    if "submitter" in fields:
        fields["submitter_id"] = submitter_id(str(fields.pop("submitter") or ""))
    entry = {
        "severity": severity,
        "message": pii.scrub_text(message),
        "event": event,
        **pii.scrub_value(fields),
        **_trace_fields(),
    }
    print(json.dumps(entry, default=str), file=sys.stdout, flush=True)
