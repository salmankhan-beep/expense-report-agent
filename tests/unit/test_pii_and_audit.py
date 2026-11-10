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

"""Unit tests for PII scrubbing and the tool intent/outcome audit trail."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app import pii, tool_audit
from app.agent import auto_approve, route_by_amount
from app.schemas import DuplicateCheckResult
from app.tools import format_record
from tests.unit.test_expense_agent import FakeContext, expense

# --- scrubbing -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("Contact jane.doe@example.com for the receipt", "[EMAIL]"),
        ("Call me on +1 415-555-0132 about it", "[PHONE]"),
        ("Paid with card 4111 1111 1111 1111", "[CARD]"),
        ("SSN 123-45-6789 on file", "[SSN]"),
        ("Refund to DE89 3704 0044 0532 0130 00", "[IBAN]"),
    ],
)
def test_regex_scrubber_redacts(text, token) -> None:
    scrubbed = pii.scrub_text_regex(text)
    assert token in scrubbed


@pytest.mark.parametrize(
    "text",
    [
        "Client dinner with Acme Corp on 2026-05-06",
        "Flight to NYC, $1850.00, order 4111 1111 1111 1112",  # fails Luhn
        "Invoice 2026-0042 for 3 licenses",
    ],
)
def test_regex_scrubber_keeps_business_context(text) -> None:
    assert pii.scrub_text_regex(text) == text


def test_dlp_backend_used_when_enabled(monkeypatch) -> None:
    calls = []

    class FakeDlp:
        def deidentify_content(self, request):
            calls.append(request)
            return SimpleNamespace(item=SimpleNamespace(value="[STREET_ADDRESS]"))

    monkeypatch.setenv("PII_SCRUBBER", "dlp")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-project")
    monkeypatch.setattr(pii, "_dlp_client", lambda: FakeDlp())
    assert pii.scrub_text("1600 Amphitheatre Parkway") == "[STREET_ADDRESS]"
    assert calls[0]["parent"] == "projects/demo-project/locations/global"


def test_dlp_failure_falls_back_to_regex(monkeypatch) -> None:
    def broken():
        raise RuntimeError("DLP API disabled")

    monkeypatch.setenv("PII_SCRUBBER", "dlp")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-project")
    monkeypatch.setattr(pii, "_dlp_client", broken)
    assert pii.scrub_text("mail me at a@b.com") == "mail me at [EMAIL]"


def test_pseudonym_is_keyed(monkeypatch) -> None:
    default = pii.pseudonymize("bob@company.com")
    assert default == pii.pseudonymize("  BOB@company.com ")
    pii._hash_key.cache_clear()
    monkeypatch.setenv("PII_HASH_KEY", "another-secret")
    try:
        assert pii.pseudonymize("bob@company.com") != default
    finally:
        pii._hash_key.cache_clear()


# --- nothing persisted contains PII ---------------------------------------


def test_memory_record_contains_no_pii() -> None:
    fact = format_record(
        expense(description="Dinner, call 415-555-0132, card 4111 1111 1111 1111"),
        "approved",
    )
    assert "@" not in fact
    assert "415-555" not in fact and "4111" not in fact
    assert "[PHONE]" in fact and "[CARD]" in fact


def test_logs_contain_no_emails(capsys) -> None:
    asyncio.run(route_by_amount(expense(amount=250.0), FakeContext()))
    asyncio.run(
        auto_approve(expense(description="ping bob@company.com"), FakeContext())
    )
    out = capsys.readouterr().out
    assert "@" not in out
    entries = [json.loads(line) for line in out.splitlines()]
    assert all("submitter" not in e for e in entries)
    assert all(
        e["submitter_id"].startswith("sub_") for e in entries if "submitter_id" in e
    )


# --- tool intent vs outcome --------------------------------------------------


class FakeToolContext:
    def __init__(self, call_id="call-1"):
        self.function_call_id = call_id
        self.agent_name = "senior_review_agent"
        self.state = {"review_model": "gemini-3.1-pro-preview"}


def tool(name):
    return SimpleNamespace(name=name)


def logs(capsys, event):
    return [
        e
        for e in (json.loads(line) for line in capsys.readouterr().out.splitlines())
        if e["event"] == event
    ]


def test_intent_and_outcome_are_linked(capsys) -> None:
    ctx = FakeToolContext()
    args = {"submitter": "bob@company.com", "amount": 45.0, "date": "2026-04-12"}
    tool_audit.before_tool_callback(tool("check_duplicate_claims"), args, ctx)
    result = DuplicateCheckResult(is_duplicate=True)
    tool_audit.after_tool_callback(
        tool("check_duplicate_claims"), args, ctx, {"result": result}
    )

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    intent, outcome = lines
    assert intent["event"] == "tool_intent" and outcome["event"] == "tool_outcome"
    assert intent["call_id"] == outcome["call_id"] == "call-1"
    assert intent["review_model"] == "gemini-3.1-pro-preview"
    assert intent["args"]["submitter_id"].startswith("sub_")
    assert "submitter" not in intent["args"]
    assert outcome["status"] == "ok" and outcome["intent_achieved"] is True
    assert outcome["result"] == {"is_duplicate": True}
    assert outcome["latency_ms"] is not None


def test_failed_tool_outcome_records_error_and_recovery(capsys) -> None:
    ctx = FakeToolContext("call-2")
    tool_audit.before_tool_callback(tool("check_duplicate_claims"), {}, ctx)
    bad = DuplicateCheckResult(
        status="error", error="Invalid input", recovery="Call again"
    )
    tool_audit.after_tool_callback(tool("check_duplicate_claims"), {}, ctx, bad)
    (outcome,) = logs(capsys, "tool_outcome")
    assert outcome["severity"] == "WARNING"
    assert outcome["intent_achieved"] is False
    assert outcome["recovery"] == "Call again"


def test_tool_exception_becomes_recoverable_error(capsys) -> None:
    ctx = FakeToolContext("call-3")
    tool_audit.before_tool_callback(tool("get_spending_history"), {}, ctx)
    response = tool_audit.on_tool_error_callback(
        tool("get_spending_history"), {}, ctx, TimeoutError("memory timed out")
    )
    assert response["status"] == "error"
    assert "request-more-info" in response["recovery"]
    (outcome,) = logs(capsys, "tool_outcome")
    assert outcome["status"] == "exception"
    assert "TimeoutError" in outcome["error"]


def test_reviewers_have_audit_callbacks() -> None:
    from app.agent import review_agent, senior_review_agent

    for reviewer in (review_agent, senior_review_agent):
        assert reviewer.before_tool_callback is tool_audit.before_tool_callback
        assert reviewer.after_tool_callback is tool_audit.after_tool_callback
        assert reviewer.on_tool_error_callback is tool_audit.on_tool_error_callback
