"""Sensor entities for Wyze Native."""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import WyzeNativeDataUpdateCoordinator
from .entity import WyzeNativeEntity


_LOGGER = logging.getLogger(__name__)


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_first(dev: dict[str, Any], keys: list[str]) -> Any:
    """Try multiple keys across dev/device_params/raw."""
    params = dev.get("device_params") or {}
    raw = dev.get("raw") or {}
    for k in keys:
        if isinstance(params, dict) and k in params:
            return params.get(k)
        if isinstance(raw, dict) and k in raw:
            return raw.get(k)
        if k in dev:
            return dev.get(k)
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Wyze sensor entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: WyzeNativeDataUpdateCoordinator = data["coordinator"]

    entities: list[SensorEntity] = []
    for mac in coordinator.data:
        entities.append(WyzeNativeConnectionSensor(coordinator, mac))
        entities.append(WyzeNativeBatterySensor(coordinator, mac))
        entities.append(WyzeNativeRssiSensor(coordinator, mac))
        entities.append(WyzeNativeLastThumbnailSensor(coordinator, mac))
        entities.append(WyzeNativeSsidSensor(coordinator, mac))
        entities.append(WyzeNativeLocalIpSensor(coordinator, mac))
        entities.append(WyzeNativePublicIpSensor(coordinator, mac))
        entities.append(WyzeNativeFirmwareVersionSensor(coordinator, mac))
        entities.append(WyzeNativeHardwareVersionSensor(coordinator, mac))
        entities.append(WyzeNativePropertiesSensor(coordinator, mac))
        entities.append(WyzeNativeTemperatureSensor(coordinator, mac))
        entities.append(WyzeNativeHumiditySensor(coordinator, mac))

    async_add_entities(entities)


class _WyzeNativeValueSensor(WyzeNativeEntity, SensorEntity):
    """Generic coordinator-backed sensor for a Wyze device."""

    def __init__(
        self,
        coordinator: WyzeNativeDataUpdateCoordinator,
        mac: str,
        *,
        unique_suffix: str,
        name: str,
        value_fn: Callable[[dict[str, Any]], Any],
    ) -> None:
        super().__init__(coordinator, mac)
        self._attr_unique_id = f"{mac}_{unique_suffix}"
        self._attr_name = name
        self._value_fn = value_fn

    @property
    def native_value(self) -> Any:
        dev = self.coordinator.data.get(self._mac)
        if not dev:
            return None
        return self._value_fn(dev)


class WyzeNativeConnectionSensor(_WyzeNativeValueSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:lan-connect"

    def __init__(self, coordinator: WyzeNativeDataUpdateCoordinator, mac: str) -> None:
        super().__init__(
            coordinator,
            mac,
            unique_suffix="connection",
            name="Connection",
            value_fn=self._value,
        )

    @staticmethod
    def _value(dev: dict[str, Any]) -> str:
        conn_state = _coerce_int(_extract_first(dev, ["conn_state"]))
        return "online" if conn_state == 1 else "offline"


class WyzeNativeBatterySensor(_WyzeNativeValueSensor):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator: WyzeNativeDataUpdateCoordinator, mac: str) -> None:
        super().__init__(
            coordinator,
            mac,
            unique_suffix="battery",
            name="Battery",
            value_fn=self._value,
        )

    @staticmethod
    def _value(dev: dict[str, Any]) -> int | None:
        val = _extract_first(
            dev,
            [
                "electricity",
                "battery",
                "battery_level",
                "battery_percent",
                "battery_percentage",
                "battery_value",
            ],
        )
        v = _coerce_int(val)
        if v is None:
            return None
        if 0 <= v <= 100:
            return v
        return None


class WyzeNativeRssiSensor(_WyzeNativeValueSensor):
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator: WyzeNativeDataUpdateCoordinator, mac: str) -> None:
        super().__init__(
            coordinator,
            mac,
            unique_suffix="rssi",
            name="WiFi RSSI",
            value_fn=self._value,
        )

    @staticmethod
    def _value(dev: dict[str, Any]) -> int | None:
        # RSSI is typically negative (dBm).
        val = _extract_first(
            dev,
            [
                "rssi",
                "wifi_rssi",
                "wifiRSSI",
                "signal",
                "signal_strength",
                "wifi_signal",
            ],
        )
        v = _coerce_int(val)
        if v is None:
            return None
        # Accept common ranges; some APIs report 0-100 signal percent which we ignore.
        if -120 <= v <= 0:
            return v
        return None


class WyzeNativeLastThumbnailSensor(_WyzeNativeValueSensor):
    """Timestamp of the last thumbnail/event reported by Wyze."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: WyzeNativeDataUpdateCoordinator, mac: str) -> None:
        super().__init__(
            coordinator,
            mac,
            unique_suffix="last_thumbnail",
            name="Last Thumbnail",
            value_fn=self._value,
        )

    @staticmethod
    def _value(dev: dict[str, Any]):
        params = dev.get("device_params") or {}
        thumbs = params.get("camera_thumbnails") or {}
        ts = thumbs.get("thumbnails_ts") if isinstance(thumbs, dict) else None
        try:
            ts_int = int(ts) if ts is not None else 0
        except (TypeError, ValueError):
            ts_int = 0
        if ts_int <= 0:
            return None
        return dt_util.utc_from_timestamp(ts_int / 1000)


class WyzeNativeSsidSensor(_WyzeNativeValueSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator: WyzeNativeDataUpdateCoordinator, mac: str) -> None:
        super().__init__(
            coordinator,
            mac,
            unique_suffix="ssid",
            name="WiFi SSID",
            value_fn=lambda dev: _extract_first(dev, ["ssid"]),
        )


class WyzeNativeLocalIpSensor(_WyzeNativeValueSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:ip-network"

    def __init__(self, coordinator: WyzeNativeDataUpdateCoordinator, mac: str) -> None:
        super().__init__(
            coordinator,
            mac,
            unique_suffix="local_ip",
            name="Local IP",
            value_fn=lambda dev: _extract_first(dev, ["ip"]),
        )


class WyzeNativePublicIpSensor(_WyzeNativeValueSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:ip"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: WyzeNativeDataUpdateCoordinator, mac: str) -> None:
        super().__init__(
            coordinator,
            mac,
            unique_suffix="public_ip",
            name="Public IP",
            value_fn=lambda dev: _extract_first(dev, ["public_ip"]),
        )


class WyzeNativeFirmwareVersionSensor(_WyzeNativeValueSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:chip"

    def __init__(self, coordinator: WyzeNativeDataUpdateCoordinator, mac: str) -> None:
        super().__init__(
            coordinator,
            mac,
            unique_suffix="firmware_version",
            name="Firmware Version",
            value_fn=self._value,
        )

    @staticmethod
    def _value(dev: dict[str, Any]) -> str | None:
        val = _extract_first(dev, ["firmware_ver"])
        if val is None:
            return None
        s = str(val).strip()
        return s or None


class WyzeNativeHardwareVersionSensor(_WyzeNativeValueSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:chip"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: WyzeNativeDataUpdateCoordinator, mac: str) -> None:
        super().__init__(
            coordinator,
            mac,
            unique_suffix="hardware_version",
            name="Hardware Version",
            value_fn=self._value,
        )

    @staticmethod
    def _value(dev: dict[str, Any]) -> str | None:
        val = _extract_first(dev, ["hardware_ver"])
        if val is None:
            return None
        s = str(val).strip()
        return s or None


class WyzeNativePropertiesSensor(WyzeNativeEntity, SensorEntity):
    """Expose raw PID properties for diagnostics / reverse-engineering.

    This intentionally does not create dozens of per-PID entities, but still
    lets users inspect which properties are present on their camera model.
    """

    _attr_name = "Properties"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:code-json"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: WyzeNativeDataUpdateCoordinator, mac: str) -> None:
        super().__init__(coordinator, mac)
        self._attr_unique_id = f"{mac}_properties"

    @property
    def native_value(self) -> int:
        dev = self.coordinator.data.get(self._mac) or {}
        props = dev.get("properties_by_pid") or {}
        if isinstance(props, dict):
            return len(props)
        return 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        dev = self.coordinator.data.get(self._mac) or {}
        props = dev.get("properties_by_pid") or {}
        prop_list = dev.get("property_list") or []
        # Keep attributes small/stable.
        attrs: dict[str, Any] = {}
        if isinstance(props, dict):
            attrs["properties_by_pid"] = dict(props)
            attrs["property_pids"] = sorted(props.keys())
        elif isinstance(prop_list, list):
            attrs["property_pids"] = sorted(
                {
                    str(p.get("pid") or "").upper()
                    for p in prop_list
                    if isinstance(p, dict) and p.get("pid")
                }
            )
        return attrs


class WyzeNativeTemperatureSensor(_WyzeNativeValueSensor):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: WyzeNativeDataUpdateCoordinator, mac: str) -> None:
        super().__init__(
            coordinator,
            mac,
            unique_suffix="temperature",
            name="Temperature",
            value_fn=self._value,
        )

    @staticmethod
    def _value(dev: dict[str, Any]) -> float | None:
        params = dev.get("device_params") or {}
        supports: bool | None = None
        if isinstance(params, dict) and "is_temperature_humidity" in params:
            try:
                supports = int(params.get("is_temperature_humidity") or 0) == 1
            except (TypeError, ValueError):
                supports = None
        if supports is False:
            return None
        val = _extract_first(dev, ["temperature"])
        try:
            v = float(val)
        except (TypeError, ValueError):
            return None
        # Wyze often returns 0 when the device does not support temp/humidity.
        if v == 0 and supports is None:
            return None
        return v


class WyzeNativeHumiditySensor(_WyzeNativeValueSensor):
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: WyzeNativeDataUpdateCoordinator, mac: str) -> None:
        super().__init__(
            coordinator,
            mac,
            unique_suffix="humidity",
            name="Humidity",
            value_fn=self._value,
        )

    @staticmethod
    def _value(dev: dict[str, Any]) -> float | None:
        params = dev.get("device_params") or {}
        supports: bool | None = None
        if isinstance(params, dict) and "is_temperature_humidity" in params:
            try:
                supports = int(params.get("is_temperature_humidity") or 0) == 1
            except (TypeError, ValueError):
                supports = None
        if supports is False:
            return None
        val = _extract_first(dev, ["humidity"])
        try:
            v = float(val)
        except (TypeError, ValueError):
            return None
        if v == 0 and supports is None:
            return None
        return v
