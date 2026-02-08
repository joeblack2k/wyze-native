"""The Wyze Native integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_API_KEY,
    CONF_EMAIL,
    CONF_KEY_ID,
    CONF_PASSWORD,
    DOMAIN,
)
from .coordinator import WyzeNativeDataUpdateCoordinator
from .wyze_api import WyzeApiClient, WyzeApiError, WyzeAuthError


_LOGGER = logging.getLogger(__name__)

PLATFORMS: tuple[Platform, ...] = (
    Platform.CAMERA,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
)

_STALE_SWITCH_UNIQUE_SUFFIXES: set[str] = {
    # These were incorrectly exposed as switches in early versions; they are "supports_*"
    # capability flags (0/1) rather than actual toggles for cameras.
    "motion_alarm_switch",
    "audio_alarm_switch",
    "smoke_alarm_switch",
    "co_alarm_switch",
    # Previously created PID-based event switch used a different unique_id; now replaced
    # by the legacy `records_event_switch` unique_id for stability.
    "p4",
}


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration (YAML is not supported)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Wyze Native from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    session = async_get_clientsession(hass)
    api = WyzeApiClient(
        session,
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
        key_id=entry.data[CONF_KEY_ID],
        api_key=entry.data[CONF_API_KEY],
        phone_id=entry.data.get("phone_id"),
        access_token=entry.data.get("access_token"),
        refresh_token=entry.data.get("refresh_token"),
        user_id=entry.data.get("user_id"),
    )

    # Ensure we have a valid token before starting the coordinator.
    try:
        if not api.access_token:
            await api.login()
    except WyzeAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except WyzeApiError as err:
        # Treat as auth failure if Wyze denies login.
        raise ConfigEntryAuthFailed(str(err)) from err

    coordinator = WyzeNativeDataUpdateCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {"api": api, "coordinator": coordinator}

    _cleanup_stale_entity_registry_entries(hass, entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _cleanup_stale_entity_registry_entries(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: WyzeNativeDataUpdateCoordinator
) -> None:
    """Remove stale entities from the entity registry.

    Home Assistant keeps entity registry entries even when the integration no longer
    creates them. That is usually desirable, but early versions of this integration
    exposed several Wyze camera *capability* flags as switches, which caused clutter
    and confusion. We remove those stale entities automatically.
    """
    ent_reg = er.async_get(hass)
    entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    if not entries:
        return

    macs = {mac for mac in (coordinator.data or {}).keys() if isinstance(mac, str)}
    stale_unique_ids: set[str] = set()
    for mac in macs:
        for suffix in _STALE_SWITCH_UNIQUE_SUFFIXES:
            stale_unique_ids.add(f"{mac.lower()}_{suffix}")

    removed = 0
    for e in entries:
        uid = (e.unique_id or "").lower()
        if uid in stale_unique_ids:
            _LOGGER.debug("Removing stale entity registry entry: %s (%s)", e.entity_id, e.unique_id)
            ent_reg.async_remove(e.entity_id)
            removed += 1

    if removed:
        _LOGGER.info("Removed %s stale Wyze Native entities from the entity registry", removed)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Wyze Native config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
