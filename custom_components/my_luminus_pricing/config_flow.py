"""Config flows for our integration.
https://developers.home-assistant.io/docs/data_entry_flow_index/#labels--descriptions

Authentication: the user enters the email and password of their My Luminus
account. They are exchanged for tokens through the Luminus app's login, and
kept in the config entry so the integration can log in again by itself when
the refresh token stops working.
"""

from __future__ import annotations
from collections.abc import Mapping
from typing import Any
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api import API, AuthError, VerificationRequired
from .const import CONF_REFRESH_TOKEN, DOMAIN
import logging
import voluptuous as vol

_LOGGER = logging.getLogger(__name__)


def _schema(username: str | None = None) -> vol.Schema:
    """Email and password form, with the email prefilled when known."""
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=username or vol.UNDEFINED): TextSelector(
                TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
            ),
            vol.Required(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
            ),
        }
    )


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Log in to Luminus and read the meters.

    Raises InvalidAuth when Luminus rejects the credentials, and CannotConnect
    for any other failure.
    """
    username = data[CONF_USERNAME].strip()
    api = API(username, data[CONF_PASSWORD])
    try:
        await hass.async_add_executor_job(api.login)
        await hass.async_add_executor_job(api.get_meters)
    except VerificationRequired as err:
        _LOGGER.warning("Luminus asks for extra verification of the login: %s", err)
        raise NeedsVerification(str(err)) from err
    except AuthError as err:
        _LOGGER.warning("Luminus rejected the login: %s", err)
        raise InvalidAuth(str(err)) from err
    except Exception as err:
        _LOGGER.exception("Could not reach My Luminus")
        raise CannotConnect(str(err)) from err

    return {
        "title": f"Luminus - {username}",
        "unique_id": username.lower(),
        # The refresh token is handed to the coordinator so setup does not log in again
        "data": {CONF_USERNAME: username, CONF_PASSWORD: data[CONF_PASSWORD], CONF_REFRESH_TOKEN: api.refresh_token},
    }


class ExampleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Example Integration."""

    # Version 1: browser cookies; version 2: email and password
    VERSION = 2

    async def _async_validate(self, user_input: dict[str, Any], errors: dict[str, str]) -> dict[str, Any] | None:
        """Run validate_input and translate failures to form errors."""
        try:
            return await validate_input(self.hass, user_input)
        except NeedsVerification:
            errors["base"] = "verification_required"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
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
                return self.async_create_entry(title=info["title"], data=info["data"])

        # Show initial form.
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(user_input and user_input.get(CONF_USERNAME)),
            errors=errors,
            last_step=True,  # Adding last_step True/False decides whether form shows Next or Submit buttons
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the email or password of an existing entry from the integration's menu."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            info = await self._async_validate(user_input, errors)
            if info is not None:
                await self.async_set_unique_id(info["unique_id"])
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, title=info["title"], data=info["data"], reason="reconfigure_successful",
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(entry.data.get(CONF_USERNAME)),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Started by Home Assistant when Luminus rejects the saved credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the email and password again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            info = await self._async_validate(user_input, errors)
            if info is not None:
                if entry.data.get(CONF_USERNAME):
                    await self.async_set_unique_id(info["unique_id"])
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                # Entries migrated from browser cookies have no email yet: adopt it as unique id
                return self.async_update_reload_and_abort(
                    entry, unique_id=info["unique_id"], title=info["title"], data=info["data"],
                    reason="reauth_successful",
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_schema(entry.data.get(CONF_USERNAME)),
            errors=errors,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class NeedsVerification(HomeAssistantError):
    """Luminus' bot detection wants a CAPTCHA for this login."""
