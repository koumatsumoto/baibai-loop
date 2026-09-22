"""Read-only TradingView MCP adapter. OAuth itself belongs to the official SDK."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata, OAuthMetadata

from .auth import AuthenticationError, CredentialStorage

SERVER_URL = "https://mcp.tradingview.com/mcp"
METADATA_URL = "https://www.tradingview.com/.well-known/oauth-authorization-server"
BATCH_TOOL = "mcp-tv-get-symbol-data-batch"


async def _no_redirect(url: str) -> None:
    raise AuthenticationError("Interactive TradingView login is required")


async def _no_callback() -> AuthorizationCodeResult:
    raise AuthenticationError("Interactive TradingView login is required")


async def _identify_request(request: httpx2.Request) -> None:
    # SDK OAuth discovery creates bare requests. The provider rejects a missing UA.
    request.headers.setdefault("User-Agent", "baibai-loop-tradingview/1.0")


@asynccontextmanager
async def connect(storage: CredentialStorage) -> AsyncIterator[ClientSession]:
    auth = OAuthClientProvider(
        server_url=SERVER_URL,
        client_metadata=OAuthClientMetadata(
            redirect_uris=storage.state.client.redirect_uris,
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method=storage.state.client.token_endpoint_auth_method or "none",
        ),
        storage=storage,
        redirect_handler=_no_redirect,
        callback_handler=_no_callback,
    )
    # The SDK restores tokens but not their absolute expiration or AS metadata.
    # Discover the official endpoint before its early refresh path can run.
    async with httpx2.AsyncClient(timeout=60) as discovery:
        response = await discovery.get(METADATA_URL)
        response.raise_for_status()
        metadata = OAuthMetadata.model_validate(response.json())
    if str(metadata.issuer).rstrip("/") != "https://www.tradingview.com":
        raise AuthenticationError("Unexpected TradingView authorization issuer")
    if str(metadata.token_endpoint) != "https://www.tradingview.com/mcp/oauth/token":
        raise AuthenticationError("Unexpected TradingView token endpoint")
    auth.context.oauth_metadata = metadata
    auth.context.token_expiry_time = storage.state.expires_at
    async with (
        httpx2.AsyncClient(
            auth=auth, timeout=60, event_hooks={"request": [_identify_request]}
        ) as http,
        streamable_http_client(SERVER_URL, http_client=http) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        yield session


async def fetch_batch(
    session: ClientSession, symbols: list[str], columns: list[str]
) -> dict[str, Any]:
    if not 1 <= len(symbols) <= 50 or len(symbols) != len(set(symbols)):
        raise ValueError("TradingView batch requires 1..50 unique symbols")
    result = await session.call_tool(BATCH_TOOL, {"symbols": symbols, "columns": columns})
    if result.is_error:
        raise RuntimeError("TradingView MCP tool failed")
    payload = result.structured_content
    if payload is None:
        texts = [item.text for item in result.content if item.type == "text"]
        if len(texts) != 1:
            raise RuntimeError("Malformed TradingView MCP response")
        payload = json.loads(texts[0])
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise RuntimeError("TradingView batch failed")
    return payload
