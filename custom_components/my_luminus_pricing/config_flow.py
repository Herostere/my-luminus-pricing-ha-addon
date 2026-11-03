"""Config flows for our integration.
https://developers.home-assistant.io/docs/data_entry_flow_index/#labels--descriptions

Authentication: the user pastes the Cookie header of a logged-in My Luminus
browser session (www.luminus.be). There is no username/password login any more.
"""

from __future__ import annotations
from collections.abc import Mapping
from typing import Any
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api import API, AuthError
from .const import CONF_COOKIES, DOMAIN
import logging
import voluptuous as vol

_LOGGER = logging.getLogger(__name__)

# Masked field: the cookies give access to the account
COOKIE_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

STEP_COOKIES_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_COOKIES): COOKIE_SELECTOR,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate that the pasted cookies give access to My Luminus.

    Raises InvalidAuth when the cookies are missing, expired or blocked, and
    CannotConnect for any other failure.
    """
    api = API(data[CONF_COOKIES])
    if not api.cookies:
        raise InvalidAuth("No cookies found in the pasted text")

    try:
        await hass.async_add_executor_job(api.renew)
        meters = await hass.async_add_executor_job(api.get_meters)
    except AuthError as err:
        _LOGGER.warning("My Luminus rejected the cookies: %s", err)
        raise InvalidAuth(str(err)) from err
    except Exception as err:
        _LOGGER.exception("Could not reach My Luminus")
        raise CannotConnect(str(err)) from err

    # Account id from the cookies; fall back to the meter EANs
    account = api.account_id or ",".join(sorted(m.get("ean", "") for m in meters.get("meters", [])))
    # Keep the cookies as renewed during validation, in case Luminus rotates them
    cookies = "; ".join(f"{k}={v}" for k, v in api.cookies.items())
    return {"title": f"Luminus - {account}", "unique_id": account, "cookies": cookies}


class ExampleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Example Integration."""

    VERSION = 1

    async def _async_validate(self, user_input: dict[str, Any], errors: dict[str, str]) -> dict[str, Any] | None:
        """Run validate_input and translate failures to form errors."""
        try:
            return await validate_input(self.hass, user_input)
        except InvalidAuth:
            errors["base"] = "invalid_cookies"
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step.

        Called when you initiate adding an integration via the UI
        """

        errors: dict[str, str] = {}

        if user_input is not None:
            info = await self._async_validate(user_input, errors)
            if info is not None:
                # Abort early if this account is already set up
                await self.async_set_unique_id(info["unique_id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info["title"], data={CONF_COOKIES: info["cookies"]})

        # Show initial form.
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_COOKIES_SCHEMA,
            errors=errors,
            last_step=True,  # Adding last_step True/False decides whether form shows Next or Submit buttons
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the cookies of an existing entry from the integration's menu."""
        errors: dict[str, str] = {}

        if user_input is not None:
            info = await self._async_validate(user_input, errors)
            if info is not None:
                # data= replaces everything, which also removes any old username/password
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(),
                    data={CONF_COOKIES: info["cookies"]},
                    reason="reconfigure_successful",
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=STEP_COOKIES_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Started by Home Assistant when the session cookies stop working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for fresh browser cookies."""
        errors: dict[str, str] = {}

        if user_input is not None:
            info = await self._async_validate(user_input, errors)
            if info is not None:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data={CONF_COOKIES: info["cookies"]},
                    reason="reauth_successful",
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_COOKIES_SCHEMA,
            errors=errors,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
