"""Constants for our integration."""

DOMAIN = "my_luminus_pricing"

DEFAULT_SCAN_INTERVAL = 43200
MIN_SCAN_INTERVAL = 43200
HTTP_TIMEOUT = 60
# Fallback gas conversion factor (kWh per m³) until a gas settlement has been read
GAS_M3_TO_KWH = 11.3

# Config entries created with browser cookies (version 1) only held this key;
# it is removed when such an entry is migrated
CONF_COOKIES = "cookies"

# Refresh token from the login in the config flow, so setup does not log in a
# second time; later tokens are kept in the token store (see coordinator.py)
CONF_REFRESH_TOKEN = "refresh_token"
