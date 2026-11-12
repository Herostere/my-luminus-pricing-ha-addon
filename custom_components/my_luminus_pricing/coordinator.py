"""DataUpdateCoordinator for our integration."""

import logging
from datetime import date, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import forecast as fc
from . import tariffs as tf
from .api import API, AuthError, VerificationRequired
from .const import CONF_REFRESH_TOKEN, DEFAULT_SCAN_INTERVAL, DOMAIN, GAS_M3_TO_KWH
from .weather import async_fetch_temperatures

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

# Invoice EnergyType (mobile API) -> energy types of the meters it covers
# Mobile API (GetAccountStatement, retired by Luminus): invoice EnergyType -> energy types
# INVOICE_ENERGIES = {
#     "Electricity": {"Electricity"},
#     "Gas": {"Gas"},
#     "ElectricityAndGas": {"Electricity", "Gas"},
# }

# Meter register -> price key in the pricing response
REGISTER_PRICE_KEYS = {
    "Day": ("dualDay", "single"),
    "Night": ("dualNight", "exclusiveNight", "single"),
    fc.GAS_REGISTER: ("single",),
}


def _registers(consumption: dict[str, Any] | None) -> set[str]:
    """Offtake registers (Day, Night, TotalHours, ...) seen in the meter readings."""
    consumption = consumption or {}
    registers = {
        d.get("timeFrame")
        for d in (consumption.get("periodQuantities") or {}).get("details", [])
        if d.get("direction") == "Offtake"
    }
    for reading in consumption.get("indexes", []):
        registers.update(
            i.get("timeOfUse") for i in reading.get("indexes", []) if i.get("direction") == "Offtake"
        )
    registers.discard(None)
    return registers


def _meter_type(meter: dict[str, Any], consumption: dict[str, Any] | None) -> str:
    """Price set matching the meter.

    activeMeterType is not reliable: Luminus reports "single" for meters whose
    readings have separate Day and Night registers. Day/Night readings therefore
    select the dual prices; otherwise activeMeterType is used.
    """
    prices = meter["prices"]
    if {"Day", "Night"} <= _registers(consumption) and "dual" in prices:
        return "dual"
    meter_type = meter.get("activeMeterType")
    if meter_type in prices:
        return meter_type
    return "dual" if "dual" in prices else next(iter(prices))


def token_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Storage file holding the Luminus refresh token of one config entry.

    Kept outside the config entry on purpose: updating the entry's data
    triggers the update listener in __init__.py, which reloads the integration.
    """
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.token")


def cookie_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Storage file of the former browser-cookie login; only used to delete it."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.cookies")


def settlement_store(hass: HomeAssistant, entry_id: str) -> Store:
    """Storage file caching the tariff lines parsed from each settlement PDF."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.settlements")


class LuminusCoordinator(DataUpdateCoordinator):
    """My example coordinator."""

    data: list[dict[str, Any]]

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize coordinator."""

        # Credentials from the config flow; the refresh token is kept in self._store
        self.username = config_entry.data.get(CONF_USERNAME, "")
        self.password = config_entry.data.get(CONF_PASSWORD, "")
        # Token from the config flow login; identifies which stored token descends from it
        self.entry_token = config_entry.data.get(CONF_REFRESH_TOKEN)
        self._store = token_store(hass, config_entry.entry_id)
        self._saved_refresh_token: str | None = None
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
        """Load the saved refresh token and parsed settlements before the first refresh."""
        if not self.username or not self.password:
            # Entries migrated from the browser-cookie login: ask for email and password
            raise ConfigEntryAuthFailed("Log in to My Luminus with your email and password")

        stored = await self._store.async_load() or {}
        # The stored token is newer than the config flow's, unless the user logged in
        # again since (new entry token) or changed account
        same_login = stored.get("username") == self.username and stored.get("entry_token") == self.entry_token
        refresh_token = stored.get("refresh_token") if same_login else self.entry_token
        self.api = API(self.username, self.password, refresh_token)
        self._saved_refresh_token = refresh_token if same_login else None

        self._settlements = (await self._settlement_store.async_load() or {}).get("parsed", {})


    async def _async_save_token(self) -> None:
        """Persist the refresh token whenever Luminus issues a new one."""
        if self.api is None or self.api.refresh_token == self._saved_refresh_token:
            return
        await self._store.async_save({
            "username": self.username,
            "entry_token": self.entry_token,
            "refresh_token": self.api.refresh_token,
        })
        self._saved_refresh_token = self.api.refresh_token


    # ----------------------------------------------------------------------------
    # Tariffs and weather
    # ----------------------------------------------------------------------------

    async def _async_update_settlements(self, current_eans: set[str], current_energies: set[str]) -> dict[str, Any]:
        """Parse settlement PDFs not seen before; return the merged tariffs per energy type.

        The account statement lists the EAN and energy type of every contract on
        an invoice, so only settlements of the current meters are used. When the
        invoice list cannot be read, the settlements parsed before are used,
        matched by energy type; merge_settlements keeps the newest settlement per
        tariff component.
        """
        # Website version, matched settlements to the current meters by EAN:
        # invoices = await self.hass.async_add_executor_job(self.api.get_invoices)
        # for invoice in invoices:
        #     if invoice.get("type") != "Settlement" or "Detail" not in invoice.get("documents", []):
        #         continue
        #     invoice_id = str(invoice["id"])
        #     energies = {
        #         c.get("energyType") for c in invoice.get("contractInfo", []) if c.get("ean") in current_eans
        #     }
        try:
            invoices = await self.hass.async_add_executor_job(self.api.get_invoices)
        except AuthError:
            raise
        except Exception as err:  # pylint: disable=broad-except
            # Keep the integration running on the settlements read before (the invoice
            # list endpoint can disappear when Luminus changes its app API)
            _LOGGER.warning("Could not list Luminus invoices, using the settlements read before: %s", err)
            return tf.merge_settlements([
                {energy: section for energy, section in parsed.items() if energy in current_energies}
                for parsed in self._settlements.values()
                if "error" not in parsed
            ])
        # Mobile API version (GetAccountStatement, no EANs, matched by energy type only):
        # for invoice in invoices:
        #     if invoice.get("Category") != "Settlement" or not invoice.get("DownloadDetailAvailable"):
        #         continue
        #     invoice_id = str(invoice.get("DocumentNumber") or "")
        #     energies = INVOICE_ENERGIES.get(invoice.get("EnergyType"), set()) & current_energies

        # Drop settlements that are no longer on the account statement (for example
        # ones stored under the document numbers of the retired mobile API). An empty
        # list is not trusted, so a bad response cannot wipe the stored settlements.
        listed = {str(invoice.get("id")) for invoice in invoices if invoice.get("id")}
        stale = [invoice_id for invoice_id in self._settlements if listed and invoice_id not in listed]
        for invoice_id in stale:
            del self._settlements[invoice_id]
        changed = bool(stale)

        # invoice id -> energy types of that settlement that belong to a current meter
        current: dict[str, set[str]] = {}
        for invoice in invoices:
            if invoice.get("type") != "Settlement" or "Detail" not in (invoice.get("documents") or []):
                continue
            invoice_id = str(invoice.get("id") or "")
            energies = {
                c.get("energyType") for c in invoice.get("contractInfo") or [] if c.get("ean") in current_eans
            }
            if not invoice_id or not energies:
                continue
            current[invoice_id] = energies
            if invoice_id in self._settlements:
                continue
            try:
                pdf = await self.hass.async_add_executor_job(self.api.get_invoice_pdf, invoice_id)
                text = await self.hass.async_add_executor_job(tf.pdf_text, pdf)
                self._settlements[invoice_id] = tf.parse_settlement_text(text)
                _LOGGER.info("Read tariffs from Luminus settlement of %s", str(invoice.get("date") or "")[:10])
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
            if not tariffs:
                attributes["status"] = "no tariffs from a settlement yet"
            elif budget is None:
                attributes["status"] = "no budget-billing data from Luminus for this meter"
            else:
                attributes["status"] = "no budget period from Luminus"
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
            meters = await self.hass.async_add_executor_job(self.api.get_meters)
            budgets = await self.hass.async_add_executor_job(self.api.get_advance_and_paid)
            budget_by_ean = {
                b.get("ean"): b for b in (budgets if isinstance(budgets, list) else []) if isinstance(b, dict)
            }
            missing = {m['ean'] for m in meters['meters']} - set(budget_by_ean)
            if missing:
                # Diagnostics: structure only, no amounts or personal data
                shape = (
                    f"list of {len(budgets)}" if isinstance(budgets, list)
                    else f"{type(budgets).__name__} with keys {sorted(budgets)}" if isinstance(budgets, dict)
                    else type(budgets).__name__
                )
                _LOGGER.warning(
                    "Luminus budget-billing has no entry for EAN(s) %s; response: %s",
                    ", ".join(sorted(missing)), shape,
                )
            all_tariffs = await self._async_update_settlements(
                {m['ean'] for m in meters['meters']},
                {m['energyType'] for m in meters['meters']},
            )

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
                meterType = _meter_type(meterDetails, consumptionDetails)
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

        except VerificationRequired as err:
            # Retrying would only repeat the challenge: ask the user instead
            _LOGGER.error("Luminus asks for extra verification of the login (%s); log in again later", err)
            raise ConfigEntryAuthFailed(f"My Luminus login needs verification: {err}") from err

        except AuthError as err:
            # Home Assistant starts a reauthentication flow asking for the email and password
            _LOGGER.error("Luminus rejected the saved email and password (%s)", err)
            raise ConfigEntryAuthFailed(f"My Luminus login failed: {err}") from err

        except Exception as err:
            # This will show entities as unavailable by raising UpdateFailed exception.
            # The coordinator retries on its next scheduled refresh.
            raise UpdateFailed(f"Error communicating with API: {err}") from err

        finally:
            await self._async_save_token()

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
