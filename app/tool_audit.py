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

"""Audit trail of what the reviewer *intended* to do with each tool versus
what actually happened.

Attached to both reviewer agents as ADK tool callbacks:

- ``before_tool_callback`` logs ``tool_intent``: which agent and model chose
  which tool, with which (PII-scrubbed) arguments.
- ``after_tool_callback`` logs ``tool_outcome``: the result status, error and
  recovery instruction if any, key result facts, latency, and whether the
  intent was achieved.
- ``on_tool_error_callback`` handles unexpected exceptions: it logs a
  ``tool_outcome`` with status ``exception`` and returns an error result with
  a recovery instruction, so the model can continue instead of the run failing.

Intent and outcome share the function-call ID, so the pair can be joined in
Cloud Logging or BigQuery. Outcomes also become span attributes on ADK's
``execute_tool`` span and OpenTelemetry metrics.
"""

import time
from typing import Any

from google.adk.agents.context import Context
from google.adk.tools.base_tool import BaseTool
from pydantic import BaseModel

from app import observability as obs

# Result fields worth recording per tool (never raw PII).
_OUTCOME_FACTS = {
    "lookup_expense_policy": ("category", "matched_exactly", "per_claim_limit_usd"),
    "get_spending_history": ("claims_recalled", "approved_total_usd", "rejected_count"),
    "check_duplicate_claims": ("is_duplicate",),
    "emit_expense_alert": ("alert_emitted",),
}

_meter = obs.metrics.get_meter("expense_agent")
tool_outcomes_counter = _meter.create_counter(
    "expense.tool_outcomes",
    description="Reviewer tool calls, by tool, agent, and outcome status.",
)
tool_latency_histogram = _meter.create_histogram(
    "expense.tool_latency",
    unit="ms",
    description="Reviewer tool latency, by tool.",
)

_started: dict[str, float] = {}


def _call_id(tool_context: Context) -> str:
    return tool_context.function_call_id or "unknown"


def _safe_args(args: dict[str, Any]) -> dict[str, Any]:
    """Arguments as logged: submitter pseudonymized, everything else scrubbed."""
    safe = dict(args)
    if "submitter" in safe:
        safe["submitter_id"] = obs.submitter_id(str(safe.pop("submitter") or ""))
    return safe


def _as_dict(response: Any) -> dict[str, Any]:
    """Tool results arrive as Pydantic models, dicts, or ``{"result": model}``."""
    if isinstance(response, dict) and set(response) == {"result"}:
        response = response["result"]
    if isinstance(response, BaseModel):
        return response.model_dump(mode="json")
    return response if isinstance(response, dict) else {"result": response}


def _context_fields(tool: BaseTool, tool_context: Context) -> dict[str, Any]:
    return {
        "tool": tool.name,
        "call_id": _call_id(tool_context),
        "agent": tool_context.agent_name,
        "review_model": tool_context.state.get("review_model"),
    }


def before_tool_callback(
    tool: BaseTool, args: dict[str, Any], tool_context: Context
) -> None:
    """Log the model's intended tool action before it runs."""
    _started[_call_id(tool_context)] = time.perf_counter()
    obs.log_event(
        "tool_intent",
        f"{tool_context.agent_name} intends to call {tool.name}",
        **_context_fields(tool, tool_context),
        args=_safe_args(args),
    )
    return None  # never short-circuit the tool


def _record_outcome(
    tool: BaseTool,
    tool_context: Context,
    status: str,
    *,
    error: str | None = None,
    recovery: str | None = None,
    facts: dict[str, Any] | None = None,
) -> None:
    started = _started.pop(_call_id(tool_context), None)
    latency_ms = round((time.perf_counter() - started) * 1000, 1) if started else None
    achieved = status == "ok"
    obs.set_attributes(
        obs.trace.get_current_span(),
        tool_status=status,
        tool_intent_achieved=achieved,
        tool_latency_ms=latency_ms,
    )
    labels = {"tool": tool.name, "agent": tool_context.agent_name, "status": status}
    tool_outcomes_counter.add(1, labels)
    if latency_ms is not None:
        tool_latency_histogram.record(latency_ms, {"tool": tool.name})
    obs.log_event(
        "tool_outcome",
        f"{tool.name} called by {tool_context.agent_name}: {status}",
        severity="INFO" if achieved else "WARNING",
        **_context_fields(tool, tool_context),
        status=status,
        intent_achieved=achieved,
        latency_ms=latency_ms,
        error=error,
        recovery=recovery,
        result=facts or {},
    )


def after_tool_callback(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: Context,
    tool_response: Any,
) -> None:
    """Log the actual outcome of the tool call; never alter the result."""
    result = _as_dict(tool_response)
    status = str(result.get("status") or "ok")
    facts = {k: result[k] for k in _OUTCOME_FACTS.get(tool.name, ()) if k in result}
    _record_outcome(
        tool,
        tool_context,
        status,
        error=result.get("error"),
        recovery=result.get("recovery"),
        facts=facts,
    )
    return None


def on_tool_error_callback(
    tool: BaseTool, args: dict[str, Any], tool_context: Context, error: Exception
) -> dict[str, Any]:
    """Turn an unexpected tool exception into a logged error with recovery steps."""
    recovery = (
        f"Do not retry {tool.name}. Continue the review without its result, "
        "state in the review that this check could not be completed, and "
        "recommend request-more-info."
    )
    _record_outcome(
        tool,
        tool_context,
        "exception",
        error=f"{type(error).__name__}: {error}",
        recovery=recovery,
    )
    return {
        "status": "error",
        "error": f"{tool.name} failed unexpectedly: {type(error).__name__}",
        "recovery": recovery,
    }
