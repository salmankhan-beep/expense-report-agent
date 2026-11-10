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

"""Unit and runnability tests for the ambient expense agent."""

from google.adk import Workflow

from app.agent import (
    auto_approve,
    parse_expense_email,
    request_approval,
    review_agent,
    root_agent,
    route_by_amount,
)


def test_agent_runnability() -> None:
    """Verify agent.py imports and defines the ADK 2.0 Workflow and required nodes."""
    assert root_agent is not None
    assert isinstance(root_agent, Workflow)
    assert review_agent.name == "review_agent"
    assert callable(auto_approve)
    assert callable(request_approval)
    assert callable(route_by_amount)


def test_parse_and_auto_approve_unit() -> None:
    """Verify deterministic parsing and auto_approve node output under $100."""
    parsed_event = parse_expense_email(
        '{"amount": 45.0, "submitter": "bob@company.com", "category": "meals", "description": "Lunch", "date": "2026-04-12"}'
    )
    assert parsed_event.output["amount"] == 45.0
    approved_event = auto_approve(parsed_event.output)
    assert approved_event.output["status"] == "approved"
