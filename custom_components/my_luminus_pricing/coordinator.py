"""DataUpdateCoordinator for our integration."""

import hashlib
import logging
from datetime import date, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import forecast as fc
from . import tariffs as tf
from .api import API, AuthError
from .const import CONF_COOKIES, DEFAULT_SCAN_INTERVAL, DOMAIN, GAS_M3_TO_KWH
from .weather import async_fetch_temperatures

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

# Meter register -> price key in the price-information response
REGISTER_PRICE_KEYS = {
    "Day": ("dualDay", "single"),
    "Night": ("dualNight", "exclusiveNight", "single"),
    fc.GAS_REGISTER: ("single",),
}


def cookie_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Storage file holding the renewed session cookies of one config entry.

    Kept outside the config entry on purpose: updating the entry's data
    triggers the update listener in __init__.py, which reloads the integration.
    """
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.cookies")


def settlement_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Storage file caching the tariff lines parsed from each settlement PDF."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.settlements")


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

        # Parsed settlements {invoice id: parsed tariffs or {"error": ...}}
        self._settlement_store = settlement_store(hass, config_entry.entry_id)
        self._settlements: dict[str, Any] = {}
        # Daily mean temperatures, refreshed once a day
        self._temps: dict[date, float] = {}
        self._temps_day: date | None = None

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
        """Load the latest renewed cookies and parsed settlements before the first refresh."""
        if not self.entry_cookies:
            raise ConfigEntryAuthFailed("No My Luminus cookies configured")

        stored = await self._store.async_load()
        same_seed = bool(stored and stored.get("seed") == self._seed and stored.get("cookies"))
        # New or re-pasted cookies: start from what the user entered
        self.api = API(stored["cookies"] if same_seed else self.entry_cookies)
        self._saved_cookies = dict(self.api.cookies) if same_seed else None

        self._settlements = (await self._settlement_store.async_load() or {}).get("parsed", {})


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


    # ----------------------------------------------------------------------------
    # Tariffs and weather
    # ----------------------------------------------------------------------------

    async def _async_update_settlements(self, current_eans: set[str]) -> dict[str, Any]:
        """Parse settlement PDFs not seen before; return the merged tariffs per energy type.

        Only settlements of the current meters (EANs) are used, so tariffs from a
        previous address, possibly with another network operator, never mix in.
        """
        invoices = await self.hass.async_add_executor_job(self.api.get_invoices)
        # invoice id -> energy types of that settlement that belong to a current meter
        current: dict[str, set[str]] = {}
        changed = False
        for invoice in invoices:
            if invoice.get("type") != "Settlement" or "Detail" not in invoice.get("documents", []):
                continue
            invoice_id = str(invoice["id"])
            energies = {
                c.get("energyType") for c in invoice.get("contractInfo", []) if c.get("ean") in current_eans
            }
            if not energies:
                continue
            current[invoice_id] = energies
            if invoice_id in self._settlements:
                continue
            try:
                pdf = await self.hass.async_add_executor_job(self.api.get_invoice_pdf, invoice_id)
                text = await self.hass.async_add_executor_job(tf.pdf_text, pdf)
                self._settlements[invoice_id] = tf.parse_settlement_text(text)
                _LOGGER.info("Read tariffs from Luminus settlement of %s", invoice.get("date", "")[:10])
            except AuthError:
                raise
            except Exception as err:  # pylint: disable=broad-except
                # Remember the failure so a broken PDF is not downloaded on every refresh
                _LOGGER.warning("Could not read tariffs from settlement %s: %s", invoice_id, err)
                self._settlements[invoice_id] = {"error": str(err)}
            changed = True
        if changed:
            await self._settlement_store.async_save({"parsed": self._settlements})

        return tf.merge_settlements([
            {energy: section for energy, section in self._settlements[invoice_id].items() if energy in energies}
            for invoice_id, energies in current.items()
            if "error" not in self._settlements.get(invoice_id, {"error": "not parsed"})
        ])


    async def _async_update_temperatures(self, today: date) -> dict[date, float]:
        """Daily temperatures from Open-Meteo, at most once a day; keeps the last result on failure."""
        if self._temps_day == today:
            return self._temps
        latitude, longitude = self.hass.config.latitude, self.hass.config.longitude
        if latitude is None or longitude is None:
            return self._temps
        try:
            self._temps = await async_fetch_temperatures(async_get_clientsession(self.hass), latitude, longitude, today)
            self._temps_day = today
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Could not fetch temperatures from Open-Meteo: %s", err)
        return self._temps


    def _update_tariff_issue(self, energy: str, available: bool) -> None:
        """Show a Repairs entry while no tariffs could be read for an energy type."""
        issue_id = f"tariffs_unavailable_{self.config_entry.entry_id}_{energy.lower()}"
        if available:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        else:
            ir.async_create_issue(
                self.hass, DOMAIN, issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="tariffs_unavailable",
                translation_placeholders={"energy": energy},
            )


    # ----------------------------------------------------------------------------
    # Forecast
    # ----------------------------------------------------------------------------

    def _forecast(
        self, energy: str, prices: dict, consumption: dict, budget: dict | None,
        tariffs: dict | None, temps: dict[date, float], today: date,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Values and attributes for the forecast sensors of one meter."""
        values: dict[str, Any] = {"ideal_monthly_advance": None, "projected_settlement": None}
        simulation = (budget or {}).get("simulation") or {}
        attributes: dict[str, Any] = {
            "luminus_ideal_advance": simulation.get("idealAdvanceAmount"),
            "luminus_projected_cost": simulation.get("totalAmount"),
            "luminus_projected_settlement": simulation.get("settlementAmount"),
            "current_advance": (budget or {}).get("advanceAmount"),
        }
        if not tariffs or not simulation.get("fromDate") or not simulation.get("untilDate"):
            attributes["status"] = "no tariffs from a settlement yet" if not tariffs else "no budget period from Luminus"
            return values, attributes

        readings = fc.readings_from_api(consumption.get("indexes", []))
        registers = list(readings[-1].indexes) if readings else []
        price_per_kwh = {}
        for register in registers:
            key = next((k for k in REGISTER_PRICE_KEYS.get(register, ("single",)) if k in prices), None)
            if key is None:
                continue
            # API rate is in c€/kWh incl. VAT; network, transport and taxes from the settlements
            price_per_kwh[register] = prices[key]["rate"] / 100 + tf.price_per_kwh(tariffs, register)
        fixed = prices.get("fixed", {}).get("rate", 0) + tf.fixed_per_year(tariffs)
        kwh_per_unit = (tariffs.get("conversion_factor") or GAS_M3_TO_KWH) if energy == "Gas" else 1.0

        result = fc.forecast_energy(
            energy=energy,
            readings=readings,
            temps=temps,
            today=today,
            period_start=date.fromisoformat(simulation["fromDate"][:10]),
            period_end=date.fromisoformat(simulation["untilDate"][:10]),
            price_per_kwh=price_per_kwh,
            fixed_per_year=fixed,
            kwh_per_unit=kwh_per_unit,
            paid=simulation.get("totalPaidAmount") or 0,
            open_advances=simulation.get("openAdvancesCount") or 0,
            current_advance=(budget or {}).get("advanceAmount"),
        )
        if result is None:
            attributes["status"] = "not enough meter readings or weather data"
            return values, attributes

        values["ideal_monthly_advance"] = round(result.ideal_advance, 2) if result.ideal_advance is not None else None
        values["projected_settlement"] = round(result.projected_settlement, 2) if result.projected_settlement is not None else None
        attributes.update({
            "status": "ok",
            "ideal_advance_low": round(result.ideal_advance_low, 2) if result.ideal_advance_low is not None else None,
            "ideal_advance_high": round(result.ideal_advance_high, 2) if result.ideal_advance_high is not None else None,
            "projected_cost": round(result.projected_cost, 2),
            "projected_cost_low": round(result.projected_cost_low, 2),
            "projected_cost_high": round(result.projected_cost_high, 2),
            "paid_in_period": simulation.get("totalPaidAmount"),
            "open_advances": simulation.get("openAdvancesCount"),
            "period_start": simulation["fromDate"][:10],
            "period_end": simulation["untilDate"][:10],
            "used_so_far": result.used_so_far,
            "forecast_rest_of_period": result.forecast_use,
            "unit": result.unit,
            "last_meter_reading": result.last_reading.isoformat(),
            "price_per_kwh_all_in": {r: round(p, 4) for r, p in price_per_kwh.items()},
            "fixed_costs_per_year": round(fixed, 2),
            "tariffs_from_settlement_until": tariffs.get("settlement_period_end"),
            **({"kwh_per_m3": kwh_per_unit} if energy == "Gas" else {}),
            **result.details,
        })
        return values, attributes


    async def async_update_data(self):
        """Fetch data from API endpoint.

        This is the place to retrieve and pre-process the data into an appropriate data structure
        to be used to provide values for all your entities.
        """
        try:
            # Renew the session first; this also detects expired cookies early
            await self.hass.async_add_executor_job(self.api.renew)
            meters = await self.hass.async_add_executor_job(self.api.get_meters)
            budgets = await self.hass.async_add_executor_job(self.api.get_advance_and_paid)
            budget_by_ean = {b.get("ean"): b for b in budgets or []}
            all_tariffs = await self._async_update_settlements({m['ean'] for m in meters['meters']})

            today = dt_util.now().date()
            temps = await self._async_update_temperatures(today)
            data = []

            for meter in meters['meters']:
                eanNr = meter['ean']
                energyType = meter['energyType']
                meterDetails = await self.hass.async_add_executor_job(self.api.get_meter, eanNr)
                consumptionDetails = await self.hass.async_add_executor_job(self.api.get_current_consumption, eanNr)
                if not meterDetails:
                    continue

                pname = meterDetails['productName']
                prices = meterDetails['prices']
                meterType = meterDetails.get('activeMeterType')
                if meterType not in prices:
                    meterType = "dual" if "dual" in prices else next(iter(prices))
                meterPrices = prices[meterType]

                device = {
                    'device_id': eanNr,
                    'device_name': pname + ' (' + eanNr + ')',
                    'device_type': energyType,
                    'product_name': pname
                }
                data.append(device)

                for propName, price in meterPrices.items():
                    device[propName] = price['rate'] / (1 if propName == 'fixed' else 100)

                tariffs = all_tariffs.get(energyType)
                self._update_tariff_issue(energyType, tariffs is not None)
                values, attributes = self._forecast(
                    energyType, meterPrices, consumptionDetails or {}, budget_by_ean.get(eanNr),
                    tariffs, temps, today,
                )
                device.update(values)
                device['attributes'] = {key: attributes for key in values}

            _LOGGER.info('Data updated.')

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
