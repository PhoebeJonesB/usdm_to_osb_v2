"""Thin wrapper around the OpenStudyBuilder REST API."""

import logging
import requests
from typing import Any

from .config import Config, TokenManager

logger = logging.getLogger(__name__)


class APIClient:
    """Low-level HTTP helper that auto-refreshes auth on every call."""

    def __init__(self, config: Config, token_manager: TokenManager):
        self.base = config.api_base_url
        self.tm = token_manager

    def _url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def get(self, path: str, params: dict | None = None, timeout: int = 60) -> requests.Response:
        return requests.get(self._url(path), headers=self.tm.get_headers(), params=params, timeout=timeout)

    def post(self, path: str, json: Any = None, params: dict | None = None, timeout: int = 60) -> requests.Response:
        return requests.post(self._url(path), headers=self.tm.get_headers(), json=json, params=params, timeout=timeout)

    def patch(self, path: str, json: Any = None, timeout: int = 60) -> requests.Response:
        return requests.patch(self._url(path), headers=self.tm.get_headers(), json=json, timeout=timeout)

    def delete(self, path: str, timeout: int = 60) -> requests.Response:
        return requests.delete(self._url(path), headers=self.tm.get_headers(), timeout=timeout)

    def get_all_pages(self, path: str, page_size: int = 1000, extra_params: dict | None = None) -> list[dict]:
        """Paginate through a list endpoint until exhausted."""
        all_items: list[dict] = []
        page = 1
        while True:
            params = {"page_number": page, "page_size": page_size}
            if extra_params:
                params.update(extra_params)
            resp = self.get(path, params=params)
            if resp.status_code != 200:
                logger.warning("GET %s page %d returned %d", path, page, resp.status_code)
                break
            items = resp.json().get("items", [])
            if not items:
                break
            all_items.extend(items)
            if len(items) < page_size:
                break
            page += 1
        return all_items
