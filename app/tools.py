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

    EXPENSE_RECORD | submitter=a@x.com | amount=45.00 | category=meals |
    date=2026-04-12 | status=approved | description=Team lunch

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

from app import observability as obs
from app.expense_policy import EXPENSE_POLICY, normalize_category

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
    """Serialize a decided expense as a single memory fact."""
    amount = float(expense.get("amount", 0) or 0)
    return (
        f"{RECORD_PREFIX} | submitter={_clean(expense.get('submitter')).lower()}"
        f" | amount={amount:.2f}"
        f" | category={normalize_category(str(expense.get('category', '')))}"
        f" | date={_clean(expense.get('date'), 20)}"
        f" | status={_clean(status, 20)}"
        f" | description={_clean(expense.get('description'))}"
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


async def recall_records(ctx: Context, submitter: str) -> list[dict[str, Any]]:
    """Recall this submitter's past expense records from memory."""
    who = _clean(submitter).lower()
    with obs.span("memory.recall", submitter_id=obs.submitter_id(who)) as recall_span:
        try:
            response = await ctx.search_memory(f"{RECORD_PREFIX} submitter={who}")
        except ValueError:
            # No memory service configured (e.g. a bare Runner in tests).
            logger.warning("Memory service unavailable; no expense history recalled.")
            obs.set_attributes(recall_span, memory_available=False, records=0)
            return []
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


def lookup_expense_policy(category: str) -> dict[str, Any]:
    """Look up the company expense policy for a category.

    Args:
        category: The expense category as submitted, e.g. "meals", "flight",
            "hotel". Unknown categories map to "other".

    Returns:
        The matched policy category, its per-claim limit in USD, the amount
        above which a receipt is required, and any category-specific rules.
    """
    key = normalize_category(category)
    policy = EXPENSE_POLICY[key]
    return {
        "category": key,
        "per_claim_limit_usd": policy["per_claim_limit_usd"],
        "receipt_required_over_usd": policy["receipt_required_over_usd"],
        "rules": policy["rules"],
    }


async def get_spending_history(submitter: str, tool_context: Context) -> dict[str, Any]:
    """Recall a submitter's past expense claims and decisions from memory.

    Args:
        submitter: The submitter's email address.

    Returns:
        Recent recalled claims (amount, category, date, status), with the
        claim count, the approved total, and the rejected count. This covers
        the most relevant remembered claims, not a complete ledger.
    """
    records = await recall_records(tool_context, submitter)
    approved = [r for r in records if r.get("status") == "approved"]
    return {
        "submitter": _clean(submitter).lower(),
        "claims_recalled": len(records),
        "approved_total_usd": round(sum(r["amount"] for r in approved), 2),
        "rejected_count": sum(1 for r in records if r.get("status") == "rejected"),
        "recent_claims": records[:10],
    }


async def check_duplicate_claims(
    submitter: str, amount: float, date: str, tool_context: Context
) -> dict[str, Any]:
    """Check whether this submitter already claimed the same amount on the same date.

    Args:
        submitter: The submitter's email address.
        amount: The claim amount in USD.
        date: The expense date, YYYY-MM-DD.

    Returns:
        Whether a duplicate was found, and the matching past claims.
    """
    records = await recall_records(tool_context, submitter)
    matches = find_duplicates(records, amount, date)
    return {"is_duplicate": bool(matches), "matching_claims": matches}
