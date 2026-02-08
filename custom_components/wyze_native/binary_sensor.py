"""Binary sensor entities for Wyze Native."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import WyzeNativeDataUpdateCoordinator
from .entity import WyzeNativeEntity


_LOGGER = logging.getLogger(__name__)

# Mark motion "on" briefly after the last thumbnail timestamp changes.
_MOTION_WINDOW = timedelta(seconds=120)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wyze binary sensors."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: WyzeNativeDataUpdateCoordinator = data["coordinator"]

    async_add_entities([WyzeNativeMotionSensor(coordinator, mac) for mac in coordinator.data])


class WyzeNativeMotionSensor(WyzeNativeEntity, BinarySensorEntity):
    """Best-effort motion sensor based on last thumbnail/event timestamp."""

    _attr_name = "Motion"
    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(self, coordinator: WyzeNativeDataUpdateCoordinator, mac: str) -> None:
        super().__init__(coordinator, mac)
        self._attr_unique_id = f"{mac}_motion"

    @property
    def is_on(self) -> bool:
        dev = self.coordinator.data.get(self._mac) or {}
        params = dev.get("device_params") or {}
        thumbs = params.get("camera_thumbnails") or {}
        ts = None
        if isinstance(thumbs, dict):
            ts = thumbs.get("thumbnails_ts")
        try:
            ts_int = int(ts) if ts is not None else 0
        except (TypeError, ValueError):
            ts_int = 0
        if ts_int <= 0:
            return False

        last = dt_util.utc_from_timestamp(ts_int / 1000)
        return (dt_util.utcnow() - last) <= _MOTION_WINDOW

    @property
    def extra_state_attributes(self) -> dict:
        dev = self.coordinator.data.get(self._mac) or {}
        params = dev.get("device_params") or {}
        thumbs = params.get("camera_thumbnails") or {}
        ts = thumbs.get("thumbnails_ts") if isinstance(thumbs, dict) else None
        try:
            ts_int = int(ts) if ts is not None else 0
        except (TypeError, ValueError):
            ts_int = 0
        return {"last_thumbnail_ts_ms": ts_int} if ts_int else {}

