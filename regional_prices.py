"""Regional day-ahead references, never a Bosnia tariff or border trade quote.

Only HU has a verified reusable source here. RS requires a suitable
provider agreement before their data may be written to the public repository.
"""
import csv
import datetime as dt
import io
import math
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from pipeline_status import atomic_text, record, utc_now

MARKETS = {
    "HU": ("Hungary", "Europe/Budapest"),
    "RS": ("Serbia", "Europe/Belgrade"),
}
HU_SOURCE = "https://api.energy-charts.info/price?bzn=HU"
ATTRIBUTION = "Bundesnetzagentur | SMARD.de, via Fraunhofer ISE Energy-Charts"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
COLUMNS = ["Market", "Country", "Product", "Delivery_Date", "Interval_Start_UTC",
           "Interval_End_UTC", "Price_EUR_MWh", "Source", "Attribution", "License", "Fetched_At"]


def parse_hu_day(payload, delivery_day):
    """Require one complete local delivery day, including 23/25-hour DST days."""
    license_info = str(payload.get("license_info", ""))
    if not ("CC BY 4.0" in license_info and "SMARD.de" in license_info):
        raise ValueError("HU response no longer carries the verified CC BY/SMARD licence")
    if payload.get("deprecated") is True:
        raise ValueError("HU source is deprecated")
    if payload.get("unit") not in ("EUR / MWh", "EUR/MWh"):
        raise ValueError("Unexpected HU price unit")
    times, prices = payload.get("unix_seconds"), payload.get("price")
    if not isinstance(times, list) or not isinstance(prices, list) or len(times) != len(prices):
        raise ValueError("Mismatched price and timestamp arrays")
    tz = ZoneInfo(MARKETS["HU"][1])
    start = dt.datetime.combine(delivery_day, dt.time(), tz).astimezone(dt.timezone.utc)
    end = dt.datetime.combine(delivery_day + dt.timedelta(days=1), dt.time(), tz).astimezone(dt.timezone.utc)
    interval = 900  # Hungary's native day-ahead market time unit since October 2025.
    expected = list(range(int(start.timestamp()), int(end.timestamp()), interval))
    if any(isinstance(x, bool) or not isinstance(x, int) for x in times) or times != expected:
        raise ValueError("HU delivery day has gaps, duplicates, wrong order or wrong boundaries")
    checked = []
    for value in prices:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Missing or invalid HU price")
        checked.append(float(value))  # Negative and zero prices are valid.
    return [{"Market": "HU", "Country": "Hungary", "Product": "DAY_AHEAD",
             "Delivery_Date": delivery_day.isoformat(),
             "Interval_Start_UTC": dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(),
             "Interval_End_UTC": dt.datetime.fromtimestamp(t + interval, dt.timezone.utc).isoformat(),
             "Price_EUR_MWh": p, "Source": HU_SOURCE, "Attribution": ATTRIBUTION,
             "License": LICENSE_URL}
            for t, p in zip(times, checked)]


def update_regional_prices(csv_path="regional_day_ahead_prices.csv", session=None, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Current time must be timezone-aware")
    today = now.astimezone(ZoneInfo(MARKETS["HU"][1])).date()
    stamp = utc_now()
    # Fetch only the permitted series. Never fetch restricted prices then rely on
    # callers to remember not to publish them.
    for market in ("RS",):
        record("price_" + market.lower(), "BLOCKED_RIGHTS", market=market,
               product="DAY_AHEAD", detail="Publication requires a suitable source/data agreement; no prices fetched")
    own_session = session is None
    session = session or requests.Session()
    results, errors = [], []
    try:
        for day in (today, today + dt.timedelta(days=1)):
            try:
                response = session.get(HU_SOURCE, params={"start": day.isoformat(), "end": day.isoformat()},
                                       timeout=(10, 20), verify=True)
                response.raise_for_status()
                rows = parse_hu_day(response.json(), day)
                for row in rows:
                    row["Fetched_At"] = stamp
                results.extend(rows)
            except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
                errors.append({"delivery_day": day.isoformat(), "kind": type(exc).__name__})
    finally:
        if own_session:
            session.close()
    current = [r for r in results if r["Delivery_Date"] == today.isoformat()]
    if results:
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=COLUMNS)
        writer.writeheader()
        prior = []
        if Path(csv_path).exists():
            with open(csv_path, newline="", encoding="utf-8") as handle:
                prior = list(csv.DictReader(handle))
        replaced = {r["Delivery_Date"] for r in results}
        cutoff = (today - dt.timedelta(days=29)).isoformat()
        retained = [r for r in prior if r.get("Market") == "HU"
                    and r.get("Delivery_Date", "") >= cutoff
                    and r.get("Delivery_Date") not in replaced]
        writer.writerows(sorted(retained + results, key=lambda r: r["Interval_Start_UTC"]))
        atomic_text(csv_path, output.getvalue())
    # Tomorrow may not have cleared yet. That is not a failure of today's curve.
    # Failing today's fetch never becomes OK merely because tomorrow was returned.
    record("price_hu", "OK" if current else "FAILED", market="HU", product="DAY_AHEAD",
           detail="Latest published Hungarian day-ahead intervals; not an intraday quote" if current
                  else "No complete current delivery-day curve; any saved rows retain their delivery dates",
           source=HU_SOURCE, attribution=ATTRIBUTION, license=LICENSE_URL,
           delivery_days=sorted({r["Delivery_Date"] for r in results}), errors=errors,
           last_successful_fetch_at=stamp if current else None,
           licence_note="SMARD currently grants CC BY 4.0; wholesale-price licensing is under HEDWIG review")
    record("dam", "PARTIAL", detail="Regional references: HU permitted; RS blocked on publication rights; Montenegro out of scope",
           markets=["RS", "HU"], product="DAY_AHEAD",
           not_bosnia_tariff=True, csv=csv_path)
    return results


def mark_legacy_master(path="bih_data.csv"):
    """Keep the saved RS number, but never advertise it as a current dual feed."""
    path = Path(path)
    if not path.exists():
        return
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns, rows = reader.fieldnames, list(reader)
    changed = False
    for row in rows:
        if row.get("Record_ID") != "BIH-0487":
            continue
        labels = {
            "Subcategory_Group": "Table 2B: Serbia Day-Ahead Historical Reference (SEEPEX)",
            "Entity_Project_Indicator_Name": "Serbia Day-Ahead Saved Reference (SEEPEX)",
            "Location_Region_Entity": "Serbia (RS), not Bosnia or Hungary",
            "Status_Stage_Tier": "HISTORICAL SAVED VALUE - LIVE UPDATE BLOCKED",
            "Period_Year": "Historical snapshot; see original source date",
            "Primary_Metric_Label": "Saved legacy regional price (EUR/MWh); not revalidated",
            "Secondary_Metric_Label": "Saved legacy metrics; not revalidated",
            "Counterparty_Owner_Offtaker": "SEEPEX / Energy-Charts (RS zone)",
            "Technical_Commercial_Notes_Details": "Original RS-only legacy value retained, not a current verified delivery-day average. RS publication updates blocked pending suitable rights. HU intervals are separate in regional_day_ahead_prices.csv. Domestic tariffs are reference data.",
        }
        source = row.get("Verification_Status_Source_URL", "")
        if source and not source.startswith("HISTORICAL SAVED LABEL"):
            labels["Verification_Status_Source_URL"] = "HISTORICAL SAVED LABEL (not revalidated): " + source
        for key, value in labels.items():
            if key in row and row[key] != value:
                row[key] = value
                changed = True
    if changed:
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        atomic_text(path, output.getvalue())


def main():
    # Preserve the existing updater's dataset results and legacy VBA metadata.
    from pipeline_status import DATASETS, write_status
    path = Path("status.json")
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    DATASETS.clear()
    DATASETS.update(old.get("dataset_details", {}))
    DATASETS.pop("price_me", None)
    update_regional_prices()
    mark_legacy_master()
    reserved = {"schema_version", "run_finished_at", "last_verified_date",
                "last_verified_date_is_legacy", "verification_note", "rows",
                "datasets", "dataset_details"}
    legacy = {k: v for k, v in old.items() if k not in reserved}
    write_status(path, rows=old.get("rows"), legacy=legacy)


if __name__ == "__main__":
    main()
