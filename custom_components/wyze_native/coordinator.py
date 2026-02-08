"""Data coordinator for Wyze Native."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .wyze_api import (
    WyzeApiClient,
    WyzeApiError,
    WyzeAuthError,
    WyzeCameraDevice,
    WyzeRateLimitError,
)


_LOGGER = logging.getLogger(__name__)

# Wyze has fairly strict rate limits. `get_device_Info` (property_list) is useful for
# some settings but is more expensive than the homepage device list, so poll it
# much less frequently.
_PROPERTY_REFRESH_INTERVAL = timedelta(minutes=30)


def _flatten_property_list(prop_list: Any) -> dict[str, Any]:
    """Convert Wyze property_list items into PID->value mapping."""
    if not isinstance(prop_list, list):
        return {}
    out: dict[str, Any] = {}
    for item in prop_list:
        if not isinstance(item, dict):
            continue
        pid = item.get("pid")
        if not pid:
            continue
        out[str(pid).upper()] = item.get("value", item.get("pvalue"))
    return out


class WyzeNativeDataUpdateCoordinator(
    DataUpdateCoordinator[dict[str, WyzeCameraDevice]]
):
    """Update coordinator for Wyze camera devices."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: WyzeApiClient,
        *,
        update_interval: timedelta = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
            always_update=False,
        )
        self.entry = entry
        self.api = api
        self._default_update_interval = update_interval

        # Seed data so entities can be created before first refresh in edge-cases.
        self.data = {}
        self._property_last_refresh_mono: float = 0.0
        self._property_disabled_until_mono: float = 0.0
        # Cache PID-backed properties across regular get_devices polling (which does not include property_list).
        self._properties_by_pid_by_mac: dict[str, dict[str, Any]] = {}
        self._property_list_by_mac: dict[str, list[dict[str, Any]]] = {}

    async def _async_update_data(self) -> dict[str, WyzeCameraDevice]:
        try:
            devices = await self.api.get_devices()
        except WyzeRateLimitError as err:
            # Slow down polling temporarily when Wyze returns 429.
            wait_s = 300
            if err.reset_by:
                wait_s = max(int(err.reset_by) - int(time.time()), 60)
            self.update_interval = timedelta(seconds=wait_s)
            raise UpdateFailed(f"Wyze rate limited: {err}") from err
        except WyzeAuthError as err:
            raise UpdateFailed(f"Wyze auth failed: {err}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"Wyze request failed: {err}") from err
        except WyzeApiError as err:
            raise UpdateFailed(f"Wyze API error: {err}") from err
        except Exception as err:  # noqa: BLE001 - surface unexpected coordinator errors
            raise UpdateFailed(f"Unexpected error: {err}") from err

        # Restore normal polling after a successful update.
        if self.update_interval != self._default_update_interval:
            self.update_interval = self._default_update_interval

        # Persist refreshed tokens if needed.
        self._async_update_entry_tokens()

        # Apply cached property_list/properties_by_pid so entities remain stable between refreshes.
        for dev in devices:
            mac = str(dev.get("mac") or "")
            if not mac:
                continue
            if mac in self._properties_by_pid_by_mac:
                dev["properties_by_pid"] = dict(self._properties_by_pid_by_mac[mac])
            if mac in self._property_list_by_mac:
                dev["property_list"] = list(self._property_list_by_mac[mac])

        # Best-effort refresh of device properties (property_list) at a slow cadence.
        await self._maybe_refresh_device_properties(devices)

        return {d["mac"]: d for d in devices if d.get("mac")}

    async def _maybe_refresh_device_properties(self, devices: list[WyzeCameraDevice]) -> None:
        """Refresh property_list for devices at a conservative interval.

        This keeps PID-backed entities (e.g. event recording) in sync without
        polling Wyze too aggressively.
        """
        now_mono = time.monotonic()
        if now_mono < self._property_disabled_until_mono:
            return

        if self._property_last_refresh_mono and (
            now_mono - self._property_last_refresh_mono
        ) < _PROPERTY_REFRESH_INTERVAL.total_seconds():
            return

        # Fetch sequentially to minimize burstiness and reduce chance of rate limiting.
        for dev in devices:
            mac = str(dev.get("mac") or "")
            model = str(dev.get("product_model") or "")
            if not mac or not model:
                continue
            try:
                info = await self.api.get_device_info(mac, model)
            except WyzeRateLimitError as err:
                wait_s = 300
                if err.reset_by:
                    wait_s = max(int(err.reset_by) - int(time.time()), 60)
                self._property_disabled_until_mono = time.monotonic() + wait_s
                _LOGGER.warning(
                    "Wyze rate limited during get_device_info; pausing property refresh for %ss",
                    wait_s,
                )
                return
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug("Failed get_device_info(%s): %s", mac, err)
                continue
            except WyzeApiError as err:
                _LOGGER.debug("Wyze API error get_device_info(%s): %s", mac, err)
                continue
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Unexpected error get_device_info(%s): %s", mac, err)
                continue

            prop_list = info.get("property_list")
            prop_list_norm: list[dict[str, Any]] = []
            if isinstance(prop_list, list):
                prop_list_norm = [p for p in prop_list if isinstance(p, dict)]
            props_by_pid = _flatten_property_list(prop_list_norm)

            self._property_list_by_mac[mac] = prop_list_norm
            self._properties_by_pid_by_mac[mac] = props_by_pid

            dev["property_list"] = list(prop_list_norm)
            dev["properties_by_pid"] = dict(props_by_pid)

            # Avoid hammering even with only a few devices.
            await asyncio.sleep(0.2)

        self._property_last_refresh_mono = time.monotonic()

    def set_cached_property(self, mac: str, pid: str, value: Any) -> None:
        """Update PID cache for a device (used for optimistic UI after set_property)."""
        mac = str(mac or "").strip()
        if not mac:
            return
        pid_u = str(pid).upper().strip()
        if not pid_u:
            return
        props = self._properties_by_pid_by_mac.setdefault(mac, {})
        props[pid_u] = value

        # Also update current coordinator data if available.
        dev = self.data.get(mac)
        if dev is None:
            return
        by_pid = dev.get("properties_by_pid")
        if isinstance(by_pid, dict):
            by_pid[pid_u] = value
        else:
            dev["properties_by_pid"] = {pid_u: value}

    def _async_update_entry_tokens(self) -> None:
        """Store rotated tokens back into the config entry."""
        access_token = self.api.access_token
        refresh_token = self.api.refresh_token
        phone_id = self.api.phone_id
        user_id = self.api.user_id

        new_data: dict[str, Any] = {}
        if access_token:
            new_data["access_token"] = access_token
        if refresh_token:
            new_data["refresh_token"] = refresh_token
        if phone_id:
            new_data["phone_id"] = phone_id
        if user_id:
            new_data["user_id"] = user_id

        # Only write when something actually changed.
        if not new_data:
            return

        for k, v in new_data.items():
            if self.entry.data.get(k) != v:
                self.hass.config_entries.async_update_entry(
                    self.entry, data={**self.entry.data, **new_data}
                )
                break
