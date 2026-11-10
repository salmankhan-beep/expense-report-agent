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

"""Unit tests for tracing and trace-correlated logging (no cloud calls)."""

import asyncio
import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import observability as obs
from app.agent import auto_approve, route_by_amount
from app.tools import format_record
from tests.unit.test_expense_agent import FakeContext, expense


@pytest.fixture
def spans(monkeypatch):
    """Capture spans from app.observability in memory."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(obs, "_tracer", lambda: provider.get_tracer("test"))
    return exporter


def by_name(exporter, name):
    return [s for s in exporter.get_finished_spans() if s.name == name]


def test_route_span_explains_the_decision(spans) -> None:
    previous = format_record(expense(), "approved")
    asyncio.run(route_by_amount(expense(), FakeContext([previous])))

    (route,) = by_name(spans, "expense.route")
    attrs = dict(route.attributes)
    assert attrs["expense.route"] == "DEEP_REVIEW"
    assert attrs["expense.complexity"] == "duplicate"
    assert attrs["expense.reasons"] == "duplicate"
    assert attrs["expense.duplicate"] is True
    assert attrs["expense.history_records"] == 1
    assert attrs["expense.policy_limit_usd"] == 75.0

    (recall,) = by_name(spans, "memory.recall")
    assert recall.parent.span_id == route.context.span_id  # nested under routing
    assert dict(recall.attributes)["expense.records"] == 1


def test_spans_never_contain_email_addresses(spans) -> None:
    asyncio.run(route_by_amount(expense(amount=250.0), FakeContext()))
    asyncio.run(auto_approve(expense(), FakeContext()))
    for finished in spans.get_finished_spans():
        for value in finished.attributes.values():
            assert "@" not in str(value), (finished.name, value)


def test_decision_span_and_memory_write(spans) -> None:
    asyncio.run(auto_approve(expense(), FakeContext()))
    (decision,) = by_name(spans, "expense.decision")
    assert dict(decision.attributes)["expense.decided_by"] == "auto"
    (write,) = by_name(spans, "memory.write")
    assert dict(write.attributes)["expense.written"] is True
    assert write.parent.span_id == decision.context.span_id


def test_logs_carry_trace_and_span_ids(spans, capsys, monkeypatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-project")
    asyncio.run(route_by_amount(expense(amount=250.0), FakeContext()))

    (route,) = by_name(spans, "expense.route")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    (entry,) = [e for e in lines if e.get("event") == "expense_routed"]
    trace_id = format(route.context.trace_id, "032x")
    assert entry["logging.googleapis.com/trace"] == (
        f"projects/demo-project/traces/{trace_id}"
    )
    assert entry["logging.googleapis.com/spanId"] == format(
        route.context.span_id, "016x"
    )
    assert entry["route"] == "DEEP_REVIEW"  # $250 is > 2x the $75 meals limit
    assert entry["review_model"]
    assert entry["reasons"] == ["threshold", "policy_limit"]


def test_log_without_active_span_has_no_trace_fields(capsys) -> None:
    obs.log_event("ping", "hello")
    entry = json.loads(capsys.readouterr().out)
    assert entry["event"] == "ping"
    assert "logging.googleapis.com/trace" not in entry


def test_submitter_id_is_stable_and_case_insensitive() -> None:
    assert obs.submitter_id("Bob@Company.com") == obs.submitter_id("bob@company.com")
    assert "@" not in obs.submitter_id("bob@company.com")
