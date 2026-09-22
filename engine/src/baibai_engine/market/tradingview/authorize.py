"""Local interactive OAuth bootstrap using the official SDK and a loopback callback."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

from .auth import AuthenticationError, OAuthState, write_local_state
from .client import SERVER_URL, _identify_request


class _BootstrapStorage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.client: OAuthClientInformationFull | None = None
        self.tokens: OAuthToken | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client = client_info

    async def set_tokens(self, tokens: OAuthToken) -> None:
        if self.client is None:
            raise AuthenticationError("OAuth client registration was not completed")
        write_local_state(
            self.path,
            OAuthState(
                client=self.client, tokens=tokens, expires_at=time.time() + (tokens.expires_in or 0)
            ),
        )
        self.tokens = tokens


async def authorize(path: Path) -> None:
    result: asyncio.Future[AuthorizationCodeResult] = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            pieces = line.decode("ascii").split()
            callback = urlsplit(pieces[1]) if len(pieces) == 3 else None
            params = parse_qs(callback.query) if callback else {}
            if callback and callback.path == "/callback" and "code" in params and not result.done():
                result.set_result(
                    AuthorizationCodeResult(
                        code=params["code"][0],
                        state=params.get("state", [None])[0],
                        iss=params.get("iss", [None])[0],
                    )
                )
                body = b"Authorization received. Return to the terminal for the result."
            else:
                body = b"OAuth callback listener is ready."
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def redirect(url: str) -> None:
        print(
            "Open this TradingView authorization URL (valid for this running process):", flush=True
        )
        print(url, flush=True)

    async def callback() -> AuthorizationCodeResult:
        return await asyncio.wait_for(result, timeout=3600)

    # Start listening before the browser URL is displayed.
    server = await asyncio.start_server(handle, "127.0.0.1", 8765)
    auth = OAuthClientProvider(
        server_url=SERVER_URL,
        client_metadata=OAuthClientMetadata(
            client_name="Baibai Loop",
            redirect_uris=["http://127.0.0.1:8765/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",  # nosec B106 - OAuth public-client auth method
        ),
        storage=_BootstrapStorage(path),
        redirect_handler=redirect,
        callback_handler=callback,
    )
    async with (
        server,
        httpx2.AsyncClient(
            auth=auth, timeout=60, event_hooks={"request": [_identify_request]}
        ) as http,
        streamable_http_client(SERVER_URL, http_client=http) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
    print(f"OAuth state saved privately: {path}")
