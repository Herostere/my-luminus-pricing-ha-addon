"""Constants for our integration."""

DOMAIN = "my_luminus_pricing"

DEFAULT_SCAN_INTERVAL = 43200
MIN_SCAN_INTERVAL = 43200
HTTP_TIMEOUT = 60
# Fallback gas conversion factor (kWh per m³) until a gas settlement has been read
GAS_M3_TO_KWH = 11.3


# Cookie header of a logged-in My Luminus browser session (www.luminus.be),
# format "name=value; name=value"
CONF_COOKIES = "cookies"

# Load a My Luminus page this often to renew the 24 h session cookies
KEEPALIVE_INTERVAL_HOURS = 6
