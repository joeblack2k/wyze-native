"""Async Wyze API client used by the Wyze Native integration.

This module ports the relevant cloud API logic from docker-wyze-bridge/wyzecam
to pure-python + aiohttp (no requests, no external deps).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hmac
import json
import logging
import time
import uuid
from hashlib import md5
from typing import Any, Final, TypedDict, cast

import aiohttp


_LOGGER = logging.getLogger(__name__)

AUTH_API: Final = "https://auth-prod.api.wyze.com"
WYZE_API: Final = "https://api.wyzecam.com/app"
CLOUD_API: Final = "https://app-core.cloud.wyze.com/app"

# These values are used to emulate the Wyze iOS app for cloud endpoints.
# Keep them in one place so they can be adjusted if Wyze changes validation.
APP_VERSION: Final = "3.5.5.8"
IOS_VERSION: Final = "17.7.2"
SCALE_USER_AGENT: Final = f"Wyze/{APP_VERSION} (iPhone; iOS {IOS_VERSION}; Scale/3.00)"

# Used for some auth/MFA endpoints (not required for /api/user/login).
WYZE_APP_API_KEY: Final = "WMXHYf79Nr5gIlt3r0r7p9Tcw5bvs6BB4U8O8nGJ"

# Signing material for some v4 cloud endpoints.
APP_KEY: Final[dict[str, str]] = {"9319141212m2ik": "wyze_app_secret_key_132"}
DEFAULT_APP_ID: Final = "9319141212m2ik"

# sc/sv per endpoint; copied from docker-wyze-bridge's vendored wyzecam client.
SC_SV: Final[dict[str, dict[str, str]]] = {
    "default": {
        "sc": "9f275790cab94a72bd206c8876429f3c",
        "sv": "e1fe392906d54888a9b99b88de4162d7",
    },
    "run_action": {
        "sc": "01dd431d098546f9baf5233724fa2ee2",
        "sv": "2c0edc06d4c5465b8c55af207144f0d9",
    },
    "get_device_Info": {
        "sc": "01dd431d098546f9baf5233724fa2ee2",
        "sv": "0bc2c3bedf6c4be688754c9ad42bbf2e",
    },
    "get_event_list": {
        "sc": "9f275790cab94a72bd206c8876429f3c",
        "sv": "782ced6909a44d92a1f70d582bbe88be",
    },
    "set_device_Info": {
        "sc": "01dd431d098546f9baf5233724fa2ee2",
        "sv": "e8e1db44128f4e31a2047a8f5f80b2bd",
    },
}

_DEFAULT_TIMEOUT: Final = aiohttp.ClientTimeout(total=20)


class WyzeApiError(Exception):
    """Base error raised for Wyze API failures."""


class WyzeAuthError(WyzeApiError):
    """Authentication failed or credentials are missing."""


class WyzeAccessTokenError(WyzeApiError):
    """Access token is expired/invalid (Wyze code 2001)."""


class WyzeRateLimitError(WyzeApiError):
    """Wyze API rate limit was reached."""

    def __init__(self, remaining: int, reset_by: int | None, message: str) -> None:
        super().__init__(message)
        self.remaining = remaining
        self.reset_by = reset_by


@dataclass(slots=True)
class WyzeCredential:
    access_token: str | None = None
    refresh_token: str | None = None
    user_id: str | None = None
    phone_id: str | None = None

    # Present on some accounts (MFA).
    mfa_options: list[Any] | None = None
    mfa_details: dict[str, Any] | None = None
    sms_session_id: str | None = None
    email_session_id: str | None = None


class WyzeCameraDevice(TypedDict, total=False):
    mac: str
    nickname: str
    product_model: str
    conn_state: int
    power_switch: int
    device_params: dict[str, Any]
    thumbnail_url: str
    raw: dict[str, Any]
    # Extended info from get_device_Info (populated opportunistically by the coordinator).
    property_list: list[dict[str, Any]]
    properties_by_pid: dict[str, Any]


def hash_password(password: str) -> str:
    """Match Wyze's password hashing: MD5 run 3 times.

    Supports already-hashed passwords prefixed with 'hashed:' or 'md5:'.
    """
    encoded = password.strip()
    for prefix in ("hashed:", "md5:"):
        if encoded.lower().startswith(prefix):
            return encoded[len(prefix) :]
    for _ in range(3):
        encoded = md5(encoded.encode("ascii")).hexdigest()  # nosec - compatibility
    return encoded


def _sort_dict(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def sign_msg(app_id: str, msg: str | dict[str, Any], token: str = "") -> str:
    """Compute Wyze signature2 (HMAC-MD5 over canonical JSON)."""
    secret = APP_KEY.get(app_id, app_id)
    key = md5((token + secret).encode()).hexdigest().encode()  # nosec - compatibility
    if isinstance(msg, dict):
        msg = _sort_dict(msg)
    return hmac.new(key, msg.encode(), md5).hexdigest()  # nosec - compatibility


def _parse_reset_by(reset_by: str) -> int | None:
    ts_format = "%a %b %d %H:%M:%S %Z %Y"
    try:
        return int(datetime.strptime(reset_by, ts_format).timestamp())
    except Exception:
        return None


class WyzeApiClient:
    """Minimal async Wyze cloud API client (login + device list + basic control)."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        email: str,
        password: str,
        key_id: str,
        api_key: str,
        phone_id: str | None = None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self._session = session

        self._email = email.strip()
        self._password = password
        self._key_id = key_id.strip()
        self._api_key = api_key.strip()

        self._phone_id = phone_id or str(uuid.uuid4())
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._user_id = user_id

        self._devices_by_mac: dict[str, WyzeCameraDevice] = {}
        # Cache latest event thumbnail URL (used as a fallback when device thumbnail is empty).
        # Value is (monotonic_ts, url_or_none).
        self._latest_event_thumb_cache: dict[str, tuple[float, str | None]] = {}

    @property
    def phone_id(self) -> str:
        return self._phone_id

    @property
    def access_token(self) -> str | None:
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    @property
    def user_id(self) -> str | None:
        return self._user_id

    def _headers(
        self,
        *,
        phone_id: str | None = None,
        key_id: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, str]:
        """Format headers for Wyze endpoints.

        key_id/api_key are required for AUTH_API /api/user/login.
        """
        if not phone_id:
            return {
                "user-agent": SCALE_USER_AGENT,
                "appversion": APP_VERSION,
                "env": "prod",
            }

        if key_id and api_key:
            # The auth service expects these exact header names.
            return {
                "apikey": api_key,
                "keyid": key_id,
                "user-agent": f"wyze_native/{APP_VERSION}",
            }

        # For some login/MFA endpoints (not used in our main flow yet).
        return {
            "X-API-Key": WYZE_APP_API_KEY,
            "phone-id": phone_id,
            "user-agent": f"wyze_ios_{APP_VERSION}",
        }

    def _payload(self, endpoint: str = "default") -> dict[str, Any]:
        if not self._access_token:
            raise WyzeAuthError("Not logged in (missing access_token).")

        values = SC_SV.get(endpoint, SC_SV["default"])
        return {
            "sc": values["sc"],
            "sv": values["sv"],
            "app_ver": f"com.hualai.WyzeCam___{APP_VERSION}",
            "app_version": APP_VERSION,
            "app_name": "com.hualai.WyzeCam",
            "phone_system_type": 1,
            "ts": int(time.time() * 1000),
            "access_token": self._access_token,
            "phone_id": self._phone_id,
        }

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        data: Any | None = None,
        timeout: aiohttp.ClientTimeout = _DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        async with self._session.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_data,
            data=data,
            timeout=timeout,
        ) as resp:
            # Prefer handling explicit 429 responses rather than pre-emptively failing
            # based on "remaining" headers (which can be low even for a successful flow).
            if resp.status == 429:
                remaining = 0
                remaining_str = resp.headers.get("X-RateLimit-Remaining")
                if remaining_str:
                    try:
                        remaining = int(remaining_str)
                    except ValueError:
                        remaining = 0
                reset_by = _parse_reset_by(resp.headers.get("X-RateLimit-Reset-By", ""))
                retry_after = resp.headers.get("Retry-After", "")
                text = await resp.text()
                raise WyzeRateLimitError(
                    remaining=remaining,
                    reset_by=reset_by,
                    message=(
                        "Wyze API rate limited the request "
                        f"(remaining={remaining}, reset_by={reset_by}, retry_after={retry_after}, status=429): "
                        f"{text[:200]}"
                    ),
                )

            try:
                body = cast(dict[str, Any], await resp.json(content_type=None))
            except Exception as err:
                text = await resp.text()
                raise WyzeApiError(f"Non-JSON response from Wyze: {resp.status} {text}") from err

            code = str(body.get("code", body.get("errorCode", 0)))
            if code == "2001":
                raise WyzeAccessTokenError("Access token expired/invalid (code=2001).")

            if code not in {"1", "0"}:
                msg = body.get("msg", body.get("description", code))
                raise WyzeApiError(f"Wyze API error code={code} msg={msg}")

            # Success: prefer the 'data' envelope when present.
            if isinstance(body.get("data"), dict):
                return cast(dict[str, Any], body["data"])
            return body

    async def _request_authed(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_data: dict[str, Any] | None = None,
        data: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Perform an authenticated request; refresh token once on expiry."""
        try:
            return await self._request_json(
                method, url, headers=headers, json_data=json_data, data=data, params=params
            )
        except WyzeAccessTokenError:
            await self.async_refresh_token()
            return await self._request_json(
                method, url, headers=headers, json_data=json_data, data=data, params=params
            )

    async def login(self) -> WyzeCredential:
        """Login and store access/refresh tokens.

        Raises WyzeAuthError for missing credentials, or WyzeApiError for API failures.
        """
        if not (self._email and self._password and self._key_id and self._api_key):
            raise WyzeAuthError("Missing email/password/key_id/api_key.")

        payload = {"email": self._email, "password": hash_password(self._password)}
        headers = self._headers(
            phone_id=self._phone_id, key_id=self._key_id, api_key=self._api_key
        )
        data = await self._request_json(
            "POST", f"{AUTH_API}/api/user/login", headers=headers, json_data=payload
        )

        cred = WyzeCredential(
            access_token=cast(str | None, data.get("access_token")),
            refresh_token=cast(str | None, data.get("refresh_token")),
            user_id=cast(str | None, data.get("user_id")),
            phone_id=self._phone_id,
            mfa_options=cast(list[Any] | None, data.get("mfa_options")),
            mfa_details=cast(dict[str, Any] | None, data.get("mfa_details")),
            sms_session_id=cast(str | None, data.get("sms_session_id")),
            email_session_id=cast(str | None, data.get("email_session_id")),
        )

        # Persist for subsequent calls.
        self._access_token = cred.access_token
        self._refresh_token = cred.refresh_token
        self._user_id = cred.user_id

        if not self._access_token:
            raise WyzeAuthError(
                "Login did not return an access_token. "
                "If your account has MFA enabled, it is not supported yet."
            )

        return cred

    async def async_refresh_token(self) -> WyzeCredential:
        """Refresh the access token using the stored refresh token."""
        if not self._refresh_token:
            raise WyzeAuthError("Missing refresh_token; cannot refresh.")

        payload = self._payload()
        payload["refresh_token"] = self._refresh_token

        data = await self._request_json(
            "POST", f"{WYZE_API}/user/refresh_token", headers=self._headers(), json_data=payload
        )

        self._access_token = cast(str | None, data.get("access_token", self._access_token))
        self._refresh_token = cast(str | None, data.get("refresh_token", self._refresh_token))

        return WyzeCredential(
            access_token=self._access_token,
            refresh_token=self._refresh_token,
            user_id=self._user_id,
            phone_id=self._phone_id,
        )

    async def get_devices(self) -> list[WyzeCameraDevice]:
        """Return camera devices from Wyze's homepage object list."""
        payload = self._payload()
        data = await self._request_authed(
            "POST",
            f"{WYZE_API}/v2/home_page/get_object_list",
            headers=self._headers(),
            json_data=payload,
        )

        # Wyze has historically used both keys depending on API version.
        devices = data.get("device_list") or data.get("device_info_list") or []
        if not isinstance(devices, list):
            raise WyzeApiError("Unexpected Wyze payload: device_list is not a list.")

        cameras: list[WyzeCameraDevice] = []
        by_mac: dict[str, WyzeCameraDevice] = {}
        for dev in devices:
            if not isinstance(dev, dict):
                continue
            if dev.get("product_type") != "Camera":
                continue

            mac = cast(str | None, dev.get("mac"))
            product_model = cast(str | None, dev.get("product_model"))
            if not mac or not product_model:
                continue

            device_params = dev.get("device_params") or {}
            if not isinstance(device_params, dict):
                device_params = {}

            thumbs = device_params.get("camera_thumbnails") or {}
            if not isinstance(thumbs, dict):
                thumbs = {}
            thumb_val = thumbs.get("thumbnails_url")
            thumbnail_url = thumb_val if isinstance(thumb_val, str) else ""

            conn_state_val = dev.get("conn_state")
            try:
                conn_state = int(conn_state_val) if conn_state_val is not None else 0
            except (TypeError, ValueError):
                conn_state = 0

            # IMPORTANT: power_switch can legitimately be 0, so do not use `or` chaining.
            power_val = device_params.get("power_switch")
            if power_val is None:
                power_val = dev.get("power_switch")
            try:
                power_switch = int(power_val) if power_val is not None else 0
            except (TypeError, ValueError):
                power_switch = 0

            item: WyzeCameraDevice = {
                "mac": mac,
                "nickname": cast(str, dev.get("nickname") or ""),
                "product_model": product_model,
                "conn_state": conn_state,
                "power_switch": power_switch,
                "device_params": cast(dict[str, Any], device_params),
                "thumbnail_url": thumbnail_url,
                "raw": cast(dict[str, Any], dev),
            }
            cameras.append(item)
            by_mac[mac] = item

        self._devices_by_mac = by_mac
        return cameras

    async def set_state(self, mac: str, key: str, value: Any) -> dict[str, Any]:
        """Set a camera state via Wyze's set_device_Info endpoint.

        Note: The required key/value pairs are model/firmware dependent.
        """
        payload = self._payload("set_device_Info")
        payload["device_mac"] = mac
        payload[key] = value

        return await self._request_authed(
            "POST",
            f"{WYZE_API}/device/set_device_Info",
            headers=self._headers(),
            json_data=payload,
        )

    async def get_device_info(self, mac: str, device_model: str) -> dict[str, Any]:
        """Get extended device info (including property_list) for a device."""
        payload = self._payload("get_device_Info")
        payload.update({"device_mac": mac, "device_model": device_model})
        return await self._request_authed(
            "POST",
            f"{WYZE_API}/v2/device/get_device_Info",
            headers=self._headers(),
            json_data=payload,
        )

    async def set_property(
        self, mac: str, device_model: str, pid: str, pvalue: str | int
    ) -> dict[str, Any]:
        """Set a device property (pid/pvalue) via Wyze's set_property endpoint."""
        payload = self._payload()
        payload.update(
            {
                "device_mac": mac,
                "device_model": device_model,
                "pid": str(pid).upper(),
                "pvalue": str(pvalue),
            }
        )
        return await self._request_authed(
            "POST",
            f"{WYZE_API}/v2/device/set_property",
            headers=self._headers(),
            json_data=payload,
        )

    async def get_image_url(self, mac: str) -> str | None:
        """Return the latest thumbnail URL for a given camera MAC."""
        device = self._devices_by_mac.get(mac)
        if device is None:
            # Populate cache if needed.
            for dev in await self.get_devices():
                if dev.get("mac") == mac:
                    device = dev
                    break

        if not device:
            return None

        url = device.get("thumbnail_url")
        if url:
            return url

        # Fall back to raw payload exploration if Wyze changes device_params.
        raw = device.get("raw") or {}
        if isinstance(raw, dict):
            params = raw.get("device_params") or {}
            if isinstance(params, dict):
                thumbs = params.get("camera_thumbnails") or {}
                if isinstance(thumbs, dict):
                    url2 = thumbs.get("thumbnails_url")
                    if isinstance(url2, str) and url2:
                        return url2

        return None

    def _cloud_v4_headers(self, payload: str, *, app_id: str = DEFAULT_APP_ID) -> dict[str, str]:
        """Headers for Wyze Cloud v4 endpoints (signed payload)."""
        if not self._access_token:
            raise WyzeAuthError("Not logged in (missing access_token).")

        # Matches docker-wyze-bridge's sign_payload()
        return {
            "content-type": "application/json",
            "phoneid": self._phone_id,
            "user-agent": f"wyze_ios_{APP_VERSION}",
            "appinfo": f"wyze_ios_{APP_VERSION}",
            "appversion": APP_VERSION,
            "access_token": self._access_token,
            "appid": app_id,
            "env": "prod",
            "signature2": sign_msg(app_id, payload, self._access_token),
        }

    async def _request_cloud_v4(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """Call a Wyze Cloud v4 endpoint that requires signed payload."""
        payload = _sort_dict(params)
        url = f"{CLOUD_API}/v4/device/{endpoint}"
        headers = self._cloud_v4_headers(payload)
        try:
            return await self._request_json("POST", url, headers=headers, data=payload)
        except WyzeAccessTokenError:
            # Refresh token and retry with updated signature.
            await self.async_refresh_token()
            payload = _sort_dict(params)
            headers = self._cloud_v4_headers(payload)
            return await self._request_json("POST", url, headers=headers, data=payload)

    async def get_event_list(
        self,
        device_ids: list[str],
        *,
        begin_time_ms: int | None = None,
        end_time_ms: int | None = None,
        count: int = 20,
        order_by: int = 1,
        last_ts: int = 0,
        event_value_list: list[Any] | None = None,
        event_tag_list: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Get event list for one or more device IDs (camera MACs)."""
        if count < 1 or count > 20:
            raise WyzeApiError(f"count {count} must be between 1 and 20")
        if order_by not in (1, 2):
            raise WyzeApiError("order_by must be 1 or 2")

        # docker-wyze-bridge uses +60s for end_time; keep it for compatibility.
        current_ms = int(time.time() + 60) * 1000
        end_time_ms = end_time_ms or current_ms
        if begin_time_ms is None:
            begin_time_ms = max((last_ts + 1) * 1000, end_time_ms - 1_000_000)

        params = {
            "count": count,
            "order_by": order_by,
            "begin_time": begin_time_ms,
            "end_time": end_time_ms,
            "nonce": str(int(time.time() * 1000)),
            "device_id_list": list(set(device_ids)),
            "event_value_list": event_value_list or [],
            "event_tag_list": event_tag_list or [],
        }

        data = await self._request_cloud_v4("get_event_list", params)
        events = data.get("event_list") or []
        if not isinstance(events, list):
            return []
        return cast(list[dict[str, Any]], [e for e in events if isinstance(e, dict)])

    async def get_latest_event_image_url(self, mac: str, *, cache_seconds: int = 60) -> str | None:
        """Return a best-effort image URL from the latest event for a camera."""
        now_mono = time.monotonic()
        cached = self._latest_event_thumb_cache.get(mac)
        if cached and (now_mono - cached[0]) < cache_seconds:
            return cached[1]

        def _pick_url(events: list[dict[str, Any]]) -> str | None:
            if not events:
                return None

            # Prefer the most recent event_ts (ms); fall back to the first item.
            best: dict[str, Any] | None = None
            best_ts = -1
            for e in events:
                ts = e.get("event_ts")
                try:
                    ts_i = int(ts)
                except (TypeError, ValueError):
                    ts_i = -1
                if ts_i > best_ts:
                    best = e
                    best_ts = ts_i
            if best is None:
                best = events[0]

            files = best.get("file_list") or []
            if isinstance(files, list):
                # Prefer IMAGE files (type==1) to avoid returning a video URL.
                for f in files:
                    if not isinstance(f, dict):
                        continue
                    try:
                        if int(f.get("type")) == 1 and f.get("url"):
                            return cast(str, f["url"])
                    except (TypeError, ValueError):
                        continue
                for f in files:
                    if isinstance(f, dict) and f.get("url"):
                        return cast(str, f["url"])

            thumb = best.get("thumbnail") or best.get("thumbnail_url")
            if isinstance(thumb, str) and thumb:
                return thumb
            return None

        url: str | None
        try:
            events = await self.get_event_list([mac], count=20, order_by=1, last_ts=0)
            url = _pick_url(events)
            if not url:
                # Widen the window (up to 24h) if no events in the short window.
                end_ms = int(time.time() + 60) * 1000
                begin_ms = end_ms - 86_400_000
                events = await self.get_event_list(
                    [mac],
                    begin_time_ms=begin_ms,
                    end_time_ms=end_ms,
                    count=20,
                    order_by=1,
                    last_ts=0,
                )
                url = _pick_url(events)
            if not url:
                # Last resort: ask for the latest events across the full history window.
                # The API is still limited by `count`, so the response stays small.
                end_ms = int(time.time() + 60) * 1000
                events = await self.get_event_list(
                    [mac],
                    begin_time_ms=0,
                    end_time_ms=end_ms,
                    count=20,
                    order_by=1,
                    last_ts=0,
                )
                url = _pick_url(events)
        except WyzeApiError:
            url = None

        self._latest_event_thumb_cache[mac] = (now_mono, url)
        return url
