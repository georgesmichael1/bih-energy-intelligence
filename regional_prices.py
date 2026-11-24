"""Regional day-ahead references, never a Bosnia tariff or border trade quote.

Direct token-based A44 auction consumer for RS/HU. Owner approved public output;
source attribution and primary-owner rights remain applicable. Token is server-side.
"""
import csv
import datetime as dt
import io
import math
import json
import time
import xml.etree.ElementTree as ET
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
    """Fetch today's auction only. A separate price consumer, not the protected adapter."""
    from price_xml_parser import parse_prices
    import os
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Current time must be timezone-aware")
    token = os.environ.get("ENTSOE_API_TOKEN", "").strip()
    stamp = now.astimezone(dt.timezone.utc).isoformat()
    own_session = session is None
    session = session or requests.Session()
    results, good = [], []
    source = "https://web-api.tp.entsoe.eu/api"
    areas = {"RS": "10YCS-SERBIATSOV", "HU": "10YHU-MAVIR----U"}
    try:
        for market, (country, timezone) in MARKETS.items():
            day = now.astimezone(ZoneInfo(timezone)).date()
            try:
                if not token:
                    raise ValueError("Missing server-side token")
                start = dt.datetime.combine(day, dt.time(), ZoneInfo(timezone)).astimezone(dt.timezone.utc)
                end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), ZoneInfo(timezone)).astimezone(dt.timezone.utc)
                params = {"securityToken": token, "documentType": "A44",
                    "contract_MarketAgreement.type": "A01", "in_Domain": areas[market], "out_Domain": areas[market],
                    "periodStart": start.strftime("%Y%m%d%H%M"), "periodEnd": end.strftime("%Y%m%d%H%M")}
                for attempt in range(3):
                    try:
                        response = session.get(source, params=params, timeout=(10, 60), verify=True)
                        if response.status_code in (401, 403, 429):
                            raise ValueError("HTTP_" + str(response.status_code))
                        if response.status_code != 200:
                            if response.status_code >= 500 and attempt < 2:
                                time.sleep(5 * (attempt+1))
                                continue
                            raise ValueError("HTTP_" + str(response.status_code))
                        break
                    except (requests.Timeout, requests.ConnectionError):
                        if attempt == 2:
                            raise
                        time.sleep(5 * (attempt+1))
                parsed = parse_prices(response.content, areas[market])
                if parsed[0]["start"] != start or parsed[-1]["end"] != end:
                    raise ValueError("Wrong delivery-day boundaries")
                if any(a["end"] != b["start"] for a,b in zip(parsed, parsed[1:])):
                    raise ValueError("Delivery-day interval gap or overlap")
                rows = [{"Market": market, "Country": country, "Product": "DAY_AHEAD",
                         "Delivery_Date": day.isoformat(), "Interval_Start_UTC": row["start"].isoformat(),
                         "Interval_End_UTC": row["end"].isoformat(), "Price_EUR_MWh": row["price"],
                         "Source": source, "Attribution": "ENTSO-E Transparency Platform; primary owner: " + ("SEEPEX" if market == "RS" else "HUPX"),
                         "License": "ENTSO-E Terms of Use; primary-owner rights apply, not a blanket CC-BY grant",
                         "Fetched_At": stamp} for row in parsed]
                results.extend(rows)
                good.append(market)
                record("price_" + market.lower(), "OK", market=market, product="DAY_AHEAD", source=source,
                       delivery_days=[day.isoformat()], intervals=len(rows), fetched_at=stamp,
                       detail="Today's complete auction curve fetched at run time; not instant intraday",
                       resolution_note="ENTSO-E interval grid; RS hourly auction may repeat across quarter-hours",
                       last_successful_fetch_at=stamp)
            except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError, ET.ParseError) as exc:
                record("price_" + market.lower(), "FAILED", market=market, source=source,
                       detail="Today auction retrieval/validation failed: " + type(exc).__name__ + (" " + str(exc) if isinstance(exc, ValueError) and str(exc).startswith("HTTP_") else "") + "; cached rows not confirmed current")
    finally:
        if own_session:
            session.close()
    if results:
        prior = []
        if Path(csv_path).exists():
            with open(csv_path, newline="", encoding="utf-8") as handle:
                prior = list(csv.DictReader(handle))
        replaced = {(r["Market"], r["Delivery_Date"]) for r in results}
        cutoff = (now.date()-dt.timedelta(days=29)).isoformat()
        retained = [r for r in prior if r.get("Market") in MARKETS and r.get("Delivery_Date", "") >= cutoff
                    and (r.get("Market"),r.get("Delivery_Date")) not in replaced]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(sorted(retained + results, key=lambda r:(r["Market"],r["Interval_Start_UTC"])))
        atomic_text(csv_path, output.getvalue())
    record("dam", "OK" if len(good)==len(MARKETS) else "PARTIAL", markets=list(MARKETS),
           product="DAY_AHEAD", csv=str(csv_path), not_bosnia_tariff=True,
           detail="Today's RS/HU auction curves validated" if len(good)==len(MARKETS) else "Required current-day market missing")
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
