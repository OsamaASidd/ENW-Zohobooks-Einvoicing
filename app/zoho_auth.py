"""Zoho OAuth2 token management (self-client / authorization-code flow).

Flow:
1. A "grant token" (authorization code) is exchanged ONCE for an access
   token + a long-lived refresh token. Grant tokens expire within minutes
   and can only be used once.
2. The refresh token is persisted to .env and used from then on to mint
   short-lived (~1h) access tokens on demand.
"""

import time

import httpx
from dotenv import set_key


class ZohoAuthError(RuntimeError):
    pass


class ZohoAuth:
    def __init__(self, client_id, client_secret, accounts_base, env_path, refresh_token=None, ssl_context=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.accounts_base = accounts_base.rstrip("/")
        self.env_path = env_path
        self.refresh_token = refresh_token or None
        self._access_token = None
        self._expires_at = 0
        self._client = httpx.AsyncClient(timeout=30.0, verify=ssl_context if ssl_context is not None else True)

    async def aclose(self):
        await self._client.aclose()

    async def exchange_grant_token(self, grant_token: str) -> dict:
        resp = await self._client.post(
            f"{self.accounts_base}/oauth/v2/token",
            params={
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": grant_token,
            },
        )
        data = resp.json()
        if "error" in data or "refresh_token" not in data:
            raise ZohoAuthError(
                f"Grant token exchange failed: {data}. Grant tokens expire within "
                "minutes and are single-use - generate a fresh one in the Zoho API "
                "Console (Self Client) and try again."
            )

        self.refresh_token = data["refresh_token"]
        self._access_token = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 3600) - 60

        set_key(str(self.env_path), "ZOHO_REFRESH_TOKEN", self.refresh_token)
        set_key(str(self.env_path), "ZOHO_GRANT_TOKEN", "")
        return data

    async def _refresh_access_token(self) -> str:
        if not self.refresh_token:
            raise ZohoAuthError(
                "No refresh token available yet. Generate a fresh grant token in "
                "the Zoho API Console and POST it to /auth/exchange, or set "
                "ZOHO_GRANT_TOKEN in .env and restart the server."
            )
        resp = await self._client.post(
            f"{self.accounts_base}/oauth/v2/token",
            params={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
            },
        )
        data = resp.json()
        if "error" in data or "access_token" not in data:
            raise ZohoAuthError(f"Access token refresh failed: {data}")

        self._access_token = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 3600) - 60
        return self._access_token

    async def get_access_token(self) -> str:
        if self._access_token and time.time() < self._expires_at:
            return self._access_token
        return await self._refresh_access_token()

    def status(self) -> dict:
        return {
            "has_refresh_token": bool(self.refresh_token),
            "access_token_cached": bool(self._access_token),
            "access_token_expires_in_seconds": (
                max(0, int(self._expires_at - time.time())) if self._access_token else 0
            ),
        }
