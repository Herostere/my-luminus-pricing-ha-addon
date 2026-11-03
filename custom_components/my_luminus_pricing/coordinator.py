"""DataUpdateCoordinator for our integration."""

import hashlib
from datetime import timedelta, datetime
from typing import Any
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import API, AuthError
from .const import CONF_COOKIES, DEFAULT_SCAN_INTERVAL, DOMAIN, GAS_M3_TO_KWH, FORCAST_DATA_ELECTRICITY, FORCAST_DATA_GAS
import logging

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1


def cookie_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Storage file holding the renewed session cookies of one config entry.

    Kept outside the config entry on purpose: updating the entry's data
    triggers the update listener in __init__.py, which reloads the integration.
    """
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.cookies")


class LuminusCoordinator(DataUpdateCoordinator):
    """My example coordinator."""

    data: list[dict[str, Any]]

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize coordinator."""

        # Cookies pasted in the config flow; renewed cookies are kept in self._store
        self.entry_cookies = config_entry.data.get(CONF_COOKIES, "")
        # Identifies which pasted cookies the stored (renewed) cookies descend from
        self._seed = hashlib.sha256(self.entry_cookies.encode()).hexdigest()
        self._store = cookie_store(hass, config_entry.entry_id)
        self._saved_cookies: dict[str, str] | None = None
        self.api: API | None = None

        # set variables from options.  You need a default here in case options have not been set
        self.poll_interval = config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )

        # Initialise DataUpdateCoordinator
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN} ({config_entry.unique_id})",
            # Method to call on every update interval.
            update_method=self.async_update_data,
            # Polling interval. Will only be polled if you have made your
            # platform entities, CoordinatorEntities.
            # Using config option here but you can just use a fixed value.
            update_interval=timedelta(seconds=self.poll_interval),
        )


    async def _async_setup(self) -> None:
        """Load the latest renewed cookies before the first refresh."""
        if not self.entry_cookies:
            raise ConfigEntryAuthFailed("No My Luminus cookies configured")

        stored = await self._store.async_load()
        if stored and stored.get("seed") == self._seed and stored.get("cookies"):
            cookies = stored["cookies"]
        else:
            # New or re-pasted cookies: start from what the user entered
            cookies = self.entry_cookies

        self.api = API(cookies)
        self._saved_cookies = dict(self.api.cookies) if stored and stored.get("seed") == self._seed else None


    async def _async_save_cookies(self) -> None:
        """Persist renewed cookies so a restart continues the same session."""
        if self.api is None or self.api.cookies == self._saved_cookies:
            return
        await self._store.async_save({"seed": self._seed, "cookies": self.api.cookies})
        self._saved_cookies = dict(self.api.cookies)


    async def async_keep_alive(self, _now=None) -> None:
        """Renew the 24 h session between (12 h) data refreshes."""
        if self.api is None:
            return
        try:
            await self.hass.async_add_executor_job(self.api.renew)
            await self._async_save_cookies()
        except AuthError as err:
            _LOGGER.error("My Luminus session no longer valid (%s); paste fresh browser cookies when asked to reauthenticate", err)
            self.config_entry.async_start_reauth(self.hass)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("My Luminus keep-alive failed, will retry: %s", err)


    def _forecast_remaining_cost(self, cost_so_far: float, current_month: int, remaining_months: int, energy: str) -> float:
        """Forecast remaining cost until next April."""
        if current_month <= 0 or remaining_months <= 0:
            return 0

        forecast = 0

        month_number = current_month
        for offset in range(remaining_months):
            forecast += FORCAST_DATA_GAS.get(month_number, 0) if energy == "gas" else FORCAST_DATA_ELECTRICITY.get(month_number, 0)
            month_number += 1

        return forecast


    async def async_update_data(self):
        """Fetch data from API endpoint.

        This is the place to retrieve and pre-process the data into an appropriate data structure
        to be used to provide values for all your entities.
        """
        try:
            # ----------------------------------------------------------------------------
            # Get the data from your api
            # ----------------------------------------------------------------------------

            # Renew the session first; this also detects expired cookies early
            await self.hass.async_add_executor_job(self.api.renew)
            meters = await self.hass.async_add_executor_job(self.api.get_meters)
            data = []

            current_month = ((datetime.now().month - 5) % 12) + 1

            for meter in meters['meters']:
                eanNr = meter['ean']
                energyType = meter['energyType']
                meterDetails = await self.hass.async_add_executor_job(self.api.get_meter, eanNr)
                consumptionDetails = await self.hass.async_add_executor_job(self.api.get_current_consumption, eanNr)
                budgetDetails = await self.hass.async_add_executor_job(self.api.get_advance_and_paid)

                if meterDetails and consumptionDetails and budgetDetails:
                    pname = meterDetails['productName']
                    prices = meterDetails['prices']
                    meterType = "dual" if "dual" in prices else meterDetails['activeMeterType']
                    meterPrices = prices[meterType]
                    budget_billing = budgetDetails[0] if budgetDetails[0].get("ean") == eanNr else budgetDetails[1]

                    # This can cause issues when the final bill is due. Adding "or" to fix that.
                    remaining_months = (budget_billing.get("simulation") or {}).get("openAdvancesCount") or (12 - ((datetime.now().month - 5) % 12) + 1)
                    already_paid = (budget_billing.get("simulation")or {}).get("totalPaidAmount") or 0
                    # There is an issue because we can't get the "already paid" value when My Luminus is not allowing to adjust what we pay.
                    # Typically happening when Luminus is waiting for the "Décompte"/"Afrekening"
                    # There can be a delay of +- 6 weeks between the final bill and the moment My Luminus allows to adjust the advance payment. During that period, the "already paid" value is not available, which causes the forecast to be inaccurate. Adding "or 0" to fix that, but it can cause the forecast to be inaccurate during that period.

                    period_quantities = consumptionDetails.get("periodQuantities", {})

                    device = {
                        'device_id': eanNr,
                        'device_name': pname + ' (' + eanNr + ')',
                        'device_type': energyType,
                        'product_name': pname
                    }
                    data.append(device)

                    for propName, price in meterPrices.items():
                        device[propName] = price['rate'] / (1 if propName == 'fixed' else 100)

                    if energyType == "Gas":
                        gas_m3 = period_quantities.get("offtake", 0)
                        gas_kwh = gas_m3 * GAS_M3_TO_KWH
                        gas_price = device.get("single", 0)

                        cost_so_far = gas_kwh * gas_price
                        forecast_remaining = self._forecast_remaining_cost(cost_so_far, current_month, remaining_months, "gas")
                        projected_total = cost_so_far + forecast_remaining

                        device["estimated_cost"] = (projected_total - already_paid) / remaining_months 
                        # device["estimated_cost"] = ((gas_kwh * gas_price) - already_paid) / remaining_months
                        
                        _LOGGER.warning("Gas debug ean=%s gas_m3=%s gas_kwh=%s gas_price=%s already_paid=%s remaining_months=%s current_month=%s", eanNr, gas_m3, gas_kwh, gas_price, already_paid, remaining_months, current_month,)
                        _LOGGER.warning("Forecast debug cost_so_far=%s forecast_remaining=%s projected_total=%s estimated=%s", cost_so_far, forecast_remaining, projected_total, (projected_total - already_paid) / remaining_months if remaining_months else None,)

                    elif energyType == "Electricity":
                        day_kwh = 0
                        night_kwh = 0

                        for detail in period_quantities.get("details", []):
                            if detail.get("direction") != "Offtake":
                                continue

                            if detail.get("timeFrame") == "Day":
                                day_kwh = detail.get("quantity", 0)
                            elif detail.get("timeFrame") == "Night":
                                night_kwh = detail.get("quantity", 0)

                        device["electricity_consumption_day_kwh"] = day_kwh
                        device["electricity_consumption_night_kwh"] = night_kwh

                        day_price = 0
                        night_price = 0
                        if meterType == "dual":
                            day_price = device.get("dualDay", 0)
                            night_price = device.get("dualNight", 0)
                            
                        cost_so_far = (day_kwh * day_price) + (night_kwh * night_price)
                        forecast_remaining = self._forecast_remaining_cost(cost_so_far, current_month, remaining_months, "electricity")
                        projected_total = cost_so_far + forecast_remaining

                        device["estimated_cost"] = (projected_total - already_paid) / remaining_months    
                        # device["estimated_cost"] = ((day_kwh * day_price) + (night_kwh * night_price) - already_paid) / remaining_months
                    
            #await self.hass.async_add_executor_job(self.api.logout)
            _LOGGER.info('Data updated.')
            #_LOGGER.warning('updated coordinator data', data)

        except AuthError as err:
            # Home Assistant starts a reauthentication flow asking for fresh cookies
            raise ConfigEntryAuthFailed(f"My Luminus cookies no longer valid: {err}") from err

        except Exception as err:
            # This will show entities as unavailable by raising UpdateFailed exception.
            # The coordinator retries on its next scheduled refresh.
            raise UpdateFailed(f"Error communicating with API: {err}") from err

        finally:
            await self._async_save_cookies()

        # What is returned here is stored in self.data by the DataUpdateCoordinator
        return data

    # ----------------------------------------------------------------------------
    # Here we add some custom functions on our data coordinator to be called
    # from entity platforms to get access to the specific data they want.
    #
    # These will be specific to your api or yo may not need them at all
    # ----------------------------------------------------------------------------
    def get_device(self, device_id: int) -> dict[str, Any]:
        """Get a device entity from our api data."""
        try:
            return [
                devices for devices in self.data if devices["device_id"] == device_id
            ][0]
        except (TypeError, IndexError):
            # In this case if the device id does not exist you will get an IndexError.
            # If api did not return any data, you will get TypeError.
            return None

    def get_device_parameter(self, device_id: int, parameter: str) -> Any:
        """Get the parameter value of one of our devices from our api data."""
        if device := self.get_device(device_id):
            return device.get(parameter)
