"""Mask known credential query parameters before source failures leave the process.

Acquisition reports and retained run-error records share this pure transformation.
It does not sanitize arbitrary headers or bodies; callers still avoid logging them.
"""

from __future__ import annotations

import re

# Credential-bearing query parameters as the providers in use compose them.
_CREDENTIAL_QUERY = re.compile(
    r"([?&](?:appId|api_key|apikey|apiKey|token|access_token|key)=)[^&\s\"']+",
    re.IGNORECASE,
)


def redact_credentials(text: str) -> str:
    """Replace credential query-parameter values with ``<redacted>``."""

    return _CREDENTIAL_QUERY.sub(r"\1<redacted>", text)


__all__ = ["redact_credentials"]
