"""Minimal Plex Media Server API client (JSON over HTTP, token auth)."""
from __future__ import annotations

from typing import Any

import httpx

from . import __version__


class PlexError(RuntimeError):
    pass


class PlexClient:
    def __init__(self, base_url: str, token: str, verify_ssl: bool = True):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._http = httpx.Client(
            base_url=self.base_url or "http://plex.invalid",
            timeout=httpx.Timeout(60, connect=10),
            verify=verify_ssl,
            headers={
                "Accept": "application/json",
                "X-Plex-Token": token,
                "X-Plex-Product": "Quotarr",
                "X-Plex-Version": __version__,
                "X-Plex-Client-Identifier": "quotarr-server",
            },
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _request(self, path: str, params: dict | None = None) -> httpx.Response:
        if not self.configured:
            raise PlexError("PLEX_URL and PLEX_TOKEN are not set.")
        try:
            resp = self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise PlexError(f"Could not reach Plex at {self.base_url}: {exc}") from exc
        if resp.status_code == 401:
            raise PlexError("Plex rejected the token (HTTP 401). Check PLEX_TOKEN.")
        if resp.status_code >= 400:
            raise PlexError(f"Plex returned HTTP {resp.status_code} for {path}")
        return resp

    def _container(self, path: str, params: dict | None = None) -> dict[str, Any]:
        return self._request(path, params).json().get("MediaContainer", {})

    def server_info(self) -> dict[str, Any]:
        mc = self._container("/")
        return {"name": mc.get("friendlyName"), "version": mc.get("version")}

    def show_libraries(self) -> list[dict]:
        dirs = self._container("/library/sections").get("Directory", [])
        return [d for d in dirs if d.get("type") == "show"]

    def shows(self, section_key: str) -> list[dict]:
        return self._container(f"/library/sections/{section_key}/all").get("Metadata", [])

    def episodes(self, show_key: str) -> list[dict]:
        return self._container(f"/library/metadata/{show_key}/allLeaves").get("Metadata", [])

    def metadata(self, key: str) -> dict | None:
        items = self._container(f"/library/metadata/{key}").get("Metadata", [])
        return items[0] if items else None

    def fetch(self, path: str, params: dict | None = None) -> bytes:
        return self._request(path, params).content

    def poster(self, thumb: str, width: int = 300, height: int = 450) -> bytes:
        return self.fetch(
            "/photo/:/transcode",
            {"url": thumb, "width": width, "height": height, "minSize": 1, "upscale": 1},
        )

    def media_url(self, part_key: str) -> str:
        return self.base_url + part_key
