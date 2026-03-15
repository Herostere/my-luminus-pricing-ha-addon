"""DataUpdateCoordinator for our integration."""

from datetime import timedelta, datetime
from typing import Any
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    #CONF_HOST,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import DOMAIN, HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import API, APIConnectionError
from .const import DEFAULT_SCAN_INTERVAL, USE_MOCK_DATA, GAS_M3_TO_KWH
import logging

_LOGGER = logging.getLogger(__name__)

class LuminusCoordinator(DataUpdateCoordinator):
    """My example coordinator."""

    data: list[dict[str, Any]]

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize coordinator."""

        # Set variables from values entered in config flow setup
        self.user = config_entry.data[CONF_USERNAME]
        self.pwd = config_entry.data[CONF_PASSWORD]

        # set variables from options.  You need a default here in case options have not been set
        self.poll_interval = config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )

        # Initialise DataUpdateCoordinator
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({config_entry.unique_id})",
            # Method to call on every update interval.
            update_method=self.async_update_data,
            # Polling interval. Will only be polled if you have made your
            # platform entities, CoordinatorEntities.
            # Using config option here but you can just use a fixed value.
            update_interval=timedelta(seconds=self.poll_interval),
        )

        # Initialise your api here and make available to your integration.
        self.api = API(user=self.user, pwd=self.pwd, mock=USE_MOCK_DATA)

    async def async_update_data(self):
        """Fetch data from API endpoint.

        This is the place to retrieve and pre-process the data into an appropriate data structure
        to be used to provide values for all your entities.
        """
        try:
            # ----------------------------------------------------------------------------
            # Get the data from your api
            # NOTE: Change this to use a real api call for data
            # ----------------------------------------------------------------------------
            
            await self.hass.async_add_executor_job(self.api.login)
            meters = await self.hass.async_add_executor_job(self.api.get_meters)
            data = []

            current_month = datetime.now().month - 5 if datetime.now().month > 4 else datetime.now().month + 7
            remaining_months = 12 - current_month

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
                    already_paid = budget_billing.get("simulation").get("totalPaidAmount")
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

                        device["estimated_cost"] = ((gas_kwh * gas_price) - already_paid) / remaining_months

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
                            
                        device["estimated_cost"] = ((day_kwh * day_price) + (night_kwh * night_price) - already_paid) / remaining_months
                    
            #await self.hass.async_add_executor_job(self.api.logout)
            _LOGGER.info('Data updated.')
            #_LOGGER.warning('updated coordinator data', data)  

        except APIConnectionError as err:
            _LOGGER.error(err)
            raise UpdateFailed(err) from err
        except Exception as err:
            # This will show entities as unavailable by raising UpdateFailed exception
            raise UpdateFailed(f"Error communicating with API: {err}") from err

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
