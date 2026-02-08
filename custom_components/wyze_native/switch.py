"""Switch entities for Wyze Native."""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WyzeNativeDataUpdateCoordinator
from .entity import WyzeNativeEntity
from .wyze_api import WyzeApiClient, WyzeApiError


_LOGGER = logging.getLogger(__name__)

# Well-known camera property PIDs (from wyze-sdk / reverse engineering).
_PID_EVENT_SWITCH = "P4"     # Event recording master switch
_PID_MOTION_RECORD = "P1047" # Motion event recording enabled
_PID_SOUND_RECORD = "P1048"  # Sound event recording enabled


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wyze switch entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: WyzeNativeDataUpdateCoordinator = data["coordinator"]
    api: WyzeApiClient = data["api"]

    entities: list[SwitchEntity] = []
    for mac, dev in coordinator.data.items():
        entities.append(WyzeNativePowerSwitch(coordinator, api, mac))

        params = dev.get("device_params") or {}
        raw = dev.get("raw") or {}
        props_by_pid = dev.get("properties_by_pid") or {}

        def _has_key(key: str, source: str) -> bool:
            if source == "raw":
                return (isinstance(raw, dict) and key in raw) or (
                    isinstance(params, dict) and key in params
                )
            return (isinstance(params, dict) and key in params) or (
                isinstance(raw, dict) and key in raw
            )

        def _has_pid(pid: str) -> bool:
            pid_u = str(pid).upper()
            if isinstance(props_by_pid, dict) and pid_u in props_by_pid:
                return True
            prop_list = dev.get("property_list") or []
            if isinstance(prop_list, list):
                return any(
                    isinstance(p, dict) and str(p.get("pid") or "").upper() == pid_u
                    for p in prop_list
                )
            return False

        # PID-backed toggles (preferred): these reflect actual settings rather than "supports_*" flags.
        # Only create switches when the PID is present for the device.
        if _has_pid(_PID_EVENT_SWITCH):
            entities.append(
                WyzeNativePidSwitch(
                    coordinator,
                    api,
                    mac,
                    pid=_PID_EVENT_SWITCH,
                    name="Event Recording",
                    icon="mdi:record-rec",
                    entity_category=EntityCategory.CONFIG,
                    # Preserve legacy unique_id from the initial implementation so entity_id doesn't get duplicated.
                    unique_suffix="records_event_switch",
                )
            )
        if _has_pid(_PID_MOTION_RECORD):
            entities.append(
                WyzeNativePidSwitch(
                    coordinator,
                    api,
                    mac,
                    pid=_PID_MOTION_RECORD,
                    name="Record Motion Events",
                    icon="mdi:motion-sensor",
                    entity_category=EntityCategory.CONFIG,
                )
            )
        if _has_pid(_PID_SOUND_RECORD):
            entities.append(
                WyzeNativePidSwitch(
                    coordinator,
                    api,
                    mac,
                    pid=_PID_SOUND_RECORD,
                    name="Record Sound Events",
                    icon="mdi:volume-high",
                    entity_category=EntityCategory.CONFIG,
                )
            )

        # Common toggles exposed in device_params/raw (best-effort).
        for key, name, icon, source in (
            ("push_switch", "Notifications", "mdi:bell", "raw"),
            ("power_saving_mode_switch", "Power Saving Mode", "mdi:battery-heart", "device_params"),
            ("spotlight_status", "Spotlight", "mdi:spotlight", "device_params"),
            # Less understood toggles: present in some camera payloads, but semantics vary.
            # Keep disabled by default to avoid UI clutter/confusion.
            ("event_master_switch", "Events Master", "mdi:calendar-check", "raw"),
            ("accessory_switch", "Accessory", "mdi:usb", "device_params"),
            ("ai_notification_v2", "AI Notifications", "mdi:robot", "device_params"),
            ("dongle_switch", "Dongle", "mdi:usb", "device_params"),
        ):
            if _has_key(key, source):
                entities.append(
                    WyzeNativeBoolStateSwitch(
                        coordinator,
                        api,
                        mac,
                        key=key,
                        name=name,
                        icon=icon,
                        entity_category=EntityCategory.CONFIG,
                        value_source=source,
                        enabled_by_default=key
                        not in {
                            "event_master_switch",
                            "accessory_switch",
                            "ai_notification_v2",
                            "dongle_switch",
                        },
                    )
                )

        # Night vision support varies; only create if we can see a value.
        if isinstance(params, dict) and any(k in params for k in ("night_vision", "night_vision_status")):
            entities.append(WyzeNativeNightVisionSwitch(coordinator, api, mac))

    async_add_entities(entities)


class WyzeNativePowerSwitch(WyzeNativeEntity, SwitchEntity):
    """Power/Privacy control switch.

    Per project spec:
    - Switch ON  => Camera power ON  (privacy OFF)
    - Switch OFF => Camera power OFF (privacy ON)
    """

    _attr_name = "Power"
    _attr_icon = "mdi:power"

    def __init__(
        self, coordinator: WyzeNativeDataUpdateCoordinator, api: WyzeApiClient, mac: str
    ) -> None:
        super().__init__(coordinator, mac)
        self._api = api
        self._attr_unique_id = f"{mac}_power"
        self._pending_state: bool | None = None
        self._pending_until: float = 0.0
        # Avoid UI flicker: Wyze may transiently report the desired state then bounce back.
        # We require the state to match for a short period before clearing pending.
        self._matched_since: float | None = None

    def _actual_is_on(self) -> bool:
        """Return power state from coordinator data (no pending override)."""
        dev = self.coordinator.data.get(self._mac, {})
        raw = dev.get("raw") or {}
        val = dev.get("power_switch", raw.get("power_switch"))
        try:
            return int(val) == 1
        except (TypeError, ValueError):
            return False

    @property
    def is_on(self) -> bool:
        actual = self._actual_is_on()
        if self._pending_state is None:
            return actual

        now = time.monotonic()
        if now >= self._pending_until:
            # Pending timed out; fall back to coordinator state.
            self._pending_state = None
            self._matched_since = None
            return actual

        if actual == self._pending_state:
            # Only clear pending when we've been stable for a few seconds.
            if self._matched_since is None:
                self._matched_since = now
            if (now - self._matched_since) >= 3:
                self._pending_state = None
                self._matched_since = None
                return actual
        else:
            self._matched_since = None

        # Avoid UI flicker while Wyze applies the change.
        return self._pending_state

    async def async_turn_on(self, **kwargs) -> None:
        self._pending_state = True
        self._pending_until = time.monotonic() + 15
        self._matched_since = None
        self.async_write_ha_state()
        await self._set_power(True)

    async def async_turn_off(self, **kwargs) -> None:
        self._pending_state = False
        self._pending_until = time.monotonic() + 15
        self._matched_since = None
        self.async_write_ha_state()
        await self._set_power(False)

    async def _set_power(self, on: bool) -> None:
        value = 1 if on else 0
        dev = self.coordinator.data.get(self._mac, {})
        model = dev.get("product_model")
        try:
            if model:
                # P3 is the cloud "power state" PID used by wyze-sdk for cameras.
                try:
                    await self._api.set_property(self._mac, str(model), "P3", value)
                except WyzeApiError:
                    # Some models/accounts reject set_property; fall back to set_device_Info.
                    await self._api.set_state(self._mac, "power_switch", value)
            else:
                await self._api.set_state(self._mac, "power_switch", value)
        except WyzeApiError as err:
            _LOGGER.error("Failed setting power_switch for %s: %s", self._mac, err)
            raise

        # Give Wyze a moment to apply the command before we start comparing.
        await asyncio.sleep(2)

        # Refresh, and if the state did not change as expected, try the other method once.
        await self.coordinator.async_request_refresh()
        if self._actual_is_on() != on:
            await asyncio.sleep(2)
            await self.coordinator.async_request_refresh()

        if self._actual_is_on() != on:
            try:
                await self._api.set_state(self._mac, "power_switch", value)
            except WyzeApiError as err:
                _LOGGER.debug("Fallback set_state failed for %s: %s", self._mac, err)
            await asyncio.sleep(2)
            await self.coordinator.async_request_refresh()


class WyzeNativePidSwitch(WyzeNativeEntity, SwitchEntity):
    """On/off switch backed by Wyze's set_property PID."""

    def __init__(
        self,
        coordinator: WyzeNativeDataUpdateCoordinator,
        api: WyzeApiClient,
        mac: str,
        *,
        pid: str,
        name: str,
        icon: str,
        entity_category: EntityCategory | None = None,
        unique_suffix: str | None = None,
        enabled_by_default: bool = True,
    ) -> None:
        super().__init__(coordinator, mac)
        self._api = api
        self._pid = str(pid).upper()
        self._attr_name = name
        self._attr_icon = icon
        self._attr_entity_category = entity_category
        suffix = (unique_suffix or self._pid.lower()).strip().lower()
        self._attr_unique_id = f"{mac}_{suffix}"
        self._attr_entity_registry_enabled_default = enabled_by_default

    @property
    def is_on(self) -> bool:
        dev = self.coordinator.data.get(self._mac, {})
        props = dev.get("properties_by_pid") or {}
        val = None
        if isinstance(props, dict) and self._pid in props:
            val = props.get(self._pid)
        else:
            prop_list = dev.get("property_list") or []
            if isinstance(prop_list, list):
                for item in prop_list:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("pid") or "").upper() != self._pid:
                        continue
                    val = item.get("value", item.get("pvalue"))
                    break
        try:
            return int(val) == 1
        except (TypeError, ValueError):
            return False

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)

    async def _set(self, on: bool) -> None:
        value = 1 if on else 0
        dev = self.coordinator.data.get(self._mac, {})
        model = dev.get("product_model") or (dev.get("raw") or {}).get("product_model")
        if not model:
            raise WyzeApiError(f"Missing device model for {self._mac}")

        try:
            await self._api.set_property(self._mac, str(model), self._pid, value)
        except WyzeApiError as err:
            _LOGGER.error("Failed setting %s for %s: %s", self._pid, self._mac, err)
            raise

        # Optimistic update so UI doesn't bounce while waiting for next property refresh.
        self.coordinator.set_cached_property(self._mac, self._pid, value)
        self.async_write_ha_state()

        # Best-effort verification for just this PID, without refreshing everything.
        await asyncio.sleep(1)
        try:
            info = await self._api.get_device_info(self._mac, str(model))
            prop_list = info.get("property_list") or []
            if isinstance(prop_list, list):
                for item in prop_list:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("pid") or "").upper() != self._pid:
                        continue
                    actual = item.get("value", item.get("pvalue"))
                    self.coordinator.set_cached_property(self._mac, self._pid, actual)
                    break
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed verifying %s for %s: %s", self._pid, self._mac, err)

        await self.coordinator.async_request_refresh()


class WyzeNativeBoolStateSwitch(WyzeNativeEntity, SwitchEntity):
    """Generic on/off switch backed by Wyze's set_device_Info."""

    def __init__(
        self,
        coordinator: WyzeNativeDataUpdateCoordinator,
        api: WyzeApiClient,
        mac: str,
        *,
        key: str,
        name: str,
        icon: str,
        entity_category: EntityCategory | None = None,
        value_source: str = "device_params",
        enabled_by_default: bool = True,
    ) -> None:
        super().__init__(coordinator, mac)
        self._api = api
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_entity_category = entity_category
        self._attr_unique_id = f"{mac}_{key}"
        self._value_source = value_source
        self._attr_entity_registry_enabled_default = enabled_by_default

    @property
    def is_on(self) -> bool:
        dev = self.coordinator.data.get(self._mac, {})
        params = dev.get("device_params") or {}
        raw = dev.get("raw") or {}
        if self._value_source == "raw":
            val = raw.get(self._key) if isinstance(raw, dict) else None
            if val is None and isinstance(params, dict):
                val = params.get(self._key)
        else:
            val = params.get(self._key) if isinstance(params, dict) else None
            if val is None and isinstance(raw, dict):
                val = raw.get(self._key)
        try:
            return int(val) == 1
        except (TypeError, ValueError):
            return False

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)

    async def _set(self, on: bool) -> None:
        try:
            await self._api.set_state(self._mac, self._key, 1 if on else 0)
        except WyzeApiError as err:
            _LOGGER.error("Failed setting %s for %s: %s", self._key, self._mac, err)
            raise
        await self.coordinator.async_request_refresh()


class WyzeNativeNightVisionSwitch(WyzeNativeEntity, SwitchEntity):
    """Night vision switch (best-effort; Wyze cloud support varies by model)."""

    _attr_name = "Night Vision"
    _attr_icon = "mdi:weather-night"

    def __init__(
        self, coordinator: WyzeNativeDataUpdateCoordinator, api: WyzeApiClient, mac: str
    ) -> None:
        super().__init__(coordinator, mac)
        self._api = api
        self._attr_unique_id = f"{mac}_night_vision"

    @property
    def is_on(self) -> bool:
        dev = self.coordinator.data.get(self._mac, {})
        params = dev.get("device_params") or {}
        raw = dev.get("raw") or {}
        # Common-ish candidates; may be absent.
        val = (
            params.get("night_vision")
            or params.get("night_vision_status")
            or raw.get("night_vision")
            or raw.get("night_vision_status")
        )
        try:
            # Many camera firmwares use 2=off, 3=auto/on.
            return int(val) in (1, 3)
        except (TypeError, ValueError):
            return False

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_night_vision(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_night_vision(False)

    async def _set_night_vision(self, on: bool) -> None:
        # docker-wyze-bridge uses 3 for ON, 2 for OFF in its MQTT mapping.
        value = 3 if on else 2
        try:
            await self._api.set_state(self._mac, "night_vision", value)
        except WyzeApiError as err:
            _LOGGER.error("Failed setting night_vision for %s: %s", self._mac, err)
            raise
        await self.coordinator.async_request_refresh()
