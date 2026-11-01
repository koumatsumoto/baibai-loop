"""Synthetic OAuth state shared by provider and CLI boundary tests."""

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from baibai_engine.data_collection.providers.tradingview.auth import OAuthState


def state() -> OAuthState:
    return OAuthState(
        client=OAuthClientInformationFull(
            client_id="test-client", redirect_uris=["http://127.0.0.1:8765/callback"]
        ),
        tokens=OAuthToken(
            access_token="old-access", refresh_token="old-refresh", token_type="Bearer"
        ),
        expires_at=1,
    )
