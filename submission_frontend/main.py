"""Manager dashboard for the ambient expense agent.

A standalone FastAPI service that sits in front of the already-deployed
expense agent on Agent Runtime. It does not modify the agent.

- ``GET /``                         dashboard UI (static/index.html)
- ``GET /api/pending``              expenses paused on ``adk_request_input``
- ``POST /api/action/{session_id}`` approve / reject, resuming the paused run

Configuration (environment variables):
    GOOGLE_CLOUD_PROJECT     GCP project ID.
    AGENT_RUNTIME_ID         Full resource name
                             (projects/P/locations/L/reasoningEngines/ID)
                             or just the numeric engine ID.
    AGENT_RUNTIME_LOCATION   Engine region when AGENT_RUNTIME_ID is a bare ID
                             (default: us-east1).
"""

import asyncio
import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import truststore

# Verify TLS against the OS trust store instead of the bundled CA list. On
# managed Macs the client libraries switch to Google's mTLS endpoints, whose
# chain only verifies against the system store. Verification stays enabled.
truststore.inject_into_ssl()

import vertexai  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from google.adk.sessions import VertexAiSessionService  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from vertexai import agent_engines  # noqa: E402

logger = logging.getLogger("submission_frontend")
logging.basicConfig(level=logging.INFO)

# Sessions are resumed as this user. The session service only lets the user
# who owns a session resume it, so the event pipeline must create sessions
# under the same ID.
DEFAULT_USER = "default-user"
REQUEST_INPUT_TOOL = "adk_request_input"
STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class Settings(BaseModel):
    project: str
    location: str
    engine_id: str

    @property
    def resource_name(self) -> str:
        return (
            f"projects/{self.project}/locations/{self.location}"
            f"/reasoningEngines/{self.engine_id}"
        )


@lru_cache
def settings() -> Settings:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    runtime_id = os.environ.get("AGENT_RUNTIME_ID")
    if not project or not runtime_id:
        raise RuntimeError(
            "Set GOOGLE_CLOUD_PROJECT and AGENT_RUNTIME_ID before starting the service."
        )
    match = re.fullmatch(
        r"projects/[^/]+/locations/([^/]+)/reasoningEngines/([^/]+)", runtime_id
    )
    if match:
        location, engine_id = match.groups()
    else:
        location = os.environ.get("AGENT_RUNTIME_LOCATION", "us-east1")
        engine_id = runtime_id
    return Settings(project=project, location=location, engine_id=engine_id)


@lru_cache
def session_service() -> VertexAiSessionService:
    s = settings()
    return VertexAiSessionService(
        project=s.project, location=s.location, agent_engine_id=s.engine_id
    )


@lru_cache
def remote_agent() -> Any:
    s = settings()
    vertexai.init(project=s.project, location=s.location)
    return agent_engines.get(s.resource_name)


# ---------------------------------------------------------------------------
# Session-history parsing
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    """Payloads may arrive as dicts or JSON strings."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _parts(event: Any) -> list[Any]:
    content = getattr(event, "content", None)
    return list(getattr(content, "parts", None) or [])


def find_pending_requests(events: list[Any]) -> list[dict[str, Any]]:
    """Return unanswered ``adk_request_input`` calls plus the agent's review.

    A request is pending when its function-call ID has no matching
    ``adk_request_input`` function response later in the history.
    """
    requests: dict[str, dict[str, Any]] = {}
    answered: set[str] = set()
    review_text: str | None = None
    risk_summary: str | None = None

    for event in events:
        author = getattr(event, "author", None)
        for part in _parts(event):
            call = getattr(part, "function_call", None)
            if call is not None:
                args = dict(call.args or {})
                if call.name == REQUEST_INPUT_TOOL and call.id:
                    requests[call.id] = {
                        "interrupt_id": call.id,
                        "message": args.get("message", ""),
                        "expense": _as_dict(args.get("payload")),
                    }
                elif call.name == "emit_expense_alert":
                    risk_summary = args.get("risk_summary") or risk_summary

            response = getattr(part, "function_response", None)
            if response is not None and response.name == REQUEST_INPUT_TOOL:
                answered.add(response.id)

            text = getattr(part, "text", None)
            if (
                author == "review_agent"
                and text
                and not getattr(part, "thought", False)
            ):
                review_text = text

    return [
        {**req, "review": review_text, "risk_summary": risk_summary}
        for interrupt_id, req in requests.items()
        if interrupt_id not in answered
    ]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

app = FastAPI(title="Expense Manager Dashboard")


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/pending")
async def pending() -> list[dict[str, Any]]:
    """List every session paused on an unanswered ``adk_request_input``."""
    s = settings()
    svc = session_service()
    try:
        listing = await svc.list_sessions(app_name=s.engine_id)
    except Exception as exc:
        logger.exception("list_sessions failed")
        raise HTTPException(502, f"Could not list sessions: {exc}") from exc

    limit = asyncio.Semaphore(8)

    async def inspect(summary: Any) -> list[dict[str, Any]]:
        async with limit:
            try:
                full = await svc.get_session(
                    app_name=s.engine_id,
                    user_id=summary.user_id,
                    session_id=summary.id,
                )
            except Exception:
                logger.exception("get_session failed for %s", summary.id)
                return []
        if full is None:
            return []
        return [
            {
                "session_id": full.id,
                "user_id": full.user_id,
                "last_update_time": full.last_update_time,
                # Only sessions owned by DEFAULT_USER can be resumed from here.
                "actionable": full.user_id == DEFAULT_USER,
                **item,
            }
            for item in find_pending_requests(full.events)
        ]

    results = await asyncio.gather(*(inspect(x) for x in listing.sessions))
    items = [item for group in results for item in group]
    items.sort(key=lambda i: i.get("last_update_time") or 0, reverse=True)
    return items


class Action(BaseModel):
    interrupt_id: str
    approved: bool


@app.post("/api/action/{session_id}")
async def action(session_id: str, body: Action) -> dict[str, Any]:
    """Approve or reject a paused expense and resume its run on Agent Runtime."""
    s = settings()

    # Check ownership and that the interrupt is still open, so a double click
    # or a stale card can't resume the run twice.
    try:
        session = await session_service().get_session(
            app_name=s.engine_id, user_id=DEFAULT_USER, session_id=session_id
        )
    except ValueError as exc:
        # Raised when the session exists but belongs to another user.
        raise HTTPException(
            403,
            f"Session {session_id} belongs to another user. Only sessions "
            f"created as '{DEFAULT_USER}' can be resumed here.",
        ) from exc
    except Exception as exc:
        raise HTTPException(502, f"Could not load session: {exc}") from exc
    if session is None:
        raise HTTPException(
            404,
            f"Session {session_id} was not found for user '{DEFAULT_USER}'. "
            "Only sessions created as that user can be resumed here.",
        )
    open_ids = {r["interrupt_id"] for r in find_pending_requests(session.events)}
    if body.interrupt_id not in open_ids:
        raise HTTPException(409, "This expense has already been decided.")

    # The agent's process_decision node reads the "decision" key, so it is sent
    # alongside "approved"; with "approved" alone the agent records a rejection.
    message = {
        "role": "user",
        "parts": [
            {
                "function_response": {
                    "id": body.interrupt_id,
                    "name": REQUEST_INPUT_TOOL,
                    "response": {
                        "approved": body.approved,
                        "decision": "approve" if body.approved else "reject",
                    },
                }
            }
        ],
    }

    final: dict[str, Any] | None = None
    texts: list[str] = []
    try:
        async for event in remote_agent().async_stream_query(
            message=message, user_id=DEFAULT_USER, session_id=session_id
        ):
            output = event.get("output") if isinstance(event, dict) else None
            if isinstance(output, dict) and "status" in output:
                final = output
            for part in (event.get("content") or {}).get("parts") or []:
                if part.get("text"):
                    texts.append(part["text"])
    except Exception as exc:
        logger.exception("resume failed for %s", session_id)
        raise HTTPException(502, f"Agent Runtime rejected the resume: {exc}") from exc

    if final is None:
        raise HTTPException(
            502, "The agent resumed but returned no decision. Check the engine logs."
        )

    pending_item = next(
        (
            r
            for r in find_pending_requests(session.events)
            if r["interrupt_id"] == body.interrupt_id
        ),
        {},
    )
    return {
        "session_id": session_id,
        "status": final.get("status"),
        "message": final.get("message") or (texts[-1] if texts else ""),
        "expense": pending_item.get("expense", {}),
        "review": pending_item.get("review"),
        "risk_summary": pending_item.get("risk_summary"),
    }


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8081")))
