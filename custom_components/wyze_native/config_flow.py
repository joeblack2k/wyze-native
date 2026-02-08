"""Config flow for Wyze Native."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_API_KEY,
    CONF_EMAIL,
    CONF_KEY_ID,
    CONF_PASSWORD,
    CONF_STREAM_URL_TEMPLATE,
    CONF_USE_PLACEHOLDER_IMAGE,
    DOMAIN,
)
from .wyze_api import WyzeApiClient, WyzeApiError, WyzeAuthError, WyzeRateLimitError


_LOGGER = logging.getLogger(__name__)

TEXT_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
STREAM_TEMPLATE_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, multiline=False))


STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TEXT_SELECTOR,
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
        vol.Required(CONF_KEY_ID): TEXT_SELECTOR,
        vol.Required(CONF_API_KEY): PASSWORD_SELECTOR,
    }
)

class WyzeNativeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Wyze Native."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        return WyzeNativeOptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            key_id = user_input[CONF_KEY_ID].strip()
            api_key = user_input[CONF_API_KEY].strip()

            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            client = WyzeApiClient(
                session,
                email=email,
                password=password,
                key_id=key_id,
                api_key=api_key,
            )

            try:
                cred = await client.login()
            except WyzeRateLimitError as err:
                _LOGGER.warning("Wyze rate limited during login: %s", err)
                errors["base"] = "rate_limited"
            except WyzeAuthError as err:
                _LOGGER.warning("Wyze auth failed: %s", err)
                errors["base"] = "invalid_auth"
            except WyzeApiError as err:
                _LOGGER.warning("Wyze API error during login: %s", err)
                errors["base"] = "invalid_auth"
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during Wyze login: %s", err)
                errors["base"] = "unknown"
            else:
                data = {
                    CONF_EMAIL: email,
                    CONF_PASSWORD: password,
                    CONF_KEY_ID: key_id,
                    CONF_API_KEY: api_key,
                    "phone_id": cred.phone_id,
                    "access_token": cred.access_token,
                    "refresh_token": cred.refresh_token,
                    "user_id": cred.user_id,
                }
                return self.async_create_entry(title=email, data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )


class WyzeNativeOptionsFlowHandler(OptionsFlow):
    """Handle options flow for Wyze Native."""

    def __init__(self, config_entry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            template = (user_input.get(CONF_STREAM_URL_TEMPLATE) or "").strip()
            use_placeholder = bool(user_input.get(CONF_USE_PLACEHOLDER_IMAGE, True))
            return self.async_create_entry(
                title="",
                data={
                    CONF_STREAM_URL_TEMPLATE: template,
                    CONF_USE_PLACEHOLDER_IMAGE: use_placeholder,
                },
            )

        current = (self.config_entry.options.get(CONF_STREAM_URL_TEMPLATE) or "").strip()
        current_placeholder = bool(
            self.config_entry.options.get(CONF_USE_PLACEHOLDER_IMAGE, True)
        )
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_STREAM_URL_TEMPLATE, default=current
                ): STREAM_TEMPLATE_SELECTOR,
                vol.Optional(
                    CONF_USE_PLACEHOLDER_IMAGE, default=current_placeholder
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
