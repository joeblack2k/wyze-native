"""Constants for the Wyze Native integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final


DOMAIN: Final = "wyze_native"

CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"
CONF_KEY_ID: Final = "key_id"
CONF_API_KEY: Final = "api_key"

# Options
CONF_STREAM_URL_TEMPLATE: Final = "stream_url_template"
CONF_USE_PLACEHOLDER_IMAGE: Final = "use_placeholder_image"

# Wyze's API is rate-limited; keep polling conservative.
DEFAULT_SCAN_INTERVAL: Final = timedelta(seconds=60)
