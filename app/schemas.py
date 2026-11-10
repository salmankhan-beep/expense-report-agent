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

"""Typed contracts for the reviewer's tools.

Every tool returns a model derived from ``ToolResult``. On success ``status``
is ``"ok"`` and the payload fields are set. On failure ``status`` is
``"error"``, ``error`` says what went wrong, and ``recovery`` tells the model
exactly what to do next, so a failed call never leaves it guessing.
"""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_EMAIL = re.compile(r"^[^@\s|=]+@[^@\s|=]+\.[^@\s|=]+$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --- Inputs ----------------------------------------------------------------


class SubmitterRef(BaseModel):
    """Identifies whose history to look up."""

    submitter: str = Field(description="The submitter's email address.")

    @field_validator("submitter")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not _EMAIL.match(value):
            raise ValueError("submitter must be an email address like name@company.com")
        return value


class ClaimKey(SubmitterRef):
    """The fields that identify a claim for duplicate detection."""

    amount: float = Field(gt=0, description="Claim amount in USD.")
    date: str = Field(description="Expense date, YYYY-MM-DD.")

    @field_validator("date")
    @classmethod
    def _valid_date(cls, value: str) -> str:
        value = value.strip()
        if not _DATE.match(value):
            raise ValueError("date must be formatted YYYY-MM-DD")
        return value


# --- Outputs ---------------------------------------------------------------


class ToolResult(BaseModel):
    """Common envelope: success flag plus error and recovery guidance."""

    status: Literal["ok", "error"] = "ok"
    error: str | None = Field(default=None, description="What went wrong.")
    recovery: str | None = Field(
        default=None, description="What the agent should do next after an error."
    )


class PolicyLookupResult(ToolResult):
    category: str | None = Field(default=None, description="Matched policy category.")
    matched_exactly: bool | None = Field(
        default=None,
        description="False when the submitted category was unknown and 'other' was used.",
    )
    per_claim_limit_usd: float | None = None
    receipt_required_over_usd: float | None = None
    rules: list[str] = Field(default_factory=list)


class ExpenseRecord(BaseModel):
    """One remembered, decided claim."""

    submitter: str
    amount: float
    category: str | None = None
    date: str
    status: str | None = None
    description: str | None = None


class SpendingHistoryResult(ToolResult):
    submitter: str | None = None
    claims_recalled: int = 0
    approved_total_usd: float = 0.0
    rejected_count: int = 0
    recent_claims: list[ExpenseRecord] = Field(default_factory=list)
    note: str | None = Field(
        default=None,
        description="Caveat about coverage: recall returns the most relevant claims, not a full ledger.",
    )


class DuplicateCheckResult(ToolResult):
    is_duplicate: bool | None = None
    matching_claims: list[ExpenseRecord] = Field(default_factory=list)


class AlertResult(ToolResult):
    alert_emitted: bool = False
    submitter: str | None = None
    amount: float | None = None
