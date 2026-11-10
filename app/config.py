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

"""Centralized configuration for the ambient expense agent."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_DEEP_MODEL = "gemini-3.1-pro-preview"


def _env_flag(name: str) -> bool | None:
    """Parse a boolean env var; None when it isn't set."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip().lower() in ("1", "true", "yes")


def use_google_cloud() -> bool:
    """Decide which Gemini backend to use.

    An explicit GOOGLE_GENAI_USE_ENTERPRISE (or the older
    GOOGLE_GENAI_USE_VERTEXAI) always wins. Otherwise an AI Studio key
    (GEMINI_API_KEY or GOOGLE_API_KEY) selects AI Studio, and no key selects
    Google Cloud with Application Default Credentials.
    """
    explicit = _env_flag("GOOGLE_GENAI_USE_ENTERPRISE")
    if explicit is None:
        explicit = _env_flag("GOOGLE_GENAI_USE_VERTEXAI")
    if explicit is not None:
        return explicit
    return not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))


# --- Authentication ---
# Both flags are set to the same value so the GenAI SDK never sees a conflict.
if use_google_cloud():
    try:
        import google.auth

        _, project_id = google.auth.default()
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id or "")
    except Exception:
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")
    os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = "True"
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
else:
    os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = "False"
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"


@dataclass
class ExpenseAgentConfig:
    """Agent configuration with sensible defaults."""

    # Standard review: fast, inexpensive model for straightforward flagged claims.
    model: str = os.getenv("MODEL_NAME", DEFAULT_MODEL)
    # Deep review: stronger reasoning model for complex or high-risk claims.
    deep_review_model: str = os.getenv("DEEP_REVIEW_MODEL_NAME", DEFAULT_DEEP_MODEL)
    # Claims at or above this go to human review.
    review_threshold: float = 100.0
    # Claims at or above this get the deep-review model.
    deep_review_amount: float = 1000.0
    # Claims above this multiple of their category limit get the deep-review model.
    deep_review_limit_multiple: float = 2.0


config = ExpenseAgentConfig()
