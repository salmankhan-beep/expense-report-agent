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

import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from a2a.server.tasks import InMemoryTaskStore
from dotenv import load_dotenv
from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner
from starlette.requests import Request

from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes
from app.app_utils.reasoning_engine_adapter import (
    attach_reasoning_engine_routes,
)

load_dotenv()
logger = logging.getLogger(__name__)
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)
otel_to_cloud = os.environ.get(
    "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", ""
).lower() in ("true", "1")

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.agent import app as adk_app
    from app.agent import root_agent

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        memory_service=services.get_memory_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    yield


def _trigger_config() -> dict[str, Any]:
    """Pub/Sub trigger settings, failing closed outside local development.

    - ``TRIGGER_OIDC_AUDIENCE`` set: Pub/Sub push requests must carry a
      Google-signed OIDC token for that audience (optionally restricted to
      ``TRIGGER_OIDC_SERVICE_ACCOUNTS``, comma-separated).
    - Local dev (not on Agent Runtime / Cloud Run): triggers are enabled with
      no auth so the integration tests and local curl testing work.
    - Deployed without an audience: trigger routes are NOT registered.
    """
    audience = os.getenv("TRIGGER_OIDC_AUDIENCE")
    if audience:
        accounts = [
            a.strip()
            for a in os.getenv("TRIGGER_OIDC_SERVICE_ACCOUNTS", "").split(",")
            if a.strip()
        ]
        return {
            "trigger_sources": ["pubsub"],
            "trigger_oidc_audience": audience,
            "trigger_oidc_service_accounts": accounts or None,
        }
    is_deployed = bool(
        os.getenv("GOOGLE_CLOUD_AGENT_ENGINE_ID") or os.getenv("K_SERVICE")
    )
    if is_deployed:
        logger.warning(
            "TRIGGER_OIDC_AUDIENCE is not set; Pub/Sub trigger routes are disabled."
        )
        return {}
    return {
        "trigger_sources": ["pubsub"],
        "trigger_auth_verifier": lambda _request: None,  # local dev only
    }


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    memory_service_uri=services.MEMORY_SERVICE_URI,
    otel_to_cloud=otel_to_cloud,
    lifespan=lifespan,
    **_trigger_config(),
)
app.title = "ambient-expense-agent"
app.description = "API for interacting with the Agent ambient-expense-agent"


@app.middleware("http")
async def normalize_pubsub_subscription(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Normalize ``projects/.../subscriptions/NAME`` to just ``NAME``.

    The ADK trigger handler uses the subscription as the session ``user_id``;
    the approval UI queries sessions by the short name.
    """
    if request.url.path.endswith("/trigger/pubsub") and request.method == "POST":
        body = await request.body()
        try:
            data = json.loads(body)
            sub = data.get("subscription", "")
            if "/" in sub:
                data["subscription"] = sub.rsplit("/", 1)[-1]
                request._body = json.dumps(data).encode()
        except (json.JSONDecodeError, AttributeError):
            pass
    return await call_next(request)


# Proxy routes so the Google Cloud console playground (reasoning_engine SDK) can
# talk to this agent alongside the native adk_api routes.
attach_reasoning_engine_routes(app)


# Main execution
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
