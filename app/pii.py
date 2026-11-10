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

"""PII protection for everything the agent persists outside the session.

Two operations:

- ``pseudonymize`` turns an identifier (the submitter's email) into a stable
  keyed hash (HMAC-SHA256). The same email always maps to the same ID, so
  history lookups and log correlation still work, but the ID can't be reversed
  by hashing a list of known addresses without the key.
- ``scrub_text`` removes PII from free text (descriptions, risk summaries, log
  messages) before it is logged or written to Memory Bank.

Scrubbing backends, chosen with ``PII_SCRUBBER``:

- ``regex`` (default): emails, phone numbers, payment card numbers
  (Luhn-checked), US SSNs, and IBANs. No network calls.
- ``dlp``: Cloud Sensitive Data Protection (Cloud DLP) de-identification,
  which detects many more info types. Any DLP failure falls back to regex, so
  scrubbing never blocks a decision and never leaks raw text.

Set ``PII_HASH_KEY`` (from Secret Manager in production). Without it a fixed
development key is used and a warning is logged once.
"""

import functools
import hashlib
import hmac
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

_DEV_KEY = b"local-development-only-pii-key"

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,3})?\b")
_CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
# Not part of a longer digit run (e.g. a card or order number) on either side.
_PHONE = re.compile(
    r"(?<![\w])(?<!\d[ .-])"
    r"(?:\+?\d{1,3}[ .-]?)?(?:\(\d{2,4}\)|\d{2,4})[ .-]?\d{3,4}[ .-]?\d{3,4}"
    r"(?![ .-]?\d)(?!\w)"
)
_DATE_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Info types for the Cloud DLP backend. PERSON_NAME is deliberately left out:
# client and company names in descriptions are needed for review.
DLP_INFO_TYPES = (
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD_NUMBER",
    "US_SOCIAL_SECURITY_NUMBER",
    "IBAN_CODE",
    "PASSPORT",
    "STREET_ADDRESS",
)


# --- Pseudonymization -------------------------------------------------------


@functools.cache
def _hash_key() -> bytes:
    key = os.environ.get("PII_HASH_KEY")
    if not key:
        logger.warning(
            "PII_HASH_KEY is not set; using a development key. Set it from "
            "Secret Manager in deployed environments."
        )
        return _DEV_KEY
    return key.encode()


def pseudonymize(value: str) -> str:
    """Stable, keyed, non-reversible ID for an identifier such as an email."""
    normalized = (value or "").strip().lower().encode()
    digest = hmac.new(_hash_key(), normalized, hashlib.sha256).hexdigest()
    return f"sub_{digest[:16]}"


# --- Regex scrubbing --------------------------------------------------------


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _replace_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return "[CARD]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else match.group(0)


def _replace_phone(match: re.Match[str]) -> str:
    text = match.group(0)
    digits = re.sub(r"\D", "", text)
    # Leave dates, amounts, and short numbers alone.
    if len(digits) < 9 or _DATE_LIKE.match(text.strip()):
        return text
    return "[PHONE]"


def scrub_text_regex(text: str) -> str:
    """Redact common PII patterns locally, with no network calls."""
    text = _EMAIL.sub("[EMAIL]", text)
    text = _SSN.sub("[SSN]", text)
    text = _IBAN.sub("[IBAN]", text)
    text = _CARD_CANDIDATE.sub(_replace_card, text)
    return _PHONE.sub(_replace_phone, text)


# --- Cloud DLP scrubbing ----------------------------------------------------


@functools.cache
def _dlp_client() -> Any:
    from google.cloud import dlp_v2

    return dlp_v2.DlpServiceClient()


def scrub_text_dlp(text: str, client: Any = None) -> str:
    """De-identify ``text`` with Cloud DLP, replacing findings with [INFO_TYPE]."""
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required for Cloud DLP.")
    client = client or _dlp_client()
    response = client.deidentify_content(
        request={
            "parent": f"projects/{project}/locations/global",
            "inspect_config": {
                "info_types": [{"name": name} for name in DLP_INFO_TYPES],
                "min_likelihood": "POSSIBLE",
            },
            "deidentify_config": {
                "info_type_transformations": {
                    "transformations": [
                        {
                            "primitive_transformation": {
                                "replace_with_info_type_config": {}
                            }
                        }
                    ]
                }
            },
            "item": {"value": text},
        }
    )
    return response.item.value


_dlp_failure_logged = False


def scrub_text(text: str) -> str:
    """Remove PII from free text using the configured backend."""
    global _dlp_failure_logged
    if not text:
        return text
    if os.environ.get("PII_SCRUBBER", "regex").lower() == "dlp":
        try:
            # Regex first as defense in depth; DLP then catches the rest.
            return scrub_text_dlp(scrub_text_regex(text))
        except Exception as exc:
            if not _dlp_failure_logged:
                logger.warning("Cloud DLP scrubbing failed (%s); using regex.", exc)
                _dlp_failure_logged = True
    return scrub_text_regex(text)


def scrub_value(value: Any) -> Any:
    """Scrub strings inside a JSON-like value (dicts, lists, scalars)."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {k: scrub_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_value(v) for v in value]
    return value
