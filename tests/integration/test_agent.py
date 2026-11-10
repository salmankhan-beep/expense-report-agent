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

"""Integration tests for the ambient expense agent workflow and HITL flow."""

import base64
import json

import httpx
import pytest
import pytest_asyncio
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

import frontend.main as frontend_module
from app.agent import root_agent
from app.fast_api_app import app as backend_app
from frontend.main import app as frontend_app


def test_agent_stream() -> None:
    """Test that the agent returns valid streaming responses for an auto-approved expense."""
    session_service = InMemorySessionService()
    session = session_service.create_session_sync(user_id="test_user", app_name="test")
    runner = Runner(agent=root_agent, session_service=session_service, app_name="test")

    message = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text='{"amount": 45.00, "submitter": "bob@company.com", "category": "meals", "description": "Team lunch", "date": "2026-04-12"}'
            )
        ],
    )

    events = list(
        runner.run(
            new_message=message,
            user_id="test_user",
            session_id=session.id,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
    )
    assert len(events) > 0, "Expected at least one message"

    has_text_content = any(
        event.content
        and event.content.parts
        and any(part.text for part in event.content.parts)
        for event in events
    )
    assert has_text_content, "Expected at least one message with text content"


@pytest_asyncio.fixture
async def backend():
    """ASGI client for the backend (ADK agent + triggers)."""
    transport = httpx.ASGITransport(app=backend_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://backend") as c:
        yield c


@pytest_asyncio.fixture
async def frontend(monkeypatch):
    """ASGI client for the frontend (approval UI proxy)."""
    monkeypatch.setattr(frontend_module, "BACKEND_URL", "http://backend")
    monkeypatch.setattr(frontend_module, "APP_NAME", "app")

    backend_transport = httpx.ASGITransport(app=backend_app)
    original_async_client = httpx.AsyncClient

    class PatchedAsyncClient(original_async_client):
        def __init__(self, **kwargs):
            kwargs.setdefault("transport", backend_transport)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", PatchedAsyncClient)

    transport = httpx.ASGITransport(app=frontend_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://frontend") as c:
        yield c


def _make_pubsub_payload(expense: dict) -> dict:
    """Build a Pub/Sub trigger request body from an expense dict."""
    encoded = base64.b64encode(json.dumps(expense).encode()).decode()
    return {
        "message": {
            "data": encoded,
            "attributes": {"source": "test"},
        },
        "subscription": "test-sub",
    }


@pytest.mark.asyncio
async def test_auto_approve(backend):
    """Expenses under $100 are auto-approved without HITL."""
    payload = _make_pubsub_payload(
        {
            "amount": 45.00,
            "submitter": "bob@company.com",
            "category": "meals",
            "description": "Team lunch",
            "date": "2026-04-12",
        }
    )

    resp = await backend.post(
        "/apps/app/trigger/pubsub",
        json=payload,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


@pytest.mark.asyncio
async def test_review_and_hitl_approval(backend, frontend):
    """Expenses >= $100 go through review_agent, pause for RequestInput HITL, and resume on approval."""
    payload = _make_pubsub_payload(
        {
            "amount": 250.00,
            "submitter": "alice@company.com",
            "category": "travel",
            "description": "Flight to NYC for client meeting",
            "date": "2026-04-10",
        }
    )

    resp = await backend.post(
        "/apps/app/trigger/pubsub",
        json=payload,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"

    resp = await frontend.get("/pending-approvals")
    assert resp.status_code == 200
    pending = resp.json()
    assert len(pending) >= 1

    item = pending[0]
    assert item["payload"]["amount"] == 250.00
    assert item["payload"]["submitter"] == "alice@company.com"
    assert item["session_id"]
    assert item["interrupt_id"]

    approval_body = {
        "appName": "app",
        "userId": item["user_id"],
        "sessionId": item["session_id"],
        "newMessage": {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": item["interrupt_id"],
                        "name": "adk_request_input",
                        "response": {
                            "result": json.dumps({"decision": "approve"}),
                        },
                    }
                }
            ],
        },
    }

    resp = await frontend.post("/approve", json=approval_body)
    assert resp.status_code == 200
    events = resp.json()

    last_event = events[-1]
    assert last_event["output"]["status"] == "approved"
    assert "process_decision" in last_event["nodeInfo"]["path"]

    resp = await frontend.get("/pending-approvals")
    assert resp.status_code == 200
    assert len(resp.json()) == 0


@pytest.mark.asyncio
async def test_review_and_hitl_rejection(backend, frontend):
    """Rejecting an expense >= $100 logs the rejection and clears the pending list."""
    payload = _make_pubsub_payload(
        {
            "amount": 500.00,
            "submitter": "charlie@company.com",
            "category": "equipment",
            "description": "Standing desk",
            "date": "2026-04-11",
        }
    )

    resp = await backend.post(
        "/apps/app/trigger/pubsub",
        json=payload,
    )
    assert resp.status_code == 200

    resp = await frontend.get("/pending-approvals")
    pending = resp.json()
    item = next(p for p in pending if p["payload"]["amount"] == 500.00)

    rejection_body = {
        "appName": "app",
        "userId": item["user_id"],
        "sessionId": item["session_id"],
        "newMessage": {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "id": item["interrupt_id"],
                        "name": "adk_request_input",
                        "response": {
                            "result": json.dumps({"decision": "reject"}),
                        },
                    }
                }
            ],
        },
    }

    resp = await frontend.post("/approve", json=rejection_body)
    assert resp.status_code == 200
    events = resp.json()
    last_event = events[-1]
    assert last_event["output"]["status"] == "rejected"


@pytest.mark.asyncio
async def test_subscription_normalization(backend):
    """Full Pub/Sub subscription paths are normalized to short names."""
    payload = _make_pubsub_payload(
        {
            "amount": 30.00,
            "submitter": "dave@company.com",
            "category": "supplies",
            "description": "Printer paper",
            "date": "2026-04-13",
        }
    )
    payload["subscription"] = "projects/my-project/subscriptions/test-sub"

    resp = await backend.post(
        "/apps/app/trigger/pubsub",
        json=payload,
    )
    assert resp.status_code == 200

    resp = await backend.get("/apps/app/users/test-sub/sessions")
    assert resp.status_code == 200
    sessions = resp.json()
    assert len(sessions) >= 1


def test_duplicate_claim_recalled_across_sessions() -> None:
    """A claim approved in one session is remembered, so an identical claim in a
    later session is routed to review and the reviewer checks for duplicates.

    Runs in its own event loop with a timeout on every step so a stalled model
    call fails the test instead of hanging it.
    """
    import asyncio

    from google.adk.runners import InMemoryRunner

    from app.agent import app as adk_app

    step_timeout_s = 90
    claim = json.dumps(
        {
            "amount": 42.0,
            "submitter": "dana@company.com",
            "category": "meals",
            "description": "Lunch with Initech",
            "date": "2026-05-02",
        }
    )

    async def submit(runner: InMemoryRunner) -> list:
        session = await runner.session_service.create_session(
            app_name=adk_app.name, user_id="default-user"
        )
        message = types.Content(role="user", parts=[types.Part.from_text(text=claim)])
        stream = runner.run_async(
            user_id="default-user", session_id=session.id, new_message=message
        ).__aiter__()
        events = []
        while True:
            try:
                events.append(
                    await asyncio.wait_for(stream.__anext__(), step_timeout_s)
                )
            except StopAsyncIteration:
                return events

    async def scenario() -> tuple[list, list]:
        runner = InMemoryRunner(app=adk_app)  # includes an in-memory memory service
        return await submit(runner), await submit(runner)

    def calls(events: list) -> list[str]:
        return [
            p.function_call.name
            for e in events
            for p in (e.content.parts if e.content else [])
            if p.function_call
        ]

    first, second = asyncio.run(scenario())

    assert any(
        isinstance(e.output, dict) and e.output.get("status") == "approved"
        for e in first
    )
    assert "adk_request_input" not in calls(first)

    second_calls = calls(second)
    assert "check_duplicate_claims" in second_calls
    assert "adk_request_input" in second_calls  # paused for a human
