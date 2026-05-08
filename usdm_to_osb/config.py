"""Configuration and OAuth token management."""

import time
import logging
import requests

logger = logging.getLogger(__name__)


class Config:
    """Holds all connection settings. Edit or override via env vars / CLI args."""

    def __init__(
        self,
        api_base_url: str,
        idp_url: str = "",
        client_id: str = "",
        client_secret: str = "",
        username: str = "",
        password: str = "",
        project_number: str = "999",
        no_auth: bool = False,
    ):
        self.api_base_url = api_base_url.rstrip("/")
        self.idp_url = idp_url.rstrip("/") if idp_url else ""
        self.client_id = client_id
        self.client_secret = client_secret
        self.username = username
        self.password = password
        self.project_number = project_number
        self.no_auth = no_auth


class TokenManager:
    """Manages OAuth2 tokens using the password grant flow with auto-refresh."""

    def __init__(self, config: Config):
        self.no_auth = config.no_auth
        self.token_url = f"{config.idp_url}/o/token/" if config.idp_url else ""
        self.client_id = config.client_id
        self.client_secret = config.client_secret
        self.username = config.username
        self.password = config.password
        self._access_token = None
        self._refresh_token = None
        self._expires_at = 0.0

    def _request_token(self, data: dict, context: str) -> bool:
        try:
            resp = requests.post(self.token_url, data=data, timeout=30)
            if resp.status_code == 200:
                result = resp.json()
                self._access_token = result["access_token"]
                expires_in = result.get("expires_in", 300)
                self._expires_at = time.time() + max(expires_in - 60, 10)
                if result.get("refresh_token"):
                    self._refresh_token = result["refresh_token"]
                logger.info("Token %s OK (expires in %ds)", context, expires_in)
                return True
            else:
                logger.error("Token %s failed (%d): %s", context, resp.status_code, resp.text)
                return False
        except Exception as exc:
            logger.error("Token %s exception: %s", context, exc)
            return False

    def _authenticate(self) -> bool:
        return self._request_token(
            {
                "grant_type": "password",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "username": self.username,
                "password": self.password,
            },
            "password-grant",
        )

    def _refresh(self) -> bool:
        if not self._refresh_token:
            return False
        return self._request_token(
            {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self._refresh_token,
            },
            "refresh",
        )

    def get_token(self) -> str:
        if self.no_auth:
            return ""
        if self._access_token and time.time() < self._expires_at:
            return self._access_token
        if self._refresh_token and self._refresh():
            return self._access_token
        if self._authenticate():
            return self._access_token
        raise RuntimeError("Unable to obtain access token")

    def get_headers(self) -> dict:
        if self.no_auth:
            return {"Content-Type": "application/json"}
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Content-Type": "application/json",
        }
