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

"""Company expense policy, kept as data so routing and the reviewer share it.

Limits are per claim, in USD. Edit this table to change policy; no prompt
changes are needed.
"""

from typing import TypedDict


class CategoryPolicy(TypedDict):
    per_claim_limit_usd: float
    receipt_required_over_usd: float
    rules: list[str]


EXPENSE_POLICY: dict[str, CategoryPolicy] = {
    "meals": {
        "per_claim_limit_usd": 75.0,
        "receipt_required_over_usd": 25.0,
        "rules": [
            "Client meals must name the client or company.",
            "Alcohol is not reimbursable.",
        ],
    },
    "travel": {
        "per_claim_limit_usd": 1500.0,
        "receipt_required_over_usd": 0.0,
        "rules": [
            "Economy class only for flights under 6 hours.",
            "Book at least 14 days ahead when possible.",
        ],
    },
    "lodging": {
        "per_claim_limit_usd": 300.0,
        "receipt_required_over_usd": 0.0,
        "rules": ["Limit is per night; itemized folio required."],
    },
    "software": {
        "per_claim_limit_usd": 500.0,
        "receipt_required_over_usd": 0.0,
        "rules": ["Check for an existing company license before buying."],
    },
    "equipment": {
        "per_claim_limit_usd": 1000.0,
        "receipt_required_over_usd": 0.0,
        "rules": ["Items over $500 become company property and must be tagged."],
    },
    "supplies": {
        "per_claim_limit_usd": 150.0,
        "receipt_required_over_usd": 25.0,
        "rules": ["Prefer the company procurement catalog."],
    },
    "other": {
        "per_claim_limit_usd": 100.0,
        "receipt_required_over_usd": 0.0,
        "rules": ["Uncategorized expenses always need a clear business purpose."],
    },
}

CATEGORY_ALIASES: dict[str, str] = {
    "meal": "meals",
    "food": "meals",
    "lunch": "meals",
    "dinner": "meals",
    "breakfast": "meals",
    "flight": "travel",
    "airfare": "travel",
    "train": "travel",
    "taxi": "travel",
    "transport": "travel",
    "hotel": "lodging",
    "accommodation": "lodging",
    "license": "software",
    "subscription": "software",
    "hardware": "equipment",
    "office supplies": "supplies",
}


def normalize_category(category: str) -> str:
    """Map a free-text category onto a policy category ("other" if unknown)."""
    key = (category or "").strip().lower()
    key = CATEGORY_ALIASES.get(key, key)
    return key if key in EXPENSE_POLICY else "other"
