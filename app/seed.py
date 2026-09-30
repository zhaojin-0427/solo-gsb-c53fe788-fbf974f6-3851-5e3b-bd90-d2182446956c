"""Built-in reference emission-factor versions.

v2023-01 is the baseline; v2024-01 ships lower grid and fuel factors so the
recalculation API can demonstrate a real before/after difference.  Seed data
is only inserted when a version is absent — existing versions and the
snapshots that cite them are never rewritten.
"""

from __future__ import annotations

SEED_VERSIONS = {
    "v2023-01": [
        ("ef.natural_gas", "天然气燃烧", "MJ", "0.0561"),
        ("ef.diesel", "柴油燃烧", "L", "2.6876"),
        ("ef.grid_electricity", "华东电网电力", "kWh", "0.7035"),
        ("ef.steam", "工业蒸汽", "MJ", "0.0930"),
        ("ef.crude_oil", "原油开采与运输", "kg", "0.0800"),
        ("ef.truck_freight", "公路货运", "tkm", "0.0900"),
        ("ef.water_supply", "工业供水", "m3", "0.3000"),
    ],
    "v2024-01": [
        ("ef.natural_gas", "天然气燃烧", "MJ", "0.0561"),
        ("ef.diesel", "柴油燃烧", "L", "2.6100"),
        ("ef.grid_electricity", "华东电网电力", "kWh", "0.5703"),
        ("ef.steam", "工业蒸汽", "MJ", "0.0870"),
        ("ef.crude_oil", "原油开采与运输", "kg", "0.0750"),
        ("ef.truck_freight", "公路货运", "tkm", "0.0820"),
        ("ef.water_supply", "工业供水", "m3", "0.2800"),
    ],
}


def seed(conn) -> None:
    from . import db

    existing = set(db.list_factor_versions(conn))
    for version, factors in SEED_VERSIONS.items():
        if version in existing:
            continue
        db.upsert_factor_version(conn, version, [
            {"ref": ref, "name": name, "unit": unit,
             "kg_co2e_per_unit": value}
            for ref, name, unit, value in factors
        ])
