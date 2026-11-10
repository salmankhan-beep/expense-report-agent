# ruff: noqa
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

import base64
import json
import re
from typing import Any

from google.adk import Agent, Context, Event, Workflow
from google.adk.apps import App
from google.adk.events import RequestInput
from google.adk.models import Gemini
from google.genai import types
from pydantic import BaseModel, Field

from app.config import config


MODEL = "gemini-3.8-flash"


# ---------------------------------------------------------------------------
# Pydantic schemas for structured data flow between nodes
# ---------------------------------------------------------------------------


class ExpenseData(BaseModel):
    """Expense report data extracted from the incoming event."""

    amount: float = Field(description="Expense amount in USD")
    submitter: str = Field(description="Email of the person who submitted")
    category: str = Field(description="Expense category, e.g. travel, meals")
    description: str = Field(description="What the expense is for")
    date: str = Field(description="Date of the expense (YYYY-MM-DD)")


# ---------------------------------------------------------------------------
# Function nodes
# ---------------------------------------------------------------------------


def _extract_text_from_input(node_input: Any) -> str:
    """Normalize ADK START input (str, dict, or types.Content) into a string."""
    if isinstance(node_input, str):
        return node_input
    if isinstance(node_input, dict):
        return json.dumps(node_input)
    parts = getattr(node_input, "parts", None)
    if parts:
        texts = [p.text for p in parts if getattr(p, "text", None)]
        if texts:
            return "\n".join(texts)
    return str(node_input)


def _parse_free_text_expense(raw_text: str) -> dict[str, Any]:
    """Extract expense fields from a plain-text prompt when JSON is not provided."""
    amount_match = re.search(
        r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)|(?:amount|usd|for)\s*[:=]?\s*([0-9]+(?:\.[0-9]{1,2})?)",
        raw_text,
        re.IGNORECASE,
    )
    amount = 0.0
    if amount_match:
        matched_val = amount_match.group(1) or amount_match.group(2)
        amount = float(matched_val)

    email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", raw_text)
    submitter = email_match.group(0) if email_match else "employee@company.com"

    date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", raw_text)
    date_str = date_match.group(1) if date_match else "2026-11-10"

    lowered = raw_text.lower()
    category = "other"
    for candidate in (
        "travel",
        "flight",
        "hotel",
        "meals",
        "lunch",
        "dinner",
        "equipment",
        "software",
        "supplies",
    ):
        if candidate in lowered:
            category = (
                "meals"
                if candidate in ("lunch", "dinner")
                else ("travel" if candidate in ("flight", "hotel") else candidate)
            )
            break

    return {
        "amount": amount,
        "submitter": submitter,
        "category": category,
        "description": raw_text.strip(),
        "date": date_str,
    }


def parse_expense_email(node_input: Any) -> Event:
    """Parse a Pub/Sub trigger event, plain JSON, or text prompt into expense data.

    The trigger endpoint delivers the raw Pub/Sub message JSON. The
    expense payload lives in the ``data`` field, which may be
    base64-encoded (real Pub/Sub) or plain JSON (local testing). Also
    supports direct JSON payloads and natural-language expense strings.
    """
    raw_text = _extract_text_from_input(node_input).strip()

    try:
        event = json.loads(raw_text)
    except json.JSONDecodeError:
        if "$" in raw_text or re.search(
            r"\b(?:expense|claim|approve|reimburse)\b", raw_text, re.IGNORECASE
        ):
            parsed = _parse_free_text_expense(raw_text)
            return Event(output=parsed)
        return Event(output=_parse_free_text_expense(raw_text))

    if not isinstance(event, dict):
        return Event(output={"error": f"Invalid payload type: {raw_text[:200]}"})

    if "data" in event:
        data = event.get("data", {})
        if isinstance(data, str):
            try:
                data = json.loads(base64.b64decode(data))
            except Exception:
                try:
                    data = json.loads(data)
                except Exception:
                    return Event(
                        output={"error": f"Failed to decode data: {data[:200]}"}
                    )
    else:
        data = event

    return Event(
        output={
            "amount": float(data.get("amount", 0)),
            "submitter": data.get("submitter") or data.get("employee", "unknown"),
            "category": data.get("category", "other"),
            "description": data.get("description", ""),
            "date": data.get("date", ""),
        }
    )


def route_by_amount(node_input: dict[str, Any], ctx: Context) -> Event:
    """Route expenses based on the $100 threshold.

    Returns a routing event that the workflow uses to pick the next
    node: ``AUTO_APPROVE`` for amounts under $100, ``NEEDS_REVIEW``
    for $100 and above.

    Also stores the expense data in workflow state so the HITL
    approval node can include it in the RequestInput payload.
    """
    ctx.state["expense_data"] = node_input
    amount = float(node_input.get("amount", 0))
    if amount >= config.review_threshold:
        return Event(
            route="NEEDS_REVIEW",
            output=node_input,
            state={"expense_data": node_input},
        )
    return Event(
        route="AUTO_APPROVE",
        output=node_input,
        state={"expense_data": node_input},
    )


def auto_approve(node_input: dict[str, Any]) -> Event:
    """Auto-approve a low-value expense (< $100) and log the decision."""
    amount = float(node_input.get("amount", 0))
    submitter = node_input.get("submitter", "unknown")
    category = node_input.get("category", "other")
    description = node_input.get("description", "")

    summary = (
        f"Expense auto-approved: ${amount:.2f} from {submitter} "
        f"for {category}" + (f' ("{description}")' if description else "") + "."
    )
    log_entry = {
        "severity": "INFO",
        "message": f"Expense auto-approved: ${amount:.2f} from {submitter}",
        "decision": "approved",
        "amount": amount,
        "submitter": submitter,
        "category": category,
    }
    print(json.dumps(log_entry), flush=True)
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=summary)],
        ),
        output={"status": "approved", "message": summary, **node_input},
    )


# ---------------------------------------------------------------------------
# LLM review agent (invoked only for expenses >= $100)
# ---------------------------------------------------------------------------


def emit_expense_alert(
    submitter: str,
    amount: float,
    category: str,
    risk_summary: str,
) -> dict[str, Any]:
    """Emit a structured log alerting finance to review a high-value expense.

    Cloud Run captures JSON stdout as structured logs in Cloud Logging.
    A log-based metric and alert policy trigger email notifications
    when these logs appear.

    Args:
        submitter: Who submitted the expense.
        amount: The expense amount in USD.
        category: The expense category.
        risk_summary: Why this expense needs review.

    Returns:
        Confirmation that the alert was emitted.
    """
    log_entry = {
        "severity": "WARNING",
        "message": (
            f"Expense review alert: ${amount:.2f} from {submitter} — {risk_summary}"
        ),
        "alert_type": "expense_review",
        "submitter": submitter,
        "amount": amount,
        "category": category,
        "risk_summary": risk_summary,
    }
    print(json.dumps(log_entry), flush=True)
    return {"status": "alert_emitted", "submitter": submitter, "amount": amount}


review_agent = Agent(
    name="review_agent",
    model=Gemini(
        model=config.model or MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    mode="single_turn",
    instruction="""You are an expense review agent. You receive expense reports
of $100 or more that need review before approval.

Analyze the expense and:
1. Check for risk factors: unusual category for the amount, vague description,
   suspiciously round numbers, very high value (>$1000), or potential policy
   violations.
2. Call the `emit_expense_alert` tool with the submitter, amount, category,
   and a brief risk summary explaining why this expense needs human review.
3. Return a structured review.

Your review MUST include:
- **Amount**: The expense amount
- **Submitter**: Who submitted it
- **Category**: The expense category
- **Risk level**: low, medium, or high
- **Risk factors**: What flags you found (if any)
- **Recommendation**: approve, request-more-info, or escalate""",
    input_schema=ExpenseData,
    tools=[emit_expense_alert],
)


# ---------------------------------------------------------------------------
# HITL: pause the workflow for human approval
# ---------------------------------------------------------------------------


def request_approval(node_input: Any, ctx: Context):
    """Pause the workflow and wait for a human to approve or reject.

    Yields a ``RequestInput`` that the ADK runtime surfaces to the UI.
    The workflow stays paused until someone resumes the session (via the
    approval UI or ``POST /run``). The human's response becomes the
    output of this node and flows into ``process_decision``.
    """
    expense = ctx.state.get("expense_data", {})
    yield RequestInput(
        message="Expense requires manager approval. Approve or reject.",
        payload=expense,
    )


def process_decision(node_input: Any, ctx: Context) -> Event:
    """Process the human's approval decision and log the outcome."""
    decision = "unknown"
    if isinstance(node_input, dict):
        decision = node_input.get("decision", "unknown")
    elif isinstance(node_input, str):
        try:
            parsed = json.loads(node_input)
            if isinstance(parsed, dict) and "decision" in parsed:
                decision = parsed["decision"]
            else:
                decision = "approve" if "approve" in node_input.lower() else "reject"
        except json.JSONDecodeError:
            decision = "approve" if "approve" in node_input.lower() else "reject"

    approved = decision == "approve"
    expense = ctx.state.get("expense_data", {})
    status = "approved" if approved else "rejected"

    log_entry = {
        "severity": "INFO" if approved else "WARNING",
        "message": f"Expense {status} by manager",
        "decision": status,
    }
    print(json.dumps(log_entry), flush=True)

    submitter = expense.get("submitter", "unknown")
    amount = float(expense.get("amount", 0))
    category = expense.get("category", "")
    description = expense.get("description", "")
    date = expense.get("date", "")

    parts = [f"${amount:.2f} expense from {submitter} has been {status}."]
    if description:
        parts.append(f'"{description}" ({category}) on {date}.')
    if approved:
        parts.append(
            "The expense has been logged and will be processed for reimbursement."
        )
    else:
        parts.append(
            "The submitter will be notified and may resubmit with additional documentation."
        )

    final_message = " ".join(parts)
    return Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=final_message)],
        ),
        output={"status": status, "message": final_message},
    )


# ---------------------------------------------------------------------------
# Graph-based workflow — the root agent
# ---------------------------------------------------------------------------

root_agent = Workflow(
    # Keep in sync with create_params.root_agent_name in agents-cli-manifest.yaml:
    # telemetry reports this name as gen_ai.agent.name, and agents-cli reports the
    # manifest's value. Renaming the agent only here makes the two disagree, and
    # anything selecting traces by name stops finding this agent's.
    name="ambient_expense_agent",
    edges=[
        ("START", parse_expense_email, route_by_amount),
        (
            route_by_amount,
            {
                "AUTO_APPROVE": auto_approve,
                "NEEDS_REVIEW": review_agent,
            },
        ),
        (review_agent, request_approval, process_decision),
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
)
