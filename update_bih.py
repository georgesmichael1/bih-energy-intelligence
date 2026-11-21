#!/usr/bin/env python3
"""
BiH Energy Workbook - Daily Auto-Updater (update_bih.py)  [v2]
==============================================================
Maintains `bih_data.csv`, `change_log.csv` and `status.json` for the Excel/VBA
dashboard.

What changed vs v1
------------------
* Official sources only. Every download is checked against ALLOWED_HOSTS and
  refused otherwise. Frankfurter (.dev) was removed.
* FX: the spot label now uses the ECB observation date (not "today", which was
  wrong on weekends) and the 2026 YTD average is now really recomputed from the
  ECB daily series instead of staying frozen.
* IMF check tries the current DataMapper API (v2) first, and a failure is now a
  WARN (non-critical) instead of a red run, because it only matters twice a year
  (IMF WEO is published in April and October).
* status.json dataset values are: SUCCESS | WARN: ... | FAILED: ...
  The GitHub workflow turns the run red only on FAILED.

Things the Excel/VBA code depends on (DO NOT CHANGE)
----------------------------------------------------
* 30 columns, exact header order, 486/487 rows, unique Record_IDs.
* BIH-0128 Primary_Value format: "1.6710 (Avg) / 1.7796 (Nov 21 Spot)".
* BIH-0487 Primary_Value is a plain number.
* status.json key "last_verified_date".

Protected domains (3, 7, 9) are never overwritten.
"""

import csv
import datetime
import hashlib
import io
import json
import os
import re
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

CSV_PATH = os.environ.get("BIH_CSV_PATH", "bih_data.csv")
CHANGELOG_PATH = os.environ.get("BIH_CHANGELOG_PATH", "change_log.csv")
STATUS_PATH = os.environ.get("BIH_STATUS_PATH", "status.json")
CACHE_DIR = os.environ.get("BIH_CACHE_DIR", "raw_cache")

BAM_PER_EUR_PEG = 1.95583  # CBBH currency board peg (fixed by law)

# Only these hosts may be contacted. Add a host here only if it is an official
# / national / internationally recognised source.
ALLOWED_HOSTS = {
    "data-api.ecb.europa.eu",   # European Central Bank - FX reference rates
    "www.ecb.europa.eu",        # ECB - daily reference rate file (fallback)
    "www.imf.org",              # International Monetary Fund - DataMapper
    "api.worldbank.org",        # World Bank
    "www.cbbh.ba",              # Central Bank of BiH
    "www.nosbih.ba",            # NOSBiH (grid operator) - watchdog only
    "www.derk.ba",              # DERK (state regulator) - watchdog only
    "www.ferk.ba",              # FERK (FBiH regulator) - watchdog only
    # TEMPORARY - Fraunhofer ISE Energy-Charts, used for BIH-0487 only.
    # To be replaced by an official power-exchange / ENTSO-E source.
    "api.energy-charts.info",
}

EXPECTED_HEADERS = [
    "Record_ID", "Source_Workbook", "Source_Sheet", "Domain_Category",
    "Subcategory_Group", "Entity_Project_Indicator_Name", "Location_Region_Entity",
    "Technology_Role_Type", "Status_Stage_Tier", "Period_Year",
    "Primary_Metric_Label", "Primary_Value", "Primary_Unit",
    "Secondary_Metric_Label", "Secondary_Value", "Tariff_Price_Rate_Label",
    "Tariff_Price_Rate_Value", "Counterparty_Owner_Offtaker",
    "Technical_Commercial_Notes_Details", "Verification_Status_Source_URL",
    "OP_Region_4", "Substation_Base_MW", "Substation_MVA", "Substation_Queued_MW",
    "Substation_PV_Yield", "Aggregator_Bought_GWh", "Aggregator_Sold_Wholesale_GWh",
    "Aggregator_Retail_GWh", "EndUser_Tariff_EUR_MWh", "Household_Tariff_EUR_MWh",
]

PROTECTED_DOMAIN_PREFIXES = ("3. ", "7. ", "9. ")


class NonCritical(Exception):
    """Raised for datasets whose failure must not turn the daily run red."""


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def lead_num(val: Any, default: Optional[float] = None) -> Optional[float]:
    """Extract leading float like the VBA LeadNum() does."""
    if val is None:
        return default
    s = str(val).strip().replace("$", "").replace(",", "")
    m = re.match(r"^([+-]?\d+(?:\.\d+)?)", s)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return default
    return default


def validate_schema(rows: List[Dict[str, str]], fieldnames: List[str]) -> Tuple[bool, str]:
    if list(fieldnames) != EXPECTED_HEADERS:
        return False, f"Header mismatch! Expected {len(EXPECTED_HEADERS)} columns, got {len(fieldnames)}."
    if len(rows) not in (486, 487):
        return False, f"Row count violation! Expected 486 or 487 rows, got {len(rows)}."
    rec_ids = [r["Record_ID"] for r in rows]
    if len(set(rec_ids)) != len(rec_ids):
        return False, "Duplicate Record_IDs detected!"
    valid_prefixes = tuple(f"{i}. " for i in range(1, 11))
    for r in rows:
        dom = r.get("Domain_Category", "")
        if not dom.startswith(valid_prefixes):
            return False, f"Invalid Domain_Category prefix in {r['Record_ID']}: {dom}"
    return True, "OK"


def http_get_text(url: str, timeout: int = 20, retries: int = 2) -> str:
    """Download from an allow-listed host only, with a small retry and a cache copy."""
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise ValueError(f"Blocked: '{host}' is not an allow-listed official source")
    os.makedirs(CACHE_DIR, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "BiH-Energy-Master-Updater/2.0 (george.michael@elsewedy.com)"},
    )
    last_err: Optional[Exception] = None
    for _ in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read().decode("utf-8", errors="replace")
            break
        except Exception as e:  # network / HTTP error -> retry
            last_err = e
    else:
        raise last_err  # type: ignore[misc]
    ts = now_utc().strftime("%Y%m%d")
    h = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
    with open(os.path.join(CACHE_DIR, f"{ts}_{h}.txt"), "w", encoding="utf-8") as f:
        f.write(data)
    return data


# ==============================================================================
# DAILY: FX BAM/USD  (ECB reference rate + CBBH peg 1.95583)  -> BIH-0128
# ==============================================================================
ECB_FX_URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
ECB_DAILY_XML = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def fetch_ecb_usd_series(start_date: str) -> List[Tuple[datetime.date, float]]:
    """Daily EUR/USD observations from the ECB Data Portal (oldest first)."""
    text = http_get_text(f"{ECB_FX_URL}?startPeriod={start_date}&format=csvdata")
    out: List[Tuple[datetime.date, float]] = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            d = datetime.date.fromisoformat(row["TIME_PERIOD"])
            v = float(row["OBS_VALUE"])
        except (KeyError, ValueError):
            continue
        if v > 0:
            out.append((d, v))
    out.sort()
    if not out:
        raise ValueError("ECB Data Portal returned no USD observations")
    return out


def fetch_ecb_latest_usd_xml() -> List[Tuple[datetime.date, float]]:
    """Fallback: ECB's own daily reference-rate file (latest day only)."""
    xml = http_get_text(ECB_DAILY_XML)
    m_date = re.search(r"<Cube\s+time=['\"](\d{4}-\d{2}-\d{2})['\"]", xml)
    m_rate = re.search(r"currency=['\"]USD['\"]\s+rate=['\"]([\d.]+)['\"]", xml)
    if not (m_date and m_rate):
        raise ValueError("Could not parse ECB daily reference-rate file")
    return [(datetime.date.fromisoformat(m_date.group(1)), float(m_rate.group(1)))]


def fetch_fx(
    existing_rows: Dict[str, Dict[str, str]],
    test_override_spot: Optional[float] = None,
) -> Dict[str, Dict[str, str]]:
    today = now_utc().date()
    cur_val = existing_rows.get("BIH-0128", {}).get(
        "Primary_Value", "1.6710 (Avg) / 1.7438 (Nov 3 Spot)"
    )
    cur_avg = lead_num(cur_val, 1.6710) or 1.6710
    m_spot = re.search(r"/\s*(\d+\.\d+)\s*\(", cur_val)
    old_spot = float(m_spot.group(1)) if m_spot else 1.7438

    if test_override_spot is not None:
        obs_date = today
        new_spot = round(float(test_override_spot), 4)
        eur_usd = round(BAM_PER_EUR_PEG / new_spot, 4)
        new_avg = cur_avg
        src_url = ECB_FX_URL
    else:
        try:
            series = fetch_ecb_usd_series(f"{today.year}-01-01")
            ytd = [BAM_PER_EUR_PEG / v for d, v in series if d.year == today.year]
            new_avg = round(statistics.mean(ytd), 4) if ytd else cur_avg
            src_url = ECB_FX_URL
        except Exception:
            series = fetch_ecb_latest_usd_xml()  # may raise -> FAILED
            new_avg = cur_avg  # cannot recompute from a single day
            src_url = ECB_DAILY_XML
        obs_date, eur_usd = series[-1]
        new_spot = round(BAM_PER_EUR_PEG / eur_usd, 4)

    move = abs(new_spot - old_spot) / old_spot if old_spot else 0.0
    if move > 0.03:
        raise ValueError(
            f"SANITY REJECT: BAM/USD spot moved {move*100:.2f}% ({old_spot} -> {new_spot}), above the 3.0% limit"
        )

    label = f"{obs_date.strftime('%b')} {obs_date.day}"  # e.g. "Nov 21" (ECB observation date)
    yoy = ((new_avg - 1.7347) / 1.7347) * 100.0
    return {
        "BIH-0128": {
            "Primary_Value": f"{new_avg:.4f} (Avg) / {new_spot:.4f} ({label} Spot)",
            "Secondary_Value": f"Peg: 1.95583 BAM/EUR | EUR/USD: {eur_usd:.4f} (Spot) | YoY vs USD: {yoy:+.1f}% (Avg)",
            "Technical_Commercial_Notes_Details": f"Structural Driver: Official CBBH / ECB Spot on {label}, {obs_date.year}: {new_spot:.4f} BAM/USD",
            "Verification_Status_Source_URL": f"VERIFIED {today.strftime('%d.%m.%Y')} | CBBH (cbbh.ba); ECB Reference Rate ({src_url})",
        }
    }


# ==============================================================================
# DAILY (TEMPORARY SOURCE): Regional day-ahead power price -> BIH-0487
# ==============================================================================
def fetch_day_ahead_power(
    existing_rows: Dict[str, Dict[str, str]],
    test_override_price: Optional[float] = None,
) -> Dict[str, Dict[str, str]]:
    if "BIH-0487" not in existing_rows:
        return {}
    today_ver = now_utc().strftime("%d.%m.%Y")
    ind_tariff = lead_num(existing_rows.get("BIH-0424", {}).get("Primary_Value"), 118.57) or 118.57

    if test_override_price is not None:
        base_eur = round(float(test_override_price), 2)
        peak_eur = round(base_eur * 1.18, 2)
        src_url = "https://api.energy-charts.info/price?bzn=RS"
    else:
        url = "https://api.energy-charts.info/price?bzn=RS"
        raw = json.loads(http_get_text(url))
        prices = [float(p) for p in raw.get("price", []) if p is not None]
        if not prices:
            raise ValueError("Empty price array from Energy-Charts API")
        day_prices = prices[-24:] if len(prices) >= 24 else prices
        base_eur = round(sum(day_prices) / len(day_prices), 2)
        peak_eur = round(max(day_prices), 2)
        src_url = url

    if base_eur < -100.0 or base_eur > 1000.0:
        raise ValueError(f"SANITY REJECT: Day-ahead price {base_eur} EUR/MWh out of bounds [-100, 1000]")

    spread = round(base_eur - ind_tariff, 2)
    return {
        "BIH-0487": {
            "Primary_Value": f"{base_eur:.2f}",
            # "Avg" (not "30D Avg"): this is the average of the latest 24 prices, not a 30-day mean.
            "Secondary_Value": f"Peak: {peak_eur:.2f} EUR/MWh | Avg (latest 24 prices): {base_eur:.2f} EUR/MWh | Spread vs Ind: {spread:+.2f} EUR/MWh",
            "Tariff_Price_Rate_Value": f"{base_eur:.2f} EUR/MWh",
            "EndUser_Tariff_EUR_MWh": f"{base_eur:.2f}",
            "Verification_Status_Source_URL": f"VERIFIED {today_ver} | {src_url} (Energy-Charts / Fraunhofer ISE; ENTSO-E data) - TEMPORARY SOURCE",
        }
    }


# ==============================================================================
# SEMI-ANNUAL (IMF WEO: April / October): GDP 2026 projection -> BIH-0112
# ==============================================================================
IMF_BASES = [
    "https://www.imf.org/external/datamapper/api/v2",  # current version
    "https://www.imf.org/external/datamapper/api/v1",  # legacy, tried second
]


def fetch_macro_imf(existing_rows: Dict[str, Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    series: Dict[str, Any] = {}
    last_err: Optional[Exception] = None
    used_url = ""
    for base in IMF_BASES:
        url = f"{base}/NGDPD/BIH"
        try:
            raw = json.loads(http_get_text(url))
            series = raw.get("values", {}).get("NGDPD", {}).get("BIH", {})
            if series:
                used_url = url
                break
        except Exception as e:
            last_err = e
    if not series:
        raise NonCritical(f"IMF DataMapper unavailable ({last_err}); GDP stays unchanged")

    today_ver = now_utc().strftime("%d.%m.%Y")
    updates: Dict[str, Dict[str, str]] = {}
    if "2026" in series:
        new_gdp = round(float(series["2026"]), 2)
        old_gdp = lead_num(existing_rows.get("BIH-0112", {}).get("Primary_Value"), 37.26) or 37.26
        if 0.80 <= (new_gdp / old_gdp) <= 1.20 and abs(new_gdp - old_gdp) >= 0.01:
            updates["BIH-0112"] = {
                "Primary_Value": f"${new_gdp:.2f} Billion",
                "Verification_Status_Source_URL": f"VERIFIED {today_ver} | IMF DataMapper ({used_url})",
            }
    return updates


# ==============================================================================
# WATCHDOGS: page fingerprints only (never overwrite data)
# ==============================================================================
def check_regulatory_watchdogs() -> Dict[str, str]:
    watch_urls = {
        "NOSBiH_Balanced_Users": "https://www.nosbih.ba/bs/dokumenti/registri/bilansirani-korisnici/",
        "DERK_Reports": "https://www.derk.ba/en/izvjetaji-mainmenu-109",
        "FERK_Decisions": "https://www.ferk.ba/",
    }
    results: Dict[str, str] = {}
    for name, url in watch_urls.items():
        try:
            html = http_get_text(url, timeout=10)
            results[name] = f"OK (sha256:{hashlib.sha256(html.encode('utf-8', errors='ignore')).hexdigest()[:12]})"
        except Exception as e:
            results[name] = f"SKIPPED ({type(e).__name__})"
    return results


# ==============================================================================
# CORE ENGINE
# ==============================================================================
def run_update(
    dry_run: bool = False,
    test_fx_spot: Optional[float] = None,
    test_da_price: Optional[float] = None,
    simulate_fx_failure: bool = False,
) -> Dict[str, Any]:
    with open(CSV_PATH, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    ok, msg = validate_schema(rows, fieldnames)
    if not ok:
        raise RuntimeError(f"Baseline schema validation failed: {msg}")

    row_map = {r["Record_ID"]: r for r in rows}
    status_report: Dict[str, Any] = {
        "last_run_utc": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_verified_date": now_utc().strftime("%d %b %Y"),
        "row_count": len(rows),
        "datasets": {},
        "changes_applied": 0,
    }
    pending: Dict[str, Tuple[Dict[str, str], str]] = {}

    def run_dataset(key: str, fn, src_tag: str) -> None:
        try:
            for rid, chg in fn().items():
                pending[rid] = (chg, src_tag)
            status_report["datasets"][key] = "SUCCESS"
        except NonCritical as e:
            status_report["datasets"][key] = f"WARN: {e}"
        except Exception as e:
            status_report["datasets"][key] = f"FAILED: {e}"

    def fx_job():
        if simulate_fx_failure:
            raise urllib.error.URLError("Simulated FX endpoint outage")
        return fetch_fx(row_map, test_override_spot=test_fx_spot)

    run_dataset("fx_bam_usd", fx_job, "ECB/CBBH_FX")
    run_dataset("day_ahead_power", lambda: fetch_day_ahead_power(row_map, test_override_price=test_da_price), "ENERGY_CHARTS_TEMP")

    if test_fx_spot is None and test_da_price is None and not simulate_fx_failure:
        run_dataset("macro_imf", lambda: fetch_macro_imf(row_map), "IMF_DataMapper")
        status_report["watchdogs"] = check_regulatory_watchdogs()

    changelog: List[Dict[str, str]] = []
    today_iso = now_utc().date().isoformat()
    for r in rows:
        rid = r["Record_ID"]
        if rid not in pending:
            continue
        if r["Domain_Category"].startswith(PROTECTED_DOMAIN_PREFIXES):
            status_report["datasets"][f"blocked_{rid}"] = "SKIPPED_PROTECTED_DOMAIN"
            continue
        col_changes, src_tag = pending[rid]
        for col, new_val in col_changes.items():
            if col not in fieldnames or col in ("Record_ID", "Domain_Category"):
                continue
            old_val = r.get(col, "")
            if str(old_val).strip() != str(new_val).strip():
                r[col] = str(new_val)
                if col == "Primary_Value":
                    changelog.append({
                        "Date": today_iso, "Record_ID": rid, "Column": col,
                        "Old_Value": old_val, "New_Value": str(new_val), "Source": src_tag,
                    })

    ok_post, msg_post = validate_schema(rows, fieldnames)
    if not ok_post:
        raise RuntimeError(f"Post-update schema check failed: {msg_post}")

    status_report["changes_applied"] = len(changelog)

    if not dry_run:
        with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        exists = os.path.exists(CHANGELOG_PATH)
        with open(CHANGELOG_PATH, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["Date", "Record_ID", "Column", "Old_Value", "New_Value", "Source"])
            if not exists:
                w.writeheader()
            w.writerows(changelog)
        with open(STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump(status_report, f, indent=2)

    return status_report


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    fx_arg = float(args[0]) if args and args[0] != "none" else None
    rep = run_update(dry_run="--dry-run" in sys.argv, test_fx_spot=fx_arg)
    print(json.dumps(rep, indent=2))
