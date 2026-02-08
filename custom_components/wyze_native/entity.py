"""Shared entity helpers for Wyze Native."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WyzeNativeDataUpdateCoordinator


class WyzeNativeEntity(CoordinatorEntity[WyzeNativeDataUpdateCoordinator]):
    """Base entity class for Wyze Native."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: WyzeNativeDataUpdateCoordinator, mac: str) -> None:
        super().__init__(coordinator)
        self._mac = mac

    @property
    def device_info(self) -> DeviceInfo:
        dev = self.coordinator.data.get(self._mac, {})
        raw = dev.get("raw") or {}
        return DeviceInfo(
            identifiers={(DOMAIN, self._mac)},
            manufacturer="Wyze",
            name=(dev.get("nickname") or self._mac),
            model=str(dev.get("product_model") or raw.get("product_model") or ""),
            hw_version=str(raw.get("hardware_ver") or ""),
            sw_version=str(raw.get("firmware_ver") or ""),
        )

    @property
    def available(self) -> bool:
        # conn_state is 1 when online in many API responses.
        dev = self.coordinator.data.get(self._mac)
        if not dev:
            return False
        return int(dev.get("conn_state") or 0) == 1
