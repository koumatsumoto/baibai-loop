"""Mask credentials in text that leaves the process.

Provider errors quote the fully composed request URL, so an API key travels in
the exception text into the run record and from there into published serving
JSON. Masking lives here because both the writer (the indicator service) and the
reader (the query-only facade) need it, and the reader may not import provider
modules.
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
