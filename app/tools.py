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

"""Reviewer tools and the expense-history memory they read.

Every decision is written to the memory service (Memory Bank on Agent Runtime,
in-memory locally) as one structured fact:

    EXPENSE_RECORD | submitter=sub_3f9a... | amount=45.00 | category=meals |
    date=2026-04-12 | status=approved | description=Team lunch

The submitter is stored as a keyed pseudonymous ID (``app.pii.pseudonymize``)
and the description is PII-scrubbed, so Memory Bank holds no email addresses,
phone numbers, card numbers, or similar.

Memory recall is a similarity search, so the tools parse recalled facts back
into fields and filter them in code. Duplicate detection is therefore exact,
while spending history covers the most relevant recalled records rather than
a complete ledger.
"""

import logging
import re
from typing import Any

from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types
from pydantic import ValidationError

from app import observability as obs
from app import pii
from app.expense_policy import EXPENSE_POLICY, normalize_category
from app.schemas import (
    AlertResult,
    ClaimKey,
    DuplicateCheckResult,
    ExpenseRecord,
    PolicyLookupResult,
    SpendingHistoryResult,
    SubmitterRef,
)

logger = logging.getLogger(__name__)

RECORD_PREFIX = "EXPENSE_RECORD"
_FIELDS = ("submitter", "amount", "category", "date", "status", "description")


# ---------------------------------------------------------------------------
# Record format
# ---------------------------------------------------------------------------


def _clean(value: Any, max_len: int = 200) -> str:
    """Strip characters that could forge extra fields inside a record."""
    text = re.sub(r"[|=\r\n]+", " ", str(value or "")).strip()
    return text[:max_len]


def format_record(expense: dict[str, Any], status: str) -> str:
    """Serialize a decided expense as a single, PII-free memory fact."""
    amount = float(expense.get("amount", 0) or 0)
    description = pii.scrub_text(str(expense.get("description") or ""))
    return (
        f"{RECORD_PREFIX} | submitter={pii.pseudonymize(str(expense.get('submitter') or ''))}"
        f" | amount={amount:.2f}"
        f" | category={normalize_category(str(expense.get('category', '')))}"
        f" | date={_clean(expense.get('date'), 20)}"
        f" | status={_clean(status, 20)}"
        f" | description={_clean(description)}"
    )


def parse_record(text: str) -> dict[str, Any] | None:
    """Parse a memory fact written by ``format_record``; None if it isn't one."""
    if not text or not text.strip().startswith(RECORD_PREFIX):
        return None
    record: dict[str, Any] = {}
    for chunk in text.split(" | ")[1:]:
        key, sep, value = chunk.partition("=")
        if sep and key in _FIELDS:
            record[key] = value.strip()
    if not {"submitter", "amount", "date"} <= record.keys():
        return None
    try:
        record["amount"] = float(record["amount"])
    except ValueError:
        return None
    return record


def find_duplicates(
    records: list[dict[str, Any]], amount: float, date: str
) -> list[dict[str, Any]]:
    """Records with the same amount (to the cent) on the same date."""
    return [
        r
        for r in records
        if abs(r["amount"] - float(amount)) < 0.005 and r.get("date") == date
    ]


# ---------------------------------------------------------------------------
# Memory access
# ---------------------------------------------------------------------------


class MemoryUnavailableError(RuntimeError):
    """Expense history could not be read.

    ``configured`` is False when no memory service exists at all (memory is
    simply off), True when a configured service failed.
    """

    def __init__(self, message: str, *, configured: bool):
        super().__init__(message)
        self.configured = configured


async def recall_records(ctx: Context, submitter: str) -> list[dict[str, Any]]:
    """Recall this submitter's past expense records from memory.

    Raises:
        MemoryUnavailableError: if history can't be read.
    """
    who = pii.pseudonymize(submitter)
    with obs.span("memory.recall", submitter_id=who) as recall_span:
        try:
            response = await ctx.search_memory(f"{RECORD_PREFIX} submitter={who}")
        except ValueError as exc:
            # Raised by ADK when no memory service is configured.
            obs.set_attributes(recall_span, memory_available=False, records=0)
            raise MemoryUnavailableError(str(exc), configured=False) from exc
        except Exception as exc:
            logger.exception("Memory recall failed.")
            obs.set_attributes(recall_span, memory_available=False, records=0)
            raise MemoryUnavailableError(str(exc), configured=True) from exc
        records = []
        seen: set[str] = set()
        for memory in response.memories:
            parts = memory.content.parts if memory.content else []
            text = " ".join(p.text for p in parts or [] if p.text)
            record = parse_record(text)
            if record and record["submitter"] == who and text not in seen:
                seen.add(text)
                records.append(record)
        obs.set_attributes(
            recall_span,
            memory_available=True,
            memories_returned=len(response.memories),
            records=len(records),
        )
        return records


async def record_expense_decision(
    ctx: Context, expense: dict[str, Any], status: str
) -> None:
    """Write a decided expense to memory. Never blocks the decision on failure."""
    fact = format_record(expense, status)
    content = types.Content(role="user", parts=[types.Part.from_text(text=fact)])
    with obs.span("memory.write", status=status) as write_span:
        method = "add_memory"
        try:
            await ctx.add_memory(memories=[MemoryEntry(content=content)])
            written = True
        except NotImplementedError:
            # InMemoryMemoryService has no direct writes; store it as an event.
            method = "add_events_to_memory"
            try:
                await ctx.add_events_to_memory(
                    events=[Event(author="expense_ledger", content=content)]
                )
                written = True
            except Exception:
                logger.exception("Could not record expense decision in memory.")
                written = False
        except ValueError:
            logger.warning("Memory service unavailable; decision not recorded.")
            method, written = "none", False
        except Exception:
            logger.exception("Could not record expense decision in memory.")
            written = False
        obs.set_attributes(write_span, method=method, written=written)


# ---------------------------------------------------------------------------
# Tools for the review agent
# ---------------------------------------------------------------------------


_HISTORY_NOTE = (
    "Recall returns the most relevant remembered claims for this submitter, "
    "not a complete ledger."
)
_MEMORY_DOWN_RECOVERY = (
    "Do not retry. Continue the review without history: say in the review "
    "that history and duplicate checks were unavailable, raise the risk level "
    "by one step, and recommend request-more-info."
)


def _invalid_input(exc: ValidationError) -> str:
    return "; ".join(e["msg"] for e in exc.errors())


def lookup_expense_policy(category: str) -> PolicyLookupResult:
    """Look up the company expense policy for a category.

    Args:
        category: The expense category as submitted, e.g. "meals", "flight",
            "hotel". Unknown categories map to "other".

    Returns:
        status "ok" with the matched policy category, whether it matched
        exactly, the per-claim limit in USD, the amount above which a receipt
        is required, and category rules. On failure, status "error" with
        recovery instructions.
    """
    try:
        submitted = (category or "").strip()
        key = normalize_category(submitted)
        policy = EXPENSE_POLICY[key]
        matched = key == submitted.lower() or key != "other"
        return PolicyLookupResult(
            category=key,
            matched_exactly=matched,
            per_claim_limit_usd=policy["per_claim_limit_usd"],
            receipt_required_over_usd=policy["receipt_required_over_usd"],
            rules=list(policy["rules"]),
        )
    except Exception as exc:  # never let a policy-table problem crash the review
        logger.exception("Policy lookup failed.")
        return PolicyLookupResult(
            status="error",
            error=f"Policy lookup failed: {exc}",
            recovery=(
                "Retry once with category 'other'. If it still fails, state in the "
                "review that policy limits could not be verified and recommend "
                "request-more-info."
            ),
        )


async def get_spending_history(
    submitter: str, tool_context: Context
) -> SpendingHistoryResult:
    """Recall a submitter's past expense claims and decisions from memory.

    Args:
        submitter: The submitter's email address, exactly as in the expense.

    Returns:
        status "ok" with recent recalled claims (amount, category, date,
        status), the claim count, the approved total, and the rejected count.
        Recall covers the most relevant remembered claims, not a complete
        ledger. On failure, status "error" with recovery instructions.
    """
    try:
        ref = SubmitterRef(submitter=submitter)
    except ValidationError as exc:
        return SpendingHistoryResult(
            status="error",
            error=f"Invalid input: {_invalid_input(exc)}",
            recovery=(
                "Call again with the submitter's email address copied exactly "
                "from the expense's 'submitter' field."
            ),
        )
    try:
        records = await recall_records(tool_context, ref.submitter)
    except MemoryUnavailableError as exc:
        return SpendingHistoryResult(
            status="error",
            submitter=ref.submitter,
            error=f"Expense history is unavailable: {exc}",
            recovery=_MEMORY_DOWN_RECOVERY,
        )
    approved = [r for r in records if r.get("status") == "approved"]
    return SpendingHistoryResult(
        submitter=ref.submitter,
        claims_recalled=len(records),
        approved_total_usd=round(sum(r["amount"] for r in approved), 2),
        rejected_count=sum(1 for r in records if r.get("status") == "rejected"),
        recent_claims=[ExpenseRecord(**r) for r in records[:10]],
        note=_HISTORY_NOTE,
    )


async def check_duplicate_claims(
    submitter: str, amount: float, date: str, tool_context: Context
) -> DuplicateCheckResult:
    """Check whether this submitter already claimed the same amount on the same date.

    Args:
        submitter: The submitter's email address, exactly as in the expense.
        amount: The claim amount in USD (greater than 0).
        date: The expense date, formatted YYYY-MM-DD.

    Returns:
        status "ok" with is_duplicate and the matching past claims. On
        failure, status "error" with recovery instructions.
    """
    try:
        key = ClaimKey(submitter=submitter, amount=amount, date=date)
    except ValidationError as exc:
        return DuplicateCheckResult(
            status="error",
            error=f"Invalid input: {_invalid_input(exc)}",
            recovery=(
                "Call again using the expense's own fields: 'submitter' as an email "
                "address, 'amount' as a positive number, and 'date' as YYYY-MM-DD. "
                "If the expense has no valid date, skip this check and say so in "
                "the review."
            ),
        )
    try:
        records = await recall_records(tool_context, key.submitter)
    except MemoryUnavailableError as exc:
        return DuplicateCheckResult(
            status="error",
            error=f"Expense history is unavailable: {exc}",
            recovery=_MEMORY_DOWN_RECOVERY,
        )
    matches = find_duplicates(records, key.amount, key.date)
    return DuplicateCheckResult(
        is_duplicate=bool(matches),
        matching_claims=[ExpenseRecord(**r) for r in matches],
    )


def emit_expense_alert(
    submitter: str,
    amount: float,
    category: str,
    risk_summary: str,
) -> AlertResult:
    """Emit a structured alert telling finance a flagged expense needs review.

    The alert is a WARNING log with ``alert_type="expense_review"``, linked to
    the current trace. A log-based metric and alert policy
    (``deployment/terraform/single-project/monitoring.tf``) turn it into a
    notification.

    Args:
        submitter: Who submitted the expense (email address).
        amount: The expense amount in USD.
        category: The expense category.
        risk_summary: One sentence on why this expense needs human review.

    Returns:
        status "ok" with alert_emitted true. On failure, status "error" with
        recovery instructions.
    """
    if not (risk_summary or "").strip():
        return AlertResult(
            status="error",
            error="risk_summary is empty.",
            recovery="Call again with a one-sentence risk summary.",
        )
    try:
        obs.set_attributes(
            obs.trace.get_current_span(),
            alert_type="expense_review",
            amount_usd=float(amount),
        )
        obs.log_event(
            "expense_review_alert",
            f"Expense review alert: ${float(amount):.2f} from {submitter} — {risk_summary}",
            severity="WARNING",
            alert_type="expense_review",
            submitter=submitter,
            amount=float(amount),
            category=category,
            risk_summary=risk_summary,
        )
    except Exception as exc:
        logger.exception("Could not emit expense alert.")
        return AlertResult(
            status="error",
            error=f"Alert could not be emitted: {exc}",
            recovery=(
                "Do not retry. Finish the review and state that the finance alert "
                "could not be sent; the workflow still pauses for a human."
            ),
        )
    return AlertResult(alert_emitted=True, submitter=submitter, amount=float(amount))
