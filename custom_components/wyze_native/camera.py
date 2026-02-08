"""Camera entities for Wyze Native (thumbnail/snapshot based)."""

from __future__ import annotations

import logging
from pathlib import Path
import time
from typing import Any

import aiohttp

from homeassistant.components.camera import Camera
try:
    from homeassistant.components.camera import CameraEntityFeature
except ImportError:  # pragma: no cover
    CameraEntityFeature = None  # type: ignore[assignment]
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WyzeNativeDataUpdateCoordinator
from .entity import WyzeNativeEntity
from .wyze_api import SCALE_USER_AGENT, WyzeApiClient, WyzeApiError
from .const import CONF_STREAM_URL_TEMPLATE, CONF_USE_PLACEHOLDER_IMAGE

from homeassistant.util import slugify


_LOGGER = logging.getLogger(__name__)

_WYZE_WEB_ORIGIN = "https://my.wyze.com"
_WYZE_THUMB_HEADERS = {
    "Origin": _WYZE_WEB_ORIGIN,
    "Referer": f"{_WYZE_WEB_ORIGIN}/",
    "User-Agent": SCALE_USER_AGENT,
}

_IMAGE_CACHE_SECONDS = 10


def _looks_like_image(data: bytes) -> bool:
    if data.startswith(b"\xff\xd8\xff"):  # JPEG
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):  # PNG
        return True
    if data.startswith((b"GIF87a", b"GIF89a")):  # GIF
        return True
    # WebP: "RIFF....WEBP"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return True
    return False


def _guess_content_type(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):  # JPEG
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):  # PNG
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):  # GIF
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


_PLACEHOLDER_BYTES: bytes | None = None
_PLACEHOLDER_CONTENT_TYPE: str | None = None


def _load_placeholder_image() -> tuple[bytes, str] | None:
    """Load a small placeholder image shipped with the integration."""
    global _PLACEHOLDER_BYTES, _PLACEHOLDER_CONTENT_TYPE  # noqa: PLW0603
    if _PLACEHOLDER_BYTES is not None and _PLACEHOLDER_CONTENT_TYPE is not None:
        return _PLACEHOLDER_BYTES, _PLACEHOLDER_CONTENT_TYPE

    # Prefer a JPEG placeholder so `camera.snapshot` outputs match common `.jpg`
    # filenames users pick. Fall back to the integration icon (PNG).
    for fname in ("placeholder.jpg", "icon.png"):
        path = Path(__file__).with_name(fname)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed reading placeholder image %s: %s", path, err)
            continue

        if not data or not _looks_like_image(data):
            continue

        _PLACEHOLDER_BYTES = data
        _PLACEHOLDER_CONTENT_TYPE = _guess_content_type(data)
        return _PLACEHOLDER_BYTES, _PLACEHOLDER_CONTENT_TYPE

    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wyze camera entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: WyzeNativeDataUpdateCoordinator = data["coordinator"]
    api: WyzeApiClient = data["api"]
    session = async_get_clientsession(hass)

    async_add_entities(
        [WyzeNativeCamera(coordinator, api, session, mac) for mac in coordinator.data]
    )


class WyzeNativeCamera(WyzeNativeEntity, Camera):
    """Wyze camera that serves the latest Wyze thumbnail URL."""

    _attr_name = "Snapshot"
    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        coordinator: WyzeNativeDataUpdateCoordinator,
        api: WyzeApiClient,
        session: aiohttp.ClientSession,
        mac: str,
    ) -> None:
        WyzeNativeEntity.__init__(self, coordinator, mac)
        Camera.__init__(self)
        self._api = api
        self._session = session
        self._attr_unique_id = f"{mac}_snapshot"
        self._last_image: bytes | None = None
        self._last_image_mono: float = 0.0
        self._last_error: str | None = None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return bytes of camera image."""
        now_mono = time.monotonic()
        if (
            self._last_image is not None
            and (now_mono - self._last_image_mono) < _IMAGE_CACHE_SECONDS
            and self._last_image
        ):
            return self._last_image

        dev = self.coordinator.data.get(self._mac)
        if not dev:
            self._last_error = "device_not_found"
            return None

        async def _fetch(u: str) -> bytes | None:
            async with self._session.get(
                u,
                headers=_WYZE_THUMB_HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (401, 403):
                    _LOGGER.debug("Thumbnail fetch forbidden for %s (status=%s)", self._mac, resp.status)
                    self._last_error = f"thumb_http_{resp.status}"
                    return None
                resp.raise_for_status()
                data = await resp.read()
                if not data:
                    self._last_error = "thumb_empty_body"
                    return None
                if not _looks_like_image(data):
                    _LOGGER.debug(
                        "Thumbnail fetch returned non-image for %s (ct=%s, len=%s)",
                        self._mac,
                        resp.headers.get("Content-Type"),
                        len(data),
                    )
                    self._last_error = "thumb_not_image"
                    return None
                self._attr_content_type = resp.headers.get("Content-Type") or _guess_content_type(data)
                return data

        def _cache(img: bytes) -> bytes:
            self._last_image = img
            self._last_image_mono = time.monotonic()
            self._attr_content_type = _guess_content_type(img)
            self._last_error = None
            return img

        try:
            url = dev.get("thumbnail_url")
            if url:
                img = await _fetch(url)
                if img:
                    return _cache(img)

                # Refresh device data once and retry (URL can be stale/empty).
                await self.coordinator.async_request_refresh()
                dev = self.coordinator.data.get(self._mac) or {}
                new_url = dev.get("thumbnail_url") or url
                if new_url:
                    img = await _fetch(new_url)
                    if img:
                        return _cache(img)

            # Fallback: query the latest event list and use its image URL.
            event_url = await self._api.get_latest_event_image_url(self._mac)
            if event_url:
                img = await _fetch(event_url)
                if img:
                    return _cache(img)
            else:
                _LOGGER.debug("No thumbnail URL available for %s (device has no recent events)", self._mac)
                self._last_error = "no_thumbnail_url"
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Failed to fetch thumbnail for %s: %s", self._mac, err)
            self._last_error = f"thumb_error_{type(err).__name__}"
        except WyzeApiError as err:
            _LOGGER.debug("Failed to fetch event URL for %s: %s", self._mac, err)
            self._last_error = f"wyze_api_error_{type(err).__name__}"

        # If we previously fetched an image, keep serving it even if the current
        # Wyze thumbnail URL is missing/expired. This avoids empty snapshots.
        if self._last_image:
            return self._last_image

        # Optional: return a placeholder so camera.snapshot doesn't create an empty file.
        use_placeholder = bool(
            self.coordinator.entry.options.get(CONF_USE_PLACEHOLDER_IMAGE, True)
        )
        if use_placeholder:
            placeholder = _load_placeholder_image()
            if placeholder is not None:
                data, content_type = placeholder
                self._attr_content_type = content_type
                return data
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        dev = self.coordinator.data.get(self._mac) or {}
        params = dev.get("device_params") or {}
        thumbs = params.get("camera_thumbnails") or {}
        if not isinstance(thumbs, dict):
            thumbs = {}
        return {
            "conn_state": dev.get("conn_state"),
            "power_switch": dev.get("power_switch"),
            "thumbnail_url": dev.get("thumbnail_url"),
            "thumbnails_ts": thumbs.get("thumbnails_ts"),
            "last_error": self._last_error,
        }

    async def async_stream_source(self) -> str | None:
        """Return an optional stream source URL.

        Wyze Native does not implement native streaming (P2P/TUTK/WebRTC) to keep this
        integration pure-Python and low-risk. If you have an external RTSP/WebRTC
        gateway (for example go2rtc) you can provide a template in the config entry
        options and Home Assistant can use that for streaming.

        Supported placeholders:
        - {mac}: device MAC
        - {nickname}: original Wyze nickname
        - {name}: slugified nickname (safe for URLs)
        - {model}: product model
        - {ip}: local IP address (best-effort, from Wyze cloud device_params)
        """
        template = (self.coordinator.entry.options.get(CONF_STREAM_URL_TEMPLATE) or "").strip()
        if not template:
            return None

        dev = self.coordinator.data.get(self._mac) or {}
        params = dev.get("device_params") or {}
        nickname = str(dev.get("nickname") or self._mac)
        model = str(dev.get("product_model") or "")
        name = slugify(nickname)
        ip = ""
        if isinstance(params, dict):
            ip = str(params.get("ip") or "")
        try:
            return template.format(
                mac=self._mac,
                nickname=nickname,
                name=name,
                model=model,
                ip=ip,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Invalid stream_url_template for %s: %s", self._mac, err)
            return None

    @property
    def supported_features(self):
        features = super().supported_features
        template = (self.coordinator.entry.options.get(CONF_STREAM_URL_TEMPLATE) or "").strip()
        if template and CameraEntityFeature is not None:
            features |= CameraEntityFeature.STREAM
        return features
