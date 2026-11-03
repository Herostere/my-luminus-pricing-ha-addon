"""Forecast of the ideal monthly advance from actual meter readings.

Pure calculations, no I/O, so they can be tested outside Home Assistant.

Approach, per energy type, over Luminus' budget period (simulation fromDate to untilDate):
- Consumption already used: from the meter readings (customer and DSO), interpolated
  to the start of the period.
- Gas: daily use = base + k x heating degree days, fitted on the readings. The rest of
  the period uses actual and forecast temperatures where known, then the same calendar
  days of the last 10 years as weather scenarios (low/central/high).
- Electricity: average daily use per register (day/night) from the readings of the last
  year; low/high from the spread between reading intervals.
- Cost: kWh x (live energy price + network, transport and taxes from the latest
  settlements) + fixed fees pro rata, VAT included.
- Ideal advance = (projected cost - advances paid in the period) / open advances,
  the same formula Luminus uses for its own idealAdvanceAmount.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from .weather import CLIMATE_YEARS, hdd

GAS_REGISTER = "TotalHours"
# Aggregate readings into intervals of at least this many days (indexes are rounded)
MIN_INTERVAL_DAYS = 7
# Only readings from this recent past describe the current household
HISTORY_DAYS = 400
MAX_MODEL_ERROR = 0.3


@dataclass
class Reading:
    day: date
    indexes: dict[str, float]


@dataclass
class Interval:
    start: date
    end: date  # exclusive
    used: dict[str, float]

    @property
    def days(self) -> int:
        return (self.end - self.start).days


@dataclass
class Forecast:
    """Result for one energy type; amounts in EUR, VAT included."""

    ideal_advance: float | None
    ideal_advance_low: float | None
    ideal_advance_high: float | None
    projected_cost: float
    projected_cost_low: float
    projected_cost_high: float
    projected_settlement: float | None
    used_so_far: dict[str, float]
    forecast_use: dict[str, float]
    unit: str
    last_reading: date
    details: dict = field(default_factory=dict)


def _days(start: date, end: date):
    d = start
    while d < end:
        yield d
        d += timedelta(days=1)


def readings_from_api(indexes: list[dict]) -> list[Reading]:
    """Convert the 'indexes' list of the meter-readings endpoint, latest reading per day."""
    by_day: dict[date, Reading] = {}
    for entry in indexes:
        values = {i["timeOfUse"]: float(i["index"]) for i in entry.get("indexes", []) if i.get("index") is not None}
        if values:
            day = date.fromisoformat(entry["date"][:10])
            by_day[day] = Reading(day, values)
    return [by_day[d] for d in sorted(by_day)]


def intervals(readings: list[Reading], min_days: int = 1) -> list[Interval]:
    """Consecutive reading intervals, skipping meter changes (index going down or registers changing)."""
    result = []
    i = 0
    while i < len(readings) - 1:
        start = readings[i]
        j = i + 1
        while j < len(readings) and (readings[j].day - start.day).days < min_days:
            j += 1
        if j >= len(readings):
            break
        end = readings[j]
        span = readings[i:j + 1]
        valid = set(start.indexes) == set(end.indexes) and all(
            set(r.indexes) == set(start.indexes) and all(r.indexes[k] >= prev.indexes[k] for k in r.indexes)
            for prev, r in zip(span, span[1:])
        )
        if valid:
            result.append(Interval(start.day, end.day, {k: end.indexes[k] - start.indexes[k] for k in start.indexes}))
            i = j
        else:
            i += 1  # meter change inside: restart from the next reading
    return result


# ---------------------------------------------------------------------------
# Daily consumption models
# ---------------------------------------------------------------------------

@dataclass
class GasModel:
    base: float      # m³ per day regardless of temperature (hot water, cooking)
    per_hdd: float   # m³ per heating degree day
    error: float     # relative error of the fit on the readings

    def daily(self, temperature: float) -> float:
        return self.base + self.per_hdd * hdd(temperature)


def fit_gas_model(readings: list[Reading], temps: dict[date, float], today: date) -> GasModel | None:
    """Least squares fit of m³ = base x days + per_hdd x HDD over reading intervals."""
    rows = []
    for iv in intervals([r for r in readings if (today - r.day).days <= HISTORY_DAYS], MIN_INTERVAL_DAYS):
        days = list(_days(iv.start, iv.end))
        if not all(d in temps for d in days):
            continue
        rows.append((len(days), sum(hdd(temps[d]) for d in days), iv.used[GAS_REGISTER]))
    if not rows:
        return None

    sdd = sum(d * d for d, _, _ in rows)
    sdh = sum(d * h for d, h, _ in rows)
    shh = sum(h * h for _, h, _ in rows)
    sdm = sum(d * m for d, _, m in rows)
    shm = sum(h * m for _, h, m in rows)
    det = sdd * shh - sdh * sdh

    base = per_hdd = None
    if abs(det) > 1e-9:
        base = (sdm * shh - shm * sdh) / det
        per_hdd = (sdd * shm - sdh * sdm) / det
    if base is None or base < 0 or per_hdd < 0:
        # Fall back to a one-parameter fit
        if shh > 0 and shm > 0:
            base, per_hdd = 0.0, shm / shh
        else:
            base, per_hdd = (sdm / sdd if sdd else 0.0), 0.0

    residual = math.sqrt(sum((m - base * d - per_hdd * h) ** 2 for d, h, m in rows))
    total = sum(m for _, _, m in rows)
    error = min(MAX_MODEL_ERROR, residual / total) if total > 0 else MAX_MODEL_ERROR
    return GasModel(base, per_hdd, error)


def electricity_rates(readings: list[Reading], today: date) -> dict[str, tuple[float, float, float]]:
    """Per register: (low, central, high) kWh per day from the last year of readings."""
    recent = [r for r in readings if (today - r.day).days <= 365]
    ivs = intervals(recent, MIN_INTERVAL_DAYS)
    if not ivs:
        return {}
    rates = {}
    for register in ivs[-1].used:
        used = [iv for iv in ivs if register in iv.used]
        central = sum(iv.used[register] for iv in used) / sum(iv.days for iv in used)
        per_interval = sorted(iv.used[register] / iv.days for iv in used)
        low = per_interval[int(0.2 * (len(per_interval) - 1))]
        high = per_interval[math.ceil(0.8 * (len(per_interval) - 1))]
        rates[register] = (min(low, central), central, max(high, central))
    return rates


# ---------------------------------------------------------------------------
# Consumption over the budget period
# ---------------------------------------------------------------------------

def used_between(
    readings: list[Reading], start: date, end: date, weight: Callable[[date], float], estimate: Callable[[date, str], float]
) -> dict[str, float]:
    """Consumption per register in [start, end) from readings.

    Inside a reading interval the measured amount is spread over the days in
    proportion to weight(day) (expected daily use); days not covered by readings
    use estimate(day, register).
    """
    registers = set(readings[-1].indexes) if readings else set()
    used = {r: 0.0 for r in registers}
    covered: set[date] = set()
    for iv in intervals(readings):
        overlap_start, overlap_end = max(iv.start, start), min(iv.end, end)
        if overlap_start >= overlap_end:
            continue
        weights = {d: weight(d) for d in _days(iv.start, iv.end)}
        if sum(weights.values()) <= 0:
            weights = dict.fromkeys(weights, 1.0)  # no expected use: spread evenly
        share = sum(weights[d] for d in _days(overlap_start, overlap_end)) / sum(weights.values())
        for register, amount in iv.used.items():
            if register in used:
                used[register] += amount * share
        covered.update(_days(overlap_start, overlap_end))
    for d in _days(start, end):
        if d not in covered:
            for register in used:
                used[register] += estimate(d, register)
    return used


def _scenario_temperature(temps: dict[date, float], day: date, years_back: int) -> float | None:
    try:
        past = day.replace(year=day.year - years_back)
    except ValueError:  # 29 February
        past = day.replace(year=day.year - years_back, day=28)
    return temps.get(past)


def _percentiles(values: list[float]) -> tuple[float, float, float]:
    s = sorted(values)
    n = len(s) - 1
    return s[round(0.1 * n)], s[round(0.5 * n)], s[round(0.9 * n)]


def forecast_energy(
    *,
    energy: str,
    readings: list[Reading],
    temps: dict[date, float] | None,
    today: date,
    period_start: date,
    period_end: date,
    price_per_kwh: dict[str, float],
    fixed_per_year: float,
    kwh_per_unit: float,
    paid: float,
    open_advances: int,
    current_advance: float | None,
) -> Forecast | None:
    """Projected cost and ideal monthly advance for one energy type.

    price_per_kwh maps register (Day, Night, TotalHours) to the all-in price incl.
    VAT. kwh_per_unit converts the meter unit to kWh (gas m³ conversion factor, 1
    for electricity). period_end is inclusive, like Luminus' untilDate.
    """
    if not readings:
        return None
    end = period_end + timedelta(days=1)
    last = readings[-1].day
    split = min(max(last, period_start), end)
    registers = list(readings[-1].indexes)
    details: dict = {}

    if energy == "Gas":
        if not temps:
            return None
        model = fit_gas_model(readings, temps, today)
        if model is None:
            return None
        details.update(base_m3_per_day=round(model.base, 3), m3_per_degree_day=round(model.per_hdd, 4), model_error=round(model.error, 3))

        def weight(d: date) -> float:
            return model.daily(temps[d]) if d in temps else model.base + model.per_hdd * 5

        used = used_between(readings, period_start, split, weight, lambda d, _r: weight(d))

        # Remaining days: known temperatures first, then one scenario per past year
        known = sum(model.daily(temps[d]) for d in _days(split, end) if d in temps)
        unknown = [d for d in _days(split, end) if d not in temps]
        scenarios = []
        for years_back in range(1, CLIMATE_YEARS + 1):
            values = [_scenario_temperature(temps, d, years_back) for d in unknown]
            if all(v is not None for v in values):
                scenarios.append(known + sum(model.daily(v) for v in values))
        if not scenarios:
            return None
        low, central, high = _percentiles(scenarios)
        low, high = low * (1 - model.error), high * (1 + model.error)
        details["weather_scenarios"] = len(scenarios)
        forecast_low, forecast_mid, forecast_high = ({GAS_REGISTER: v} for v in (low, central, high))
    else:
        rates = electricity_rates(readings, today)
        if not rates:
            return None
        used = used_between(readings, period_start, split, lambda d: 1.0, lambda d, r: rates.get(r, (0, 0, 0))[1])
        days_left = (end - split).days
        forecast_low = {r: rates[r][0] * days_left for r in rates}
        forecast_mid = {r: rates[r][1] * days_left for r in rates}
        forecast_high = {r: rates[r][2] * days_left for r in rates}
        details["kwh_per_day"] = {r: round(v[1], 2) for r, v in rates.items()}

    period_days = (end - period_start).days
    fixed = fixed_per_year * period_days / 365

    def cost(future: dict[str, float]) -> float:
        total = fixed
        for register in registers:
            amount = used.get(register, 0.0) + future.get(register, 0.0)
            price = price_per_kwh.get(register, price_per_kwh.get(GAS_REGISTER, 0.0))
            total += amount * kwh_per_unit * price
        return total

    cost_low, cost_mid, cost_high = cost(forecast_low), cost(forecast_mid), cost(forecast_high)

    def advance(total: float) -> float | None:
        return (total - paid) / open_advances if open_advances > 0 else None

    settlement = None
    if current_advance is not None:
        settlement = cost_mid - paid - open_advances * current_advance

    details.update(fixed_costs_in_period=round(fixed, 2), period_days=period_days)
    return Forecast(
        ideal_advance=advance(cost_mid),
        ideal_advance_low=advance(cost_low),
        ideal_advance_high=advance(cost_high),
        projected_cost=cost_mid,
        projected_cost_low=cost_low,
        projected_cost_high=cost_high,
        projected_settlement=settlement,
        used_so_far={r: round(v, 1) for r, v in used.items()},
        forecast_use={r: round(v, 1) for r, v in forecast_mid.items()},
        unit="m³" if energy == "Gas" else "kWh",
        last_reading=last,
        details=details,
    )
