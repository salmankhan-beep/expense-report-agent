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

"""Unit tests for choosing the Gemini backend (AI Studio vs Google Cloud)."""

import pytest

from app.config import use_google_cloud

ENV_VARS = (
    "GOOGLE_GENAI_USE_ENTERPRISE",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, True),  # no key: Google Cloud with ADC
        ({"GEMINI_API_KEY": "k"}, False),  # AI Studio quick start
        ({"GOOGLE_API_KEY": "k"}, False),
        ({"GEMINI_API_KEY": "k", "GOOGLE_GENAI_USE_ENTERPRISE": "true"}, True),
        ({"GOOGLE_GENAI_USE_ENTERPRISE": "FALSE"}, False),
        ({"GOOGLE_GENAI_USE_VERTEXAI": "True"}, True),
        (
            {
                "GOOGLE_GENAI_USE_ENTERPRISE": "false",
                "GOOGLE_GENAI_USE_VERTEXAI": "true",
            },
            False,
        ),
    ],
)
def test_use_google_cloud(monkeypatch, env, expected) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert use_google_cloud() is expected
