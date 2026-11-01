"""Seed continents and countries from the IANA tz database tables shipped with `tzdata`.

Country names/codes come from ISO 3166 (iso3166.tab); the primary time zone (used for
recipient-local sending windows) comes from zone.tab, with explicit choices for large
multi-zone countries.
"""
import os

import tzdata

from .models import Continent, Country

CONTINENTS = [
    ("AF", "Africa", 1), ("AS", "Asia", 2), ("EU", "Europe", 3),
    ("NA", "North America", 4), ("SA", "South America", 5), ("OC", "Oceania", 6),
]
SOUTH_AMERICA = {"AR", "BO", "BR", "CL", "CO", "EC", "FK", "GF", "GY", "PY", "PE", "SR", "UY", "VE"}
EXCLUDE = {"AQ", "BV", "HM", "TF", "GS", "UM"}
CONTINENT_OVERRIDES = {
    "TR": "AS", "CY": "EU", "RU": "EU", "MV": "AS", "IO": "AS", "CX": "OC", "CC": "OC",
    "IS": "EU", "FO": "EU", "BM": "NA", "CV": "AF", "SH": "AF", "FK": "SA", "SJ": "EU",
    "MU": "AF", "SC": "AF", "RE": "AF", "YT": "AF", "KM": "AF", "MG": "AF",
}
PRIMARY_TZ = {
    "US": "America/New_York", "CA": "America/Toronto", "BR": "America/Sao_Paulo", "RU": "Europe/Moscow",
    "AU": "Australia/Sydney", "MX": "America/Mexico_City", "ID": "Asia/Jakarta", "KZ": "Asia/Almaty",
    "CN": "Asia/Shanghai", "AR": "America/Argentina/Buenos_Aires", "CL": "America/Santiago",
    "ES": "Europe/Madrid", "PT": "Europe/Lisbon", "EC": "America/Guayaquil", "CD": "Africa/Kinshasa",
    "MN": "Asia/Ulaanbaatar", "UA": "Europe/Kyiv", "DE": "Europe/Berlin", "MY": "Asia/Kuala_Lumpur",
    "NZ": "Pacific/Auckland", "PG": "Pacific/Port_Moresby", "UZ": "Asia/Tashkent", "KI": "Pacific/Tarawa",
    "FM": "Pacific/Pohnpei", "PF": "Pacific/Tahiti", "GL": "America/Nuuk", "CY": "Asia/Nicosia",
}


def _tab(name):
    path = os.path.join(os.path.dirname(tzdata.__file__), "zoneinfo", name)
    with open(path, encoding="utf-8") as fh:
        return [line.rstrip("\n").split("\t") for line in fh if line.strip() and not line.startswith("#")]


def _continent_for(code: str, tz: str) -> str | None:
    if code in CONTINENT_OVERRIDES:
        return CONTINENT_OVERRIDES[code]
    if code in SOUTH_AMERICA:
        return "SA"
    prefix = tz.split("/")[0]
    return {
        "Africa": "AF", "Asia": "AS", "Europe": "EU", "America": "NA", "Australia": "OC",
        "Pacific": "OC", "Arctic": "EU", "Atlantic": "EU", "Indian": "AS",
    }.get(prefix)


def seed_countries() -> int:
    for code, name, order in CONTINENTS:
        Continent.objects.update_or_create(code=code, defaults={"name": name, "sort_order": order})
    names = {row[0]: row[1] for row in _tab("iso3166.tab")}
    first_zone = {}
    for row in _tab("zone.tab"):
        first_zone.setdefault(row[0], row[2])
    created = 0
    for code, name in names.items():
        if code in EXCLUDE or code not in first_zone:
            continue
        tz = PRIMARY_TZ.get(code, first_zone[code])
        continent = _continent_for(code, tz)
        if not continent:
            continue
        _, was_created = Country.objects.update_or_create(
            iso2=code, defaults={"name": name, "continent_id": continent, "timezone": tz}
        )
        created += was_created
    return created
