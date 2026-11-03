"""Network and tax tariffs read from Luminus settlement invoices.

The Luminus API does not expose distribution, transport and tax tariffs. They
are, however, itemised in the "Detail" PDF of every settlement invoice
(Décompte / Afrekening). This module extracts those lines so the forecast can
use the tariffs that actually apply, without manual yearly updates.

Only network, transport and government lines are kept, plus Luminus' green
energy cost. The energy price itself and the Luminus fixed fee come live from
the price-information endpoint.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

_LOGGER = logging.getLogger(__name__)

ELECTRICITY = "Electricity"
GAS = "Gas"

# Ignore lines from settlements whose period ended this long before the newest one,
# so tariff components that were discontinued eventually drop out.
MAX_TARIFF_AGE_DAYS = 400

_ENERGY_HEADER = re.compile(
    r"^\s*(?P<energy>Electricit[ée]|[ÉE]lectricit[ée]|Elektriciteit|Gaz|Gas)\s*:\s*D[ée]tails?", re.I
)
_PERIOD = re.compile(r"P[ée]riode\s*:\s*(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})", re.I)
_SUBSECTION = re.compile(
    r"(?:Montant à payer pour|Te betalen (?:aan|voor)|Bedrag (?:te betalen )?(?:aan|voor))\s+(?P<who>.+?)\s*\(", re.I
)
_LINE = re.compile(
    r"^(?P<label>.*?)\s*(?P<vat>\d{1,2})\s*%\s+"
    r"(?P<qty>-?[\d.]*\d(?:,\d+)?)\s+(?P<qunit>kWh|jours?|dagen|dag|m³)\s+"
    r"(?P<price>-?[\d.]*\d(?:,\d+)?)\s+€/(?P<punit>kWh|an|jaar)\b",
    re.I,
)
_VALID_FROM = re.compile(r"\((?:du|vanaf|van)\s+(\d{2}\.\d{2}\.\d{4})\)", re.I)
_CONVERSION = re.compile(r"(\d+,\d+)\s*kWh/m³")
_CODE = re.compile(r"^([EG]\d{3})\b")
_GREEN = re.compile(r"énergie verte|energie verte|groene", re.I)
_DAY = re.compile(r"heures pleines|dagtarief|piekuren|\bdag\b", re.I)
_NIGHT = re.compile(r"heures creuses|nachttarief|daluren|\bnacht\b", re.I)


def _num(text: str) -> float:
    """Parse a Belgian number: '3.257,10' -> 3257.10, '−42,23' -> -42.23."""
    return float(text.replace("−", "-").replace(".", "").replace(",", "."))


def _date(text: str) -> date:
    return datetime.strptime(text, "%d.%m.%Y").date()


def _timeframe(label: str) -> str | None:
    if _DAY.search(label):
        return "Day"
    if _NIGHT.search(label):
        return "Night"
    return None


def _key(label: str) -> str:
    """Stable identifier of a tariff component: its code (E210, G140...) or cleaned label."""
    code = _CODE.match(label.strip())
    if code:
        return code.group(1)
    cleaned = re.sub(r"\([^)]*\)", "", label)
    return re.sub(r"\s+", " ", cleaned).strip(" -")


def pdf_text(pdf: bytes) -> str:
    """Extract the text of a PDF (blocking; run in an executor)."""
    from pypdf import PdfReader  # imported lazily: only needed when a new settlement arrives

    reader = PdfReader(io.BytesIO(pdf))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def parse_settlement_text(text: str) -> dict[str, Any]:
    """Extract tariff lines per energy type from a settlement's text.

    Returns {energy: {"period_start", "period_end", "conversion_factor", "lines": [...]}}
    with dates as ISO strings, so the result can be stored as JSON.
    """
    result: dict[str, Any] = {}
    energy: str | None = None
    who = ""
    label = ""
    conversion: float | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        m = _CONVERSION.search(line)
        if m:
            conversion = _num(m.group(1))

        m = _ENERGY_HEADER.match(line)
        if m:
            energy = GAS if m.group("energy").lower().startswith("ga") else ELECTRICITY
            result.setdefault(energy, {"period_start": None, "period_end": None, "lines": []})
            who, label = "", ""
            continue

        if energy is None:
            continue
        section = result[energy]

        m = _PERIOD.search(line)
        if m and section["period_start"] is None:
            section["period_start"] = _date(m.group(1)).isoformat()
            section["period_end"] = _date(m.group(2)).isoformat()

        m = _SUBSECTION.search(line)
        if m:
            who = m.group("who").strip()
            label = ""
            continue

        m = _LINE.match(line)
        if not m:
            continue

        # A line without label continues the previous component (newer period)
        label = m.group("label").strip() or label
        if not label or not who:
            continue
        if who.lower().startswith("luminus") and not _GREEN.search(label):
            # Energy price, Luminus fixed fee and discounts come from the API instead
            continue

        valid_from = _VALID_FROM.search(line)
        section["lines"].append({
            "key": _key(label),
            "label": re.sub(r"\s*\((?:du|vanaf|van)[^)]*\)|\s*\(\d\)", "", label).strip(),
            "who": who,
            "timeframe": _timeframe(label),
            "unit": "kWh" if m.group("punit").lower() == "kwh" else "year",
            "rate": _num(m.group("price")),
            "vat": int(m.group("vat")) / 100,
            "valid_from": _date(valid_from.group(1)).isoformat() if valid_from else None,
        })

    if conversion and GAS in result:
        result[GAS]["conversion_factor"] = conversion

    # Drop sections without usable lines or period (e.g. headers in a cover letter)
    return {
        e: s for e, s in result.items()
        if s["lines"] and s["period_end"]
    }


def merge_settlements(parsed: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine parsed settlements into the latest tariff set per energy type.

    Each component (key, unit) is taken entirely from the newest settlement that
    contains it, so a change of meter regime (day/night to single rate, or the
    reverse) replaces the old lines instead of adding to them. Within that
    settlement the last line per timeframe wins, as Luminus lists older rates
    before newer ones. A component billed for a single timeframe only (e.g.
    "heures pleines" on a single-rate meter) applies to all kWh.
    """
    merged: dict[str, Any] = {}
    for energy in (ELECTRICITY, GAS):
        sections = [p[energy] for p in parsed if energy in p]
        if not sections:
            continue
        sections.sort(key=lambda s: s["period_end"])
        newest_end = date.fromisoformat(sections[-1]["period_end"])
        cutoff = newest_end - timedelta(days=MAX_TARIFF_AGE_DAYS)

        # (key, unit) -> {timeframe: line}, overwritten by each newer settlement
        components: dict[tuple, dict] = {}
        conversion = None
        for section in sections:
            if date.fromisoformat(section["period_end"]) < cutoff:
                continue
            in_section: dict[tuple, dict] = {}
            for line in section["lines"]:
                in_section.setdefault((line["key"], line["unit"]), {})[line["timeframe"]] = line
            components.update(in_section)
            conversion = section.get("conversion_factor") or conversion

        lines = []
        for by_timeframe in components.values():
            if len(by_timeframe) == 1:
                (line,) = by_timeframe.values()
                lines.append({**line, "timeframe": None})
            else:
                lines.extend(by_timeframe.values())

        merged[energy] = {
            "settlement_period_end": sections[-1]["period_end"],
            "conversion_factor": conversion,
            "lines": lines,
        }
    return merged


def price_per_kwh(tariffs: dict[str, Any], timeframe: str | None) -> float:
    """Sum of all per-kWh network, transport and tax components, VAT included."""
    total = 0.0
    for line in tariffs.get("lines", []):
        if line["unit"] != "kWh":
            continue
        if line["timeframe"] is not None and line["timeframe"] != timeframe:
            continue
        total += line["rate"] * (1 + line["vat"])
    return total


def fixed_per_year(tariffs: dict[str, Any]) -> float:
    """Sum of the fixed annual network charges, VAT included."""
    return sum(line["rate"] * (1 + line["vat"]) for line in tariffs.get("lines", []) if line["unit"] == "year")
