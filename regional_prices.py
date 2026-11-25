"""Regional day-ahead references, never a Bosnia tariff or border trade quote.

Direct token-based A44 auction consumer for RS/HU/HR/ME. Owner approved public output;
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
    "HR": ("Croatia", "Europe/Zagreb"),
    "ME": ("Montenegro", "Europe/Podgorica"),
}
# ENTSO-E TP bidding-zone EIC codes, as selected on the TP price pages (observed 24 Nov 2026).
AREAS = {"RS": "10YCS-SERBIATSOV", "HU": "10YHU-MAVIR----U",
         "HR": "10YHR-HEP------M", "ME": "10YCS-CG-TSO---S"}
OWNERS = {"RS": "SEEPEX", "HU": "HUPX",
          "HR": "not named on the TP page (bidding zone HR)",
          "ME": "not named on the TP page (bidding zone ME)"}
HU_SOURCE = "https://api.energy-charts.info/price?bzn=HU"
HUPX_SOURCE = "https://labs.hupx.hu/data/v1/dam_aggregated_trading_data_15min"
HUPX_ATTRIBUTION = "HUPX Hungarian Power Exchange - Labs DAM aggregated trading data, 15-minute MTU"
HUPX_LICENSE_URL = "https://labs.hupx.hu/about_us/terms_of_use"
PRIMARY_MARKETS = ("HU", "RS")
EXTRA_MARKETS = ("HR", "ME")


def extra_csv_path(csv_path):
    path = Path(csv_path)
    return str(path.with_name(path.stem + "_hr_me" + path.suffix))


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


def _fetch_market_day(session, token, market, day, stamp, areas, source):
    """One complete validated local delivery day for a market, or an exception. Never partial."""
    from price_xml_parser import parse_prices
    country, timezone = MARKETS[market]
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
    return [{"Market": market, "Country": country, "Product": "DAY_AHEAD",
             "Delivery_Date": day.isoformat(), "Interval_Start_UTC": row["start"].isoformat(),
             "Interval_End_UTC": row["end"].isoformat(), "Price_EUR_MWh": row["price"],
             "Source": source, "Attribution": "ENTSO-E Transparency Platform; primary owner: " + OWNERS[market],
             "License": "ENTSO-E Terms of Use; primary-owner rights apply, not a blanket CC-BY grant",
             "Fetched_At": stamp} for row in parsed]


def parse_hupx_day(rows, delivery_day):
    """Strictly validated HU local delivery day from HUPX Labs (quarter-hour MTU):
    complete 1..N quarter-hour sequence (92/96/100 for 23/24/25-hour days),
    Region HU only, every row Status final, every price finite."""
    timezone = MARKETS["HU"][1]
    tz = ZoneInfo(timezone)
    start = dt.datetime.combine(delivery_day, dt.time(), tz).astimezone(dt.timezone.utc)
    end = dt.datetime.combine(delivery_day + dt.timedelta(days=1), dt.time(), tz).astimezone(dt.timezone.utc)
    expected = int((end - start).total_seconds() // 900)
    if len(rows) != expected:
        raise ValueError("HUPX day holds " + str(len(rows)) + " rows, expected " + str(expected))
    for row in rows:
        if row.get("Region") != "HU":
            raise ValueError("Unexpected region in HUPX rows")
        if str(row.get("Status", "")).lower() != "final":
            raise ValueError("HUPX day holds non-final rows")
    try:
        order = [int(row["ProductQH"]) for row in rows]
    except (KeyError, TypeError, ValueError):
        raise ValueError("HUPX quarter-hour labels unreadable") from None
    if sorted(order) != list(range(1, expected + 1)):
        raise ValueError("HUPX quarter-hours are not a complete 1..N sequence")
    by_qh = {int(row["ProductQH"]): row for row in rows}
    out = []
    for qh in range(1, expected + 1):
        value = by_qh[qh].get("Price")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Missing or invalid HUPX price")
        t0 = start + dt.timedelta(seconds=900 * (qh - 1))
        out.append({"Market": "HU", "Country": MARKETS["HU"][0], "Product": "DAY_AHEAD",
                    "Delivery_Date": delivery_day.isoformat(),
                    "Interval_Start_UTC": t0.isoformat(),
                    "Interval_End_UTC": (t0 + dt.timedelta(seconds=900)).isoformat(),
                    "Price_EUR_MWh": float(value), "Source": HUPX_SOURCE,
                    "Attribution": HUPX_ATTRIBUTION, "License": HUPX_LICENSE_URL,
                    "Fetched_At": utc_now()})
    return out


def fetch_hupx_day(session, delivery_day):
    """One delivery day of HU prices from HUPX Labs; raises on any invalid day."""
    params = {"filter": "DeliveryDay__gte__" + delivery_day.isoformat()
                        + ",DeliveryDay__lte__" + delivery_day.isoformat() + ",Region__in__HU"}
    response = session.get(HUPX_SOURCE, params=params, timeout=(10, 60))
    if response.status_code != 200:
        raise ValueError("HTTP_" + str(response.status_code))
    rows = response.json().get("data")
    if not isinstance(rows, list):
        raise ValueError("HUPX response has no data array")
    return parse_hupx_day(rows, delivery_day)


def _fetch_day_with_fallback(session, token, market, day, stamp, areas, source):
    """ENTSO-E A44 first; HU days fall back to the primary owner's own publication."""
    try:
        return _fetch_market_day(session, token, market, day, stamp, areas, source)
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError, ET.ParseError):
        if market != "HU":
            raise
        return fetch_hupx_day(session, day)


def _saved_complete_days(prior, market, days):
    """Days whose saved rows span the whole local delivery day without gaps."""
    timezone = MARKETS[market][1]
    done = set()
    for day in days:
        rows = sorted((r for r in prior if r.get("Market") == market and r.get("Delivery_Date") == day.isoformat()),
                      key=lambda r: r["Interval_Start_UTC"])
        start = dt.datetime.combine(day, dt.time(), ZoneInfo(timezone)).astimezone(dt.timezone.utc)
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), ZoneInfo(timezone)).astimezone(dt.timezone.utc)
        try:
            if (rows and dt.datetime.fromisoformat(rows[0]["Interval_Start_UTC"]) == start
                    and dt.datetime.fromisoformat(rows[-1]["Interval_End_UTC"]) == end
                    and all(a["Interval_End_UTC"] == b["Interval_Start_UTC"] for a, b in zip(rows, rows[1:]))
                    and all(r.get("Fetched_At") for r in rows)):
                done.add(day)
        except ValueError:
            pass
    return done


# Days confirmed absent on the ENTSO-E TP page itself (rendered page said "No data found").
# A day counts as a verified gap only while the API ALSO answers with no price series for it:
# any other failure (HTTP error, timeout, parse error) stays PARTIAL, and if data ever appears it is
# fetched and saved normally. Values are left empty, never zero.
VERIFIED_SOURCE_GAPS = {
    ('HR', '2026-11-21'): {
        'checked_at': '2026-11-25T05:52:00+00:00',
        'evidence': 'TP Energy Prices (TR 12.1.D), bidding zone HR 10YHR-HEP------M, 21/11/2026: "No data found."; '
                    'same page showed day-ahead rows for 20/11/2026',
        'evidence_url': 'https://transparency.entsoe.eu/market/energyPrices?appState=%7B%22sa%22%3A%5B%22BZN%7C10YHR-HEP------M%22%5D%2C%22st%22%3A%22BZN%22%2C%22mm%22%3Atrue%2C%22ma%22%3Afalse%2C%22sp%22%3A%22HALF%22%2C%22dt%22%3A%22TABLE%22%2C%22df%22%3A%222026-11-21%22%2C%22tz%22%3A%22CET%22%2C%22ii%22%3Anull%2C%22ps%22%3Anull%7D',
    },
}
BACKFILL_SECONDS_PER_RUN = 150  # per market (4 markets, 15 min job): about 29 single-day requests fit


def update_regional_prices(csv_path="regional_day_ahead_prices.csv", session=None, now=None):
    """Always refresh today's auction; also recover incomplete earlier days in the 31-day local window
    (all of them, within a per-market time budget). Days save only when complete."""
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
    areas = AREAS
    prior = []
    extra_path = extra_csv_path(csv_path)
    for source_path in (csv_path, extra_path):
        if Path(source_path).exists():
            with open(source_path, newline="", encoding="utf-8") as handle:
                prior.extend(csv.DictReader(handle))
    try:
        for market, (country, timezone) in MARKETS.items():
            day = now.astimezone(ZoneInfo(timezone)).date()
            try:
                rows = _fetch_day_with_fallback(session, token, market, day, stamp, areas, source)
                results.extend(rows)
                good.append(market)
                older = [day - dt.timedelta(days=n) for n in range(1, 31)]
                have = _saved_complete_days(prior, market, older)
                all_missing = [d for d in older if d not in have]
                # Every missing day is attempted, bounded by a time budget instead of a day count.
                # Newest 5 first, then the rest rotated by half-hour slot, so a budget stop can never
                # leave the same days unattempted run after run.
                head, rest = all_missing[:5], all_missing[5:]
                k = (now.hour * 2 + now.minute // 30) % len(rest) if rest else 0
                missing = head + rest[k:] + rest[:k]
                deadline = time.monotonic() + BACKFILL_SECONDS_PER_RUN
                recovered, unavailable, reasons, no_data = [], [], {}, []
                for d in missing:
                    if time.monotonic() > deadline:
                        break
                    try:
                        results.extend(_fetch_day_with_fallback(session, token, market, d, stamp, areas, source))
                        recovered.append(d.isoformat())
                    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError, ET.ParseError) as exc:
                        unavailable.append(d.isoformat())
                        # Only our own fixed ValueError texts are kept; request errors can carry the URL.
                        if type(exc) is ValueError and str(exc) == 'No price series returned':
                            no_data.append(d.isoformat())  # source answered, but published no series for that day
                        reasons[d.isoformat()] = type(exc).__name__ + (": " + str(exc)[:80] if type(exc) is ValueError else "")
                    time.sleep(1)
                still_missing = [d for d in older if d not in have and d.isoformat() not in recovered]
                # An empty API answer alone is not proof of absence: only days also verified on the TP page count.
                verified_gaps = [d for d in no_data if (market, d) in VERIFIED_SOURCE_GAPS]
                blocking = [d for d in still_missing if d.isoformat() not in verified_gaps]
                state = "OK" if not blocking else "PARTIAL"
                record("price_" + market.lower(), state, market=market, product="DAY_AHEAD", source=source,
                       delivery_days=[day.isoformat()], intervals=len(rows), fetched_at=stamp,
                       recovered_days=recovered, source_no_data_days=no_data,
                       verified_source_gaps=[dict(date=d, **VERIFIED_SOURCE_GAPS[(market, d)]) for d in verified_gaps], unavailable_reasons=reasons, attempted_days=[d.isoformat() for d in missing], incomplete_days_remaining=[d.isoformat() for d in still_missing],
                       detail="Today's complete auction curve fetched at run time; not instant intraday"
                              + ("" if not blocking else "; " + str(len(blocking)) + " earlier day(s) in the 31-day window still missing, never filled with zeros")
                              + ("" if not verified_gaps else "; verified source gap(s), left empty and retried every run: " + ",".join(verified_gaps))
                              + ("" if not [d for d in no_data if d not in verified_gaps] else "; API returned no price series for " + ",".join(d for d in no_data if d not in verified_gaps) + " (not yet verified on the TP page)")
                              + ("; HU day(s) from HUPX Labs: " + ",".join(sorted({r["Delivery_Date"] for r in results
                                  if r["Market"] == "HU" and r["Source"] == HUPX_SOURCE}))
                                 if market == "HU" and any(r["Market"] == "HU" and r["Source"] == HUPX_SOURCE for r in results) else ""),
                       resolution_note="ENTSO-E interval grid; RS hourly auction may repeat across quarter-hours",
                       last_successful_fetch_at=stamp)
                if blocking:
                    good.pop()
            except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError, ET.ParseError) as exc:
                record("price_" + market.lower(), "FAILED", market=market, source=source,
                       detail="Today auction retrieval/validation failed: " + type(exc).__name__ + (" " + str(exc) if isinstance(exc, ValueError) and str(exc).startswith("HTTP_") else "") + "; cached rows not confirmed current")
    finally:
        if own_session:
            session.close()
    if results:
        replaced = {(r["Market"], r["Delivery_Date"]) for r in results}
        cutoff = (now.date()-dt.timedelta(days=30)).isoformat()
        retained = [r for r in prior if r.get("Market") in MARKETS and r.get("Delivery_Date", "") >= cutoff
                    and (r.get("Market"),r.get("Delivery_Date")) not in replaced]
        # HU/RS stay in the original file so existing workbooks keep working; HR/ME go to a separate file.
        for path, group in ((csv_path, PRIMARY_MARKETS), (extra_path, EXTRA_MARKETS)):
            if not any(r["Market"] in group for r in results):
                continue
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(sorted([r for r in retained + results if r["Market"] in group],
                                    key=lambda r:(r["Market"],r["Interval_Start_UTC"])))
            atomic_text(path, output.getvalue())
    record("dam", "OK" if len(good)==len(MARKETS) else "PARTIAL", markets=list(MARKETS),
           product="DAY_AHEAD", csv=str(csv_path), not_bosnia_tariff=True,
           detail="Today's RS/HU/HR/ME auction curves validated; 31-day window complete" if len(good)==len(MARKETS) else "Required current-day market or window days missing")
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
    update_regional_prices()
    mark_legacy_master()
    reserved = {"schema_version", "run_finished_at", "last_verified_date",
                "last_verified_date_is_legacy", "verification_note", "rows",
                "datasets", "dataset_details"}
    legacy = {k: v for k, v in old.items() if k not in reserved}
    write_status(path, rows=old.get("rows"), legacy=legacy)


if __name__ == "__main__":
    main()
