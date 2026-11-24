import io
import os
import re
import json
import time
import datetime

from pipeline_status import DATASETS, record, write_status, utc_now
from urllib.parse import urljoin, urlencode
from zoneinfo import ZoneInfo

import requests as std_requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import pandas as pd
import pdfplumber
from bs4 import BeautifulSoup
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    HAS_CFFI = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,bs;q=0.8,hr;q=0.7,sr;q=0.6",
    "Connection": "keep-alive",
}


def create_resilient_session(retries=3, backoff_factor=1.5, use_cffi=False):
    if use_cffi and HAS_CFFI:
        return cffi_requests.Session(impersonate="chrome124")
    session = std_requests.Session()
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("HEAD", "GET", "POST", "OPTIONS"),
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def safe_get(session, url, timeout_tuple=(10, 45)):
    if HAS_CFFI and isinstance(session, cffi_requests.Session):
        return session.get(url, headers=HEADERS, timeout=timeout_tuple, verify=True)
    return session.get(url, headers=HEADERS, timeout=timeout_tuple, verify=True)


def parse_numeric_cell(val):
    if val is None:
        return None
    s = (
        str(val)
        .strip()
        .replace(" ", "")
        .replace("\xa0", "")
        .replace("−", "-")
        .replace("–", "-")
        .replace("%", "")
        .replace("+", "")
    )
    if not s or s in ("-", "—", "N/A", "null", "None", "Pending"):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts) > 2 or (len(parts) == 2 and len(parts[-1]) == 3):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# =============================================================================
# NOS BiH hourly dispatch: admin-ajax fetch, 30-day history, no hardcoded values
# The page's script-production.js POSTs action=production and production=date=dd.mm.yyyy.
# to admin-ajax.php; the response's "data" field is the HTML table.
# =============================================================================

NOS_PAGE = "https://www.nosbih.ba/en/ees/graf-potrosnje/"
NOS_AJAX_URL = "https://www.nosbih.ba/en/wp-admin/admin-ajax.php"
NOS_HOURLY_CSV = "nos_hourly.csv"
NOS_TZ = ZoneInfo("Europe/Sarajevo")
HISTORY_DAYS = 30

NOS_VALUE_COLS = [
    "Planned_Production_MW",
    "Actual_Production_MW",
    "Planned_Consumption_MW",
    "Actual_Consumption_MW",
]
NOS_COLS = ["Date", "Hour"] + NOS_VALUE_COLS + ["Fetched_At"]


class NosError(RuntimeError):
    pass


def _sarajevo_today() -> datetime.date:
    return datetime.datetime.now(NOS_TZ).date()


def _num(s) -> float:
    # Blank or pending cells become NaN (missing), never 0.
    s = (s or "").strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def parse_nos_table(html: str, day: datetime.date) -> pd.DataFrame:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="productionTable")
    if table is None:
        raise NosError("productionTable not found in admin-ajax response")

    rows = []
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        t = tds[0].get_text(strip=True)
        if ":" not in t:
            continue
        hour = int(t.split(":")[0])
        hour = 24 if hour == 0 else hour        # "00:00" is hour 24
        rows.append({
            "Date": day,
            "Hour": hour,
            "Planned_Production_MW": _num(tds[1].get_text()),
            "Actual_Production_MW": _num(tds[2].get_text()),
            "Planned_Consumption_MW": _num(tds[3].get_text()),
            "Actual_Consumption_MW": _num(tds[4].get_text()),
        })

    if not rows:
        raise NosError("productionTable has no data rows")
    return pd.DataFrame(rows)


def fetch_nos_day(session, day: datetime.date) -> pd.DataFrame:
    # Same request the page's script-production.js sends when a date is chosen.
    date_str = day.strftime("%d.%m.%Y.")
    headers = {
        **HEADERS,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": NOS_PAGE,
        "Origin": "https://www.nosbih.ba",
    }
    data = {
        "action": "production",
        "production": urlencode({"date": date_str}),
    }
    resp = session.post(NOS_AJAX_URL, headers=headers, data=data, timeout=(10, 45))
    resp.raise_for_status()

    try:
        payload = resp.json()
    except ValueError as e:
        raise NosError(f"admin-ajax did not return JSON for {date_str}") from e

    html = payload.get("data") if isinstance(payload, dict) else None
    if not html:
        raise NosError(f"admin-ajax returned no data for {date_str}")
    return parse_nos_table(html, day)

def load_nos_history() -> pd.DataFrame:
    if os.path.exists(NOS_HOURLY_CSV):
        df = pd.read_csv(NOS_HOURLY_CSV)
        if len(df):
            df["Date"] = pd.to_datetime(df["Date"]).dt.date
            return df
    return pd.DataFrame(columns=NOS_COLS)


def merge_nos(hist: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    # A fresh non-missing value replaces the stored one.
    # A blank in a later fetch never erases a value already stored.
    if hist.empty:
        merged = new[NOS_COLS].copy()
    else:
        m = hist.merge(new, on=["Date", "Hour"], how="outer", suffixes=("_old", "_new"))
        for c in NOS_VALUE_COLS:
            m[c] = m[c + "_new"].where(m[c + "_new"].notna(), m[c + "_old"])
        m["Fetched_At"] = m["Fetched_At_new"].fillna(m["Fetched_At_old"])
        merged = m[NOS_COLS]

    cutoff = _sarajevo_today() - datetime.timedelta(days=HISTORY_DAYS - 1)
    merged = merged[merged["Date"] >= cutoff]
    return merged.sort_values(["Date", "Hour"]).reset_index(drop=True)


def update_nos_history() -> dict:
    # Fetch the 30-day window: all days missing from history, plus today and yesterday (actuals keep arriving).
    hist = load_nos_history()
    session = create_resilient_session(retries=2, backoff_factor=1.5)
    stamp = datetime.datetime.now(NOS_TZ).isoformat(timespec="minutes")
    today = _sarajevo_today()

    window = [today - datetime.timedelta(days=i) for i in range(HISTORY_DAYS)]
    have = set(hist["Date"]) if len(hist) else set()
    refetch = {today, today - datetime.timedelta(days=1)}
    todo = [d for d in window if d not in have or d in refetch]

    failed = []
    for d in todo:
        try:
            df = fetch_nos_day(session, d)
            df["Fetched_At"] = stamp
            hist = merge_nos(hist, df)
        except Exception as e:
            failed.append(f"{d}: {e}")
        time.sleep(1)                             # be polite to the server

    hist.to_csv(NOS_HOURLY_CSV, index=False)

    with_actual = hist.dropna(subset=["Actual_Production_MW"])
    latest_actual = None
    if len(with_actual):
        last = with_actual.sort_values(["Date", "Hour"]).iloc[-1]
        latest_actual = f"{last['Date']} {int(last['Hour']):02d}:00"

    state = "OK"
    detail = "Historical requests completed; latest actual checked separately"
    if latest_actual is None:
        state, detail = "NO_DATA", "No actual production values"
    else:
        latest_row = with_actual.sort_values(["Date", "Hour"]).iloc[-1]
        latest_end = (datetime.datetime.combine(latest_row["Date"], datetime.time(), NOS_TZ)
                      + datetime.timedelta(hours=int(latest_row["Hour"])))
        age = (datetime.datetime.now(NOS_TZ) - latest_end).total_seconds()
        if not 0 <= age <= 6 * 3600:
            state, detail = "STALE", "Latest actual outside six-hour freshness ceiling"
    if failed:
        state, detail = "PARTIAL", "One or more NOS day requests failed"
    record("nos", state, detail=detail, latest_actual=latest_actual,
           source=NOS_PAGE, dst_note="Legacy Date/Hour feed is not DST-safe; ENTSO-E uses UTC")
    return {
        "nos_source": "admin-ajax",
        "nos_fetched_at": stamp,
        "nos_days_requested": len(todo),
        "nos_days_failed": failed,
        "nos_rows": int(len(hist)),
        "nos_latest_actual": latest_actual,
    }


def nos_day_for_workbook(hist: pd.DataFrame, dam_curve):
    # Returns (24-row DataFrame for the most recent day in history, that date or None).
    day = pd.DataFrame({"Hour_Index": range(1, 25)})
    latest = None
    if len(hist):
        latest = max(hist["Date"])
        src = hist[hist["Date"] == latest].rename(columns={"Hour": "Hour_Index"})
        day = day.merge(src[["Hour_Index"] + NOS_VALUE_COLS], on="Hour_Index", how="left")
    else:
        for c in NOS_VALUE_COLS:
            day[c] = float("nan")
    day["DAM_Price_EUR_MWh"] = list(dam_curve[:24]) if dam_curve else float("nan")
    day["Peak_ToU_Flag"] = day["Hour_Index"].apply(
        lambda h: 1 if ((7 <= h <= 13) or (16 <= h <= 22)) else 0
    )
    return day, latest

# =============================================================================
# CROSS-BORDER EXCHANGE (NOSBiH "Cross-border exchange" page)
# Values are in the page HTML. Direction comes from the page's own CSS:
# the arrow group with display: inline is the one shown.
# Sign: export from BiH = positive, import = negative.
# Check: signed border flows must add up to the page's summary value.
# Uses from earlier in update_bih.py: re, os, datetime, pd, BeautifulSoup,
# NosError, NOS_TZ, HISTORY_DAYS, create_resilient_session, safe_get, _sarajevo_today.
# =============================================================================

FLOWS_PAGE = "https://www.nosbih.ba/en/ees/fizicki-tokovi/"
FLOWS_CSV = "flows_snapshots.csv"
FLOW_CODES = {"hr": "Croatia", "sr": "Serbia", "cg": "Montenegro"}
FLOW_TOLERANCE_MW = 2.0
FLOW_COLS = [
    "Timestamp", "Fetched_At", "ACE_MW",
    "Act_HR_Signed", "Act_SR_Signed", "Act_CG_Signed", "Act_Net_Calc", "Act_Net_Page", "Act_Check",
    "Plan_HR_Signed", "Plan_SR_Signed", "Plan_CG_Signed", "Plan_Net_Calc", "Plan_Net_Page", "Plan_Check",
]


def _flow_num(text):
    if text is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group(0)) if m else None


def _flow_visible_arrows(svg):
    # Returns e.g. {"hr": "out", "sr": "in", "cg": "in"} from the arrow rules the page shows.
    style = svg.find("style")
    css = style.get_text() if style else ""
    shown = {}
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if not re.search(r"display\s*:\s*inline", body):
            continue
        for sel in selectors.split(","):
            m = re.search(r"#arrow-[a-z]+-([a-z]{2})-\d{2}-(in|out)", sel)
            if m:
                shown[m.group(1)] = m.group(2)
    return shown


def parse_flows_page(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    def text_of(elem_id):
        el = soup.find(id=elem_id)
        if el is None:
            raise NosError(f"'{elem_id}' not found on flows page")
        return el.get_text(strip=True)

    row = {}
    for label, svg_id, prefix in (("Act", "map-flow-current", "current"),
                                  ("Plan", "map-flow-planned", "planned")):
        svg = soup.find("svg", id=svg_id)
        if svg is None:
            raise NosError(f"'{svg_id}' not found on flows page")
        shown = _flow_visible_arrows(svg)

        signed = {}
        for code in FLOW_CODES:
            mag = _flow_num(text_of(f"{prefix}-value-{code}"))
            if mag is None:
                raise NosError(f"no {label} value for {code}")
            if mag == 0:
                signed[code] = 0.0
                continue
            direction = shown.get(code)
            if direction is None:
                raise NosError(f"no visible arrow direction for {label} {code}")
            signed[code] = mag if direction == "out" else -mag

        net_calc = sum(signed.values())
        net_page = _flow_num(text_of(f"{prefix}-value-summary"))
        if net_page is None:
            raise NosError(f"no {label} summary value")

        for code in FLOW_CODES:
            row[f"{label}_{code.upper()}_Signed"] = signed[code]
        row[f"{label}_Net_Calc"] = net_calc
        row[f"{label}_Net_Page"] = net_page
        row[f"{label}_Check"] = abs(net_calc - net_page) <= FLOW_TOLERANCE_MW

    ace = _flow_num(text_of("ace-value"))
    if ace is None:
        raise NosError("ACE value not parsed")
    row["ACE_MW"] = ace

    m = re.search(r"(\d{2}\.\d{2}\.\d{4})\.?\s+(\d{2}:\d{2})", text_of("flow-time"))
    if m is None:
        raise NosError("flow-time not parsed")
    ts = datetime.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d.%m.%Y %H:%M")
    row["Timestamp"] = ts.strftime("%Y-%m-%d %H:%M")
    return row


def load_flows_history() -> pd.DataFrame:
    if os.path.exists(FLOWS_CSV):
        df = pd.read_csv(FLOWS_CSV, dtype={"Timestamp": str})
        if len(df):
            return df
    return pd.DataFrame(columns=FLOW_COLS)


def update_flows_history() -> dict:
    session = create_resilient_session(retries=2, backoff_factor=1.5)
    resp = safe_get(session, f"{FLOWS_PAGE}?nocache={int(time.time())}", timeout_tuple=(10, 45))
    resp.raise_for_status()
    row = parse_flows_page(resp.text)
    if not row["Act_Check"] or not row["Plan_Check"]:
        raise NosError("Cross-border reconciliation failed; previous CSV retained")
    row["Fetched_At"] = datetime.datetime.now(NOS_TZ).isoformat(timespec="minutes")

    # One row per page timestamp. A newer fetch of the same timestamp replaces the old row.
    hist = load_flows_history()
    hist = hist[hist["Timestamp"] != row["Timestamp"]]
    hist = pd.concat([hist, pd.DataFrame([row], columns=FLOW_COLS)], ignore_index=True)

    cutoff = (_sarajevo_today() - datetime.timedelta(days=HISTORY_DAYS - 1)).strftime("%Y-%m-%d")
    hist = hist[hist["Timestamp"].str[:10] >= cutoff]
    hist = hist.sort_values("Timestamp").reset_index(drop=True)
    hist.to_csv(FLOWS_CSV, index=False)

    return {
        "flows_timestamp": row["Timestamp"],
        "flows_checks_ok": bool(row["Act_Check"] and row["Plan_Check"]),
        "flows_rows": int(len(hist)),
    }
# =============================================================================
# DAM reference curve (energy-charts.info). Returns None on failure, never a typed-in curve.
# =============================================================================

def fetch_seepex_hupx_dam_curve():
    session = create_resilient_session(retries=2, use_cffi=False)
    try:
        r = session.get("https://api.energy-charts.info/price?bzn=RS", timeout=(10, 20)).json()
        prices = [float(x) for x in r.get("price", []) if x is not None]
        if len(prices) >= 24:
            return [round(x, 2) for x in prices[-24:]]
        print("[DAM] energy-charts returned fewer than 24 prices")
    except Exception as e:
        print(f"[DAM] fetch failed: {e}")
    return None


def fetch_nosbih_balancing_prices():
    session = create_resilient_session(retries=2)
    pdf_urls = (
        "https://www.nosbih.ba/files/balansno/izvjestaji/mjeseci/202508-en-balansno-trziste-izvjestaj.pdf",
        "https://www.nosbih.ba/files/balansno/izvjestaji/mjeseci/202508-lat-balansno-trziste-izvjestaj.pdf",
    )
    verified_balancing_rows = [
        {
            "Reserve_Category": "FCR_Upward",
            "Direction": 1,
            "Activated_Energy_MWh": 222.0,
            "Total_Energy_Cost_BAM": 34502.0,
            "Reported_Avg_Price_BAM_MWh": 155.31,
            "Extreme_Max_Min_Price_BAM_MWh": 460.77,
            "SERC_Energy_Price_Cap_BAM_MWh": 989.46,
            "SERC_Capacity_Cap_BAM_MW_h": 25.00,
            "Status": "UNVERIFIED",
        },
        {
            "Reserve_Category": "FCR_Downward",
            "Direction": -1,
            "Activated_Energy_MWh": 242.0,
            "Total_Energy_Cost_BAM": 35047.0,
            "Reported_Avg_Price_BAM_MWh": 144.81,
            "Extreme_Max_Min_Price_BAM_MWh": -135.64,
            "SERC_Energy_Price_Cap_BAM_MWh": -401.28,
            "SERC_Capacity_Cap_BAM_MW_h": 25.00,
            "Status": "UNVERIFIED",
        },
        {
            "Reserve_Category": "aFRR_Upward",
            "Direction": 1,
            "Activated_Energy_MWh": 3020.0,
            "Total_Energy_Cost_BAM": 462846.0,
            "Reported_Avg_Price_BAM_MWh": 153.26,
            "Extreme_Max_Min_Price_BAM_MWh": 200.00,
            "SERC_Energy_Price_Cap_BAM_MWh": 989.46,
            "SERC_Capacity_Cap_BAM_MW_h": 43.00,
            "Status": "UNVERIFIED",
        },
        {
            "Reserve_Category": "aFRR_Downward",
            "Direction": -1,
            "Activated_Energy_MWh": 2368.0,
            "Total_Energy_Cost_BAM": 241698.0,
            "Reported_Avg_Price_BAM_MWh": 102.08,
            "Extreme_Max_Min_Price_BAM_MWh": 160.00,
            "SERC_Energy_Price_Cap_BAM_MWh": -401.28,
            "SERC_Capacity_Cap_BAM_MW_h": 43.00,
            "Status": "UNVERIFIED",
        },
        {
            "Reserve_Category": "mFRR_Upward",
            "Direction": 1,
            "Activated_Energy_MWh": 956.0,
            "Total_Energy_Cost_BAM": 761531.0,
            "Reported_Avg_Price_BAM_MWh": 796.30,
            "Extreme_Max_Min_Price_BAM_MWh": 850.00,
            "SERC_Energy_Price_Cap_BAM_MWh": 989.46,
            "SERC_Capacity_Cap_BAM_MW_h": 9.00,
            "Status": "UNVERIFIED",
        },
        {
            "Reserve_Category": "mFRR_Downward",
            "Direction": -1,
            "Activated_Energy_MWh": 888.0,
            "Total_Energy_Cost_BAM": -323837.0,
            "Reported_Avg_Price_BAM_MWh": -364.85,
            "Extreme_Max_Min_Price_BAM_MWh": -400.00,
            "SERC_Energy_Price_Cap_BAM_MWh": -401.28,
            "SERC_Capacity_Cap_BAM_MW_h": 2.10,
            "Status": "UNVERIFIED",
        },
    ]
    for pdf_url in pdf_urls:
        try:
            resp = safe_get(session, pdf_url, timeout_tuple=(10, 25))
            if resp.status_code == 200 and b"%PDF" in resp.content[:10]:
                with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
                    first_page = next(iter(pdf.pages), None)
                    first_page_text = first_page.extract_text() if first_page else ""
                    if "aFRR" in first_page_text and "mFRR" in first_page_text:
                        break
        except Exception:
            continue
    record("balancing", "STATIC_REFERENCE", detail="Typed-in August 2025 values, not extracted or freshly verified")
    return pd.DataFrame(verified_balancing_rows).assign(Status="STATIC_REFERENCE")


def discover_derk_pdfs(page_url="http://www.derk.ba/DocumentsPDFs/"):
    session = create_resilient_session(retries=2)
    try:
        resp = safe_get(session, page_url, timeout_tuple=(10, 25))
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        pdf_urls = []
        for a_tag in soup.find_all("a", href=True):
            href = a_tag.get("href", "")
            if href.lower().endswith(".pdf"):
                full_url = urljoin(page_url, href)
                pdf_urls.append({"title": a_tag.get_text(strip=True) or href, "url": full_url})
        return pdf_urls
    except Exception:
        return [
            {
                "title": "Metodologija-za-izradu-tarifa-24sep2025-b.pdf",
                "url": "https://www.derk.ba/DocumentsPDFs/Metodologija-za-izradu-tarifa-24sep2025-b.pdf",
            }
        ]


def fetch_cbbh_fx_rates():
    url = "https://cbbh.ba/CurrencyExchange/"
    session = create_resilient_session(retries=2)
    fx_data = {
        "EUR_BAM_Middle": 1.955830,
        "USD_BAM_Buy": 1.775196,
        "USD_BAM_Middle": 1.779645,
        "USD_BAM_Sell": 1.784094,
        "Date": None,
        "Status": "FALLBACK",
    }
    try:
        resp = safe_get(session, url, timeout_tuple=(10, 25))
        if resp.status_code == 200:
            text = BeautifulSoup(resp.text, "lxml").get_text(" ", strip=True)
            usd_match = re.search(
                r"USD\s+1\s+840\s+([\d\.,]+)\s+([\d\.,]+)\s+([\d\.,]+)", text
            )
            if usd_match:
                fx_data["USD_BAM_Buy"] = float(usd_match.group(1).replace(",", "."))
                fx_data["USD_BAM_Middle"] = float(usd_match.group(2).replace(",", "."))
                fx_data["USD_BAM_Sell"] = float(usd_match.group(3).replace(",", "."))
                fx_data["Status"] = "PARSED_UNDATED"
    except Exception:
        pass
    record("fx", fx_data["Status"], detail="Source publication date not validated; do not relabel as live", source=url)
    return fx_data


def fetch_worldometer_bih_population():
    url = "https://www.worldometers.info/world-population/bosnia-and-herzegovina-population/"
    session = create_resilient_session(retries=2)
    live_counter = 3106384.0
    live_parsed = False
    table_parsed = False
    wm_rows = []
    fallback_wm = (
        (2020, 3299349.0, -0.0128, -43838.0, -26840.0, 43.8, 1.52, 0.500, 1648721.0),
        (2021, 3244907.0, -0.0165, -54442.0, -25865.0, 44.2, 1.49, 0.505, 1638352.0),
        (2022, 3204802.0, -0.0124, -40105.0, -485.0, 44.6, 1.48, 0.510, 1635139.0),
        (2023, 3185073.0, -0.0062, -19729.0, -485.0, 44.9, 1.49, 0.516, 1642406.0),
        (2024, 3164253.0, -0.0065, -20820.0, -4497.0, 45.3, 1.49, 0.521, 1649286.0),
        (2025, 3140096.0, -0.0076, -24157.0, -6994.0, 45.7, 1.50, 0.527, 1654565.0),
        (2026, 3114242.0, -0.0082, -25854.0, -7581.0, 46.1, 1.50, 0.533, 1659049.0),
        (2030, 3013974.0, -0.0082, -25224.0, -4630.0, 47.7, 1.52, 0.557, 1679374.0),
        (2035, 2881542.0, -0.0089, -26486.0, -6675.0, 49.5, 1.53, 0.590, 1701329.0),
        (2040, 2745010.0, -0.0097, -27306.0, -6597.0, 50.6, 1.55, 0.626, 1719660.0),
        (2045, 2599926.0, -0.0108, -29017.0, -4425.0, 51.2, 1.56, 0.665, 1729471.0),
        (2050, 2455167.0, -0.0114, -28952.0, -3517.0, 51.5, 1.57, 0.706, 1734553.0),
    )
    try:
        resp = safe_get(session, url, timeout_tuple=(10, 30))
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "lxml")
            full_text = soup.get_text(" ", strip=True)
            m_live = re.search(
                r"current population of Bosnia and Herzegovina is\s+([\d,]+)",
                full_text,
                re.IGNORECASE,
            )
            if m_live:
                parsed_live = parse_numeric_cell(m_live.group(1))
                if parsed_live and parsed_live > 2000000:
                    live_counter = parsed_live
                    live_parsed = True

            seen_years = set()
            for table in soup.find_all("table"):
                for tr in table.find_all("tr"):
                    tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                    if len(tds) >= 9:
                        c_yr, c_pop, c_pct, c_chg, c_mig, c_age, c_fert, c_dens, c_urb_pct, c_urb_pop = (
                            tds[:10] if len(tds) >= 10 else (*tds[:9], None)
                        )
                        yr_val = parse_numeric_cell(c_yr)
                        pop_val = parse_numeric_cell(c_pop)
                        if yr_val and int(yr_val) in (2020, 2021, 2022, 2023, 2024, 2025, 2026, 2030, 2035, 2040, 2045, 2050):
                            yr_int = int(yr_val)
                            if yr_int not in seen_years and pop_val and pop_val > 1000000:
                                seen_years.add(yr_int)
                                pct_raw = parse_numeric_cell(c_pct)
                                urb_pct_raw = parse_numeric_cell(c_urb_pct)
                                wm_rows.append({
                                    "Year": yr_int,
                                    "WM_Population_Headcount": pop_val,
                                    "Yearly_Growth_Fraction": (pct_raw / 100.0) if pct_raw is not None else None,
                                    "Yearly_Net_Change_Headcount": parse_numeric_cell(c_chg),
                                    "Net_Migrants_Headcount": parse_numeric_cell(c_mig),
                                    "Median_Age_Years": parse_numeric_cell(c_age),
                                    "Fertility_Rate_Births_Per_Woman": parse_numeric_cell(c_fert),
                                    "Urban_Share_Fraction": (urb_pct_raw / 100.0) if urb_pct_raw is not None else None,
                                    "Urban_Population_Headcount": parse_numeric_cell(c_urb_pop),
                                })
            if len(wm_rows) >= 6:
                table_parsed = True
                wm_rows.sort(key=lambda x: x["Year"])
    except Exception:
        pass

    if len(wm_rows) < 6:
        wm_rows = []
        for yr, pop, pct, chg, mig, age, fert, urb_sh, urb_p in fallback_wm:
            wm_rows.append({
                "Year": yr,
                "WM_Population_Headcount": pop,
                "Yearly_Growth_Fraction": pct,
                "Yearly_Net_Change_Headcount": chg,
                "Net_Migrants_Headcount": mig,
                "Median_Age_Years": age,
                "Fertility_Rate_Births_Per_Woman": fert,
                "Urban_Share_Fraction": urb_sh,
                "Urban_Population_Headcount": urb_p,
            })

    record("population", "OK" if live_parsed and table_parsed else "FALLBACK",
           detail="Page values parsed" if live_parsed and table_parsed else "Live counter or table uses typed-in fallback",
           source=url, source_as_of=None)
    return live_counter, pd.DataFrame(wm_rows)


def fetch_bhas_headline_kpis():
    record("bhas", "UNVERIFIED", detail="Mixed parsed and typed-in defaults; field dates not validated")
    url = "https://bhas.gov.ba/?lang=en"
    session = create_resilient_session(retries=2)
    try:
        resp = safe_get(session, url, timeout_tuple=(10, 30))
        resp.raise_for_status()
        text = BeautifulSoup(resp.text, "lxml").get_text(" ", strip=True)

        def extract_pattern(pattern, text_blob, scale_dots=True):
            m = re.search(pattern, text_blob)
            if not m:
                return None
            raw = m.group(1)
            return (
                float(raw.replace(".", "").replace(",", "."))
                if scale_dots
                else float(raw.replace(",", "."))
            )

        return {
            "Population_Census_Headcount": extract_pattern(r"([\d\.]+)\s+Population\s+2013", text, True) or 3531159.0,
            "GDP_Real_Growth_Fraction": (extract_pattern(r"([\d\.,]+)%\s+Gross domestic product", text, False) or 2.1) / 100.0,
            "CPI_Inflation_Fraction": (extract_pattern(r"([\d\.,]+)%\s+Inflation \(CPI\)", text, False) or 6.0) / 100.0,
            "Avg_Net_Wage_BAM": extract_pattern(r"([\d\.]+)\s*KM\s+Average net wages", text, True) or 1701.0,
            "Imports_Goods_I_IX_Thousand_BAM": extract_pattern(r"([\d\.]+)\s+Imports of goods", text, True) or 24153874.0,
            "Exports_Goods_I_IX_Thousand_BAM": extract_pattern(r"([\d\.]+)\s+Exports of goods", text, True) or 13397484.0,
            "Status": "UNVERIFIED",
        }
    except Exception:
        return {
            "Population_Census_Headcount": 3531159.0,
            "GDP_Real_Growth_Fraction": 0.021,
            "CPI_Inflation_Fraction": 0.060,
            "Avg_Net_Wage_BAM": 1701.0,
            "Imports_Goods_I_IX_Thousand_BAM": 24153874.0,
            "Exports_Goods_I_IX_Thousand_BAM": 13397484.0,
            "Status": "UNVERIFIED",
        }


def fetch_worldbank_bih_macro(start_year=2020, end_year=2025):
    indicators = {
        "SP.POP.TOTL": "WB_Population_Headcount",
        "NY.GDP.MKTP.CD": "GDP_USD_Million",
        "NE.EXP.GNFS.CD": "Exports_USD_Million",
        "NE.IMP.GNFS.CD": "Imports_USD_Million",
    }
    records = {}
    parsed_codes = set()
    session = create_resilient_session(retries=2, use_cffi=False)
    for code, col_name in indicators.items():
        api_url = f"https://api.worldbank.org/v2/country/BIH/indicator/{code}?date={start_year}:{end_year}&format=json"
        try:
            r = session.get(api_url, timeout=(10, 20)).json()
            if isinstance(r, list) and len(r) > 1:
                data_entries = r[-1]
                if isinstance(data_entries, list):
                    for item in data_entries:
                        if isinstance(item, dict):
                            yr = int(item.get("date", 0))
                            val = item.get("value")
                            if val is not None and yr > 0:
                                scale = 1.0 if code == "SP.POP.TOTL" else 1e6
                                records.setdefault(yr, {})[col_name] = val / scale
                                parsed_codes.add(code)
        except Exception:
            continue

    if not records:
        records = {
            2020: {"WB_Population_Headcount": 3299349.0, "GDP_USD_Million": 20220.0, "Exports_USD_Million": 6950.0, "Imports_USD_Million": 9880.0},
            2021: {"WB_Population_Headcount": 3244907.0, "GDP_USD_Million": 23650.0, "Exports_USD_Million": 9820.0, "Imports_USD_Million": 12450.0},
            2022: {"WB_Population_Headcount": 3204802.0, "GDP_USD_Million": 24530.0, "Exports_USD_Million": 11200.0, "Imports_USD_Million": 14600.0},
            2023: {"WB_Population_Headcount": 3185073.0, "GDP_USD_Million": 27050.0, "Exports_USD_Million": 11150.0, "Imports_USD_Million": 15100.0},
            2024: {"WB_Population_Headcount": 3164253.0, "GDP_USD_Million": 28400.0, "Exports_USD_Million": 11400.0, "Imports_USD_Million": 15900.0},
        }

    record("worldbank", "OK" if len(parsed_codes) == len(indicators) else "PARTIAL_OR_FALLBACK",
           detail="Historical values, not today's observation", indicators_parsed=sorted(parsed_codes),
           requested_years=[start_year, end_year])
    df = pd.DataFrame.from_dict(records, orient="index").sort_index()
    df.index.name = "Year"
    return df.reset_index()


def sync_github_master_csv(
    dam_curve,
    fx_data,
    wm_live_pop,
    wm_pop_df,
    nos_status,
    csv_path="bih_data.csv",
    status_path="status.json",
):
    if not os.path.exists(csv_path):
        record("master", "FAILED", detail="Master CSV missing; sync skipped")
        print(f"   [NOTICE] {csv_path} not found in current folder; skipping CSV sync.")
        return

    df = pd.read_csv(csv_path)

    if (DATASETS.get("population", {}).get("state") == "OK"
            and len(wm_pop_df[wm_pop_df["Year"] == 2026])):
        # 1. Update BIH-0092 (Population: Worldometer Live + 2026 Mid-Yr + BHAS)
        wm_2026_rows = wm_pop_df[wm_pop_df["Year"] == 2026]
        wm_2026_val = int(wm_2026_rows["WM_Population_Headcount"].iloc[0])
        m_pop = df["Record_ID"] == "BIH-0092"
        df.loc[m_pop, "Primary_Value"] = (
            f"{int(wm_live_pop):,} (Worldometer Live) / {wm_2026_val:,} (2026 Mid-Yr) / 3,531,159 (BHAS 2013 Census)"
        )
        df.loc[m_pop, "Tariff_Price_Rate_Value"] = f"{int(wm_live_pop):,} Live | {wm_2026_val:,} (2026 Mid-Yr)"
        # Keep the older secondary narrative unchanged; it is not re-verified.

    if fx_data.get("Status") == "OK":
        # 2. Update BIH-0128 (CBBH Live USD/BAM Spot & Buy/Sell)
        m_fx = df["Record_ID"] == "BIH-0128"
        df.loc[m_fx, "Primary_Value"] = f"1.6957 (Avg) / {fx_data['USD_BAM_Middle']:.4f} (CBBH parsed, source date unverified)"
        df.loc[m_fx, "Secondary_Value"] = (
            f"CBBH Buy: {fx_data['USD_BAM_Buy']:.4f} | Middle: {fx_data['USD_BAM_Middle']:.4f} | "
            f"Sell: {fx_data['USD_BAM_Sell']:.4f} BAM/USD | Fixed Peg: 1.95583 BAM/EUR"
        )

    # 3. Update BIH-0487 from the DAM curve only when it was fetched. Otherwise keep the previous value.
    tou_mt_bam = round(115.0 + 18.5, 2)
    tou_vt_bam = round(115.0 * 1.35 + 18.5, 2)
    if dam_curve:
        dam_avg = round(sum(dam_curve) / len(dam_curve), 2)
        dam_peak = round(max(dam_curve), 2)
        dam_min = round(min(dam_curve), 2)
        m_dam = df["Record_ID"] == "BIH-0487"
        df.loc[m_dam, "Primary_Value"] = dam_avg
        df.loc[m_dam, "Secondary_Value"] = (
            f"Peak: {dam_peak:.2f} EUR/MWh | Trough: {dam_min:.2f} EUR/MWh | "
            f"ToU VT: {tou_vt_bam:.2f} BAM/MWh | MT: {tou_mt_bam:.2f} BAM/MWh"
        )
        df.loc[m_dam, "Tariff_Price_Rate_Value"] = f"{dam_avg:.2f} EUR/MWh ({dam_avg * 1.95583:.2f} BAM/MWh)"
    else:
        print("   [NOTICE] DAM curve unavailable; BIH-0487 left unchanged.")

    # 4. Dispatch rows (BIH-0488 onward) are stored in nos_hourly.csv, not here.
    from pipeline_status import atomic_text
    atomic_text(csv_path, df.to_csv(index=False))

    write_status(status_path, rows=len(df), legacy=nos_status)

    print(f"   [SUCCESS] Updated {csv_path} ({len(df)} rows) and {status_path}.")


def build_bih_intelligence_workbook(
    nos_day_df,
    nos_day_date,
    nos_status,
    bal_df,
    fx_data,
    bhas_kpis,
    wb_macro_df,
    wm_live_pop,
    wm_pop_df,
    output_filename="BiH_Energy_Macro_Intelligence.xlsx",
):
    wb = openpyxl.Workbook()
    font_family = "Times New Roman"
    f_banner = Font(name=font_family, size=11, bold=True, color="000000")
    f_header = Font(name=font_family, size=10, bold=True, color="FFFFFF")
    f_data = Font(name=font_family, size=10, bold=False)
    f_bold = Font(name=font_family, size=10, bold=True)

    fill_navy = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    fill_banner = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
    fill_input = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")

    thin_border = Border(
        left=Side(style="thin", color="D9D9D9"),
        right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"),
        bottom=Side(style="thin", color="D9D9D9"),
    )

    def _v(x):
        # Missing values are written as empty cells, never as NaN.
        if x is None:
            return None
        try:
            return None if pd.isna(x) else x
        except (TypeError, ValueError):
            return x

    def add_insight_block(ws, insight, benchmark, action, source):
        ws.cell(1, 1, "INSIGHT BLOCK").font = f_banner
        ws.cell(1, 1).fill = fill_banner
        ws.cell(2, 1, "Insight:")
        ws.cell(2, 2, insight)
        ws.cell(3, 1, "Benchmark:")
        ws.cell(3, 2, benchmark)
        ws.cell(4, 1, "Action:")
        ws.cell(4, 2, action)
        ws.cell(5, 1, "Source:")
        ws.cell(5, 2, source)
        for r in range(2, 6):
            ws.cell(r, 1).font = f_bold
            ws.cell(r, 2).font = f_data

    # TAB 1: NOS_BiH_Dispatch_DAM (latest day in nos_hourly.csv, computed from data)
    ws1 = wb.active
    ws1.title = "NOS_BiH_Dispatch_DAM"

    plan_gen = nos_day_df["Planned_Production_MW"].sum(skipna=True)
    plan_cons = nos_day_df["Planned_Consumption_MW"].sum(skipna=True)
    net_plan = plan_gen - plan_cons
    dam_series = nos_day_df["DAM_Price_EUR_MWh"].dropna()
    dam_peak_txt = f"{dam_series.max():.2f} EUR/MWh" if len(dam_series) else "unavailable"
    day_txt = nos_day_date.isoformat() if nos_day_date else "no data yet"

    add_insight_block(
        ws1,
        f"NOS BiH planned generation for {day_txt} is {plan_gen:,.0f} MWh vs planned consumption of {plan_cons:,.0f} MWh "
        f"({net_plan:+,.0f} MWh net); DAM peaks at {dam_peak_txt}.",
        "ENTSO-E BiH Control Area (10YBA-JPCC-----B) & CROPEX/SEEPEX Coupled Spot Benchmark.",
        "Charge BESS during the midday solar trough and discharge into the evening peak load.",
        f"NOS BiH Graph Consumption page (admin-ajax), fetched "
        f"{nos_status.get('nos_fetched_at', 'unknown')}. DAM reference: energy-charts.info (RS zone).",
    )

    inputs_t1 = (
        ("Currency Board Peg (BAM per EUR)", fx_data["EUR_BAM_Middle"], "0.00000"),
        ("CBBH Official Middle Rate (BAM per USD)", fx_data["USD_BAM_Middle"], "0.00000"),
        ("Base Off-Peak Industrial Tariff MT (BAM/MWh)", 115.00, "0.00"),
        ("Peak Tariff Uplift VT (%)", 0.35, "0.0%"),
        ("DERK Transmission & ISO Fee (BAM/MWh)", 18.50, "0.00"),
    )
    for idx_in, (lbl, val, num_fmt) in enumerate(inputs_t1):
        row_num = 7 + idx_in
        ws1.cell(row_num, 1, lbl).font = f_bold
        c_in = ws1.cell(row_num, 2, val)
        c_in.font = f_bold
        c_in.fill = fill_input
        c_in.number_format = num_fmt

    ws1.cell(13, 1, "TABLE 1 (FEEDS CHART 1): HOURLY GENERATION, CONSUMPTION, DAY-AHEAD MARKET (DAM) & TARIFFS").font = f_banner
    ws1.cell(13, 1).fill = fill_banner

    headers_t1 = (
        "Hour Ending (Hour)",
        "Planned Generation (MW)",
        "Actual Generation (MW)",
        "Planned Consumption (MW)",
        "Actual Consumption (MW)",
        "Planned Net Export (MW)",
        "Actual Net Export (MW)",
        "DAM Spot Price (EUR/MWh)",
        "DAM Spot Price (BAM/MWh)",
        "Peak ToU Regime Flag (1=VT, 0=MT)",
        "All-In Hourly Grid Tariff (BAM/MWh)",
        "Planned Export Revenue (EUR Thousand)",
        "Generation Share of Planned (%)",
    )
    header_r1 = 14
    for col_idx, h_text in enumerate(headers_t1, start=1):
        c = ws1.cell(header_r1, col_idx, h_text)
        c.font = f_header
        c.fill = fill_navy
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws1.freeze_panes = "A15"
    start_r1 = 15
    num_rows1 = len(nos_day_df)
    tot_r1 = start_r1 + num_rows1

    for i, row in nos_day_df.iterrows():
        r_idx = start_r1 + i
        ws1.cell(r_idx, 1, int(row["Hour_Index"])).number_format = "0"
        ws1.cell(r_idx, 2, _v(row["Planned_Production_MW"])).number_format = "#,##0.00"
        ws1.cell(r_idx, 3, _v(row["Actual_Production_MW"])).number_format = "#,##0.00"
        ws1.cell(r_idx, 4, _v(row["Planned_Consumption_MW"])).number_format = "#,##0.00"
        ws1.cell(r_idx, 5, _v(row["Actual_Consumption_MW"])).number_format = "#,##0.00"
        ws1.cell(r_idx, 6, f"=B{r_idx}-D{r_idx}").number_format = "#,##0.00"
        ws1.cell(r_idx, 7, f"=IF(AND(ISNUMBER(C{r_idx}),ISNUMBER(E{r_idx})),C{r_idx}-E{r_idx},0)").number_format = "#,##0.00"
        ws1.cell(r_idx, 8, _v(row["DAM_Price_EUR_MWh"])).number_format = "#,##0.00"
        ws1.cell(r_idx, 9, f"=H{r_idx}*$B$7").number_format = "#,##0.00"
        ws1.cell(r_idx, 10, int(row["Peak_ToU_Flag"])).number_format = "0"
        ws1.cell(r_idx, 11, f"=$B$9*(1+J{r_idx}*$B$10)+$B$11").number_format = "#,##0.00"
        ws1.cell(r_idx, 12, f"=F{r_idx}*H{r_idx}/1000").number_format = "#,##0.00"
        ws1.cell(r_idx, 13, f"=IFERROR(B{r_idx}/$B${tot_r1}, 0)").number_format = "0.0%"
        for c_i in range(1, 14):
            ws1.cell(r_idx, c_i).font = f_data
            ws1.cell(r_idx, c_i).border = thin_border

    ws1.cell(tot_r1, 1, "Total / Daily Average").font = f_bold
    for col_i in (2, 3, 4, 5, 6, 7, 10, 12):
        col_let = get_column_letter(col_i)
        cell = ws1.cell(tot_r1, col_i, f"=SUM({col_let}{start_r1}:{col_let}{tot_r1-1})")
        cell.font = f_bold
        cell.number_format = "#,##0.00" if col_i != 10 else "0"
    for col_i in (8, 9, 11):
        col_let = get_column_letter(col_i)
        cell = ws1.cell(tot_r1, col_i, f"=AVERAGE({col_let}{start_r1}:{col_let}{tot_r1-1})")
        cell.font = f_bold
        cell.number_format = "#,##0.00"
    ws1.cell(tot_r1, 13, f"=SUM(M{start_r1}:M{tot_r1-1})").number_format = "0.0%"
    ws1.cell(tot_r1, 13).font = f_bold

    rec_r1 = tot_r1 + 1
    ws1.cell(rec_r1, 1, "Reconciliation Check (Parts = Total)").font = f_bold
    ws1.cell(rec_r1, 6, f"=ROUND(B{tot_r1}-D{tot_r1},2)=ROUND(F{tot_r1},2)").font = f_bold
    ws1.cell(rec_r1, 13, f"=ROUND(M{tot_r1},4)=1").font = f_bold

    # TAB 2: Balancing_aFRR_mFRR
    ws2 = wb.create_sheet("Balancing_aFRR_mFRR")
    add_insight_block(
        ws2,
        "Upward mFRR averages 796.30 KM/MWh (407.14 EUR/MWh) vs aFRR Upward at 153.26 KM/MWh; downward mFRR averages -364.85 KM/MWh.",
        "SERC Statutory Price Caps: mFRR Up Cap = 989.46 KM/MWh; mFRR Down Floor = -401.28 KM/MWh; aFRR Capacity Cap = 43.00 KM/MW/h.",
        "Pre-qualify utility-scale BESS for aFRR capacity and upward/downward mFRR energy arbitrage.",
        "NOS BiH Monthly Balancing Market Report & SERC Decisions - VERIFIED",
    )
    ws2.cell(7, 1, "Currency Board Peg (BAM per EUR)").font = f_bold
    ws2.cell(7, 2, "=NOS_BiH_Dispatch_DAM!B7").font = f_bold
    ws2.cell(7, 2).fill = fill_input
    ws2.cell(7, 2).number_format = "0.00000"
    ws2.cell(8, 1, "CBBH Official Middle Rate (BAM per USD)").font = f_bold
    ws2.cell(8, 2, "=NOS_BiH_Dispatch_DAM!B8").font = f_bold
    ws2.cell(8, 2).fill = fill_input
    ws2.cell(8, 2).number_format = "0.00000"

    ws2.cell(10, 1, "TABLE 2 (FEEDS CHART 2): NOS BIH BALANCING ENERGY (FCR, aFRR, mFRR) & SERC CAPS").font = f_banner
    ws2.cell(10, 1).fill = fill_banner

    headers_t2 = (
        "Balancing Reserve Product",
        "Direction Indicator (1=Up, -1=Down)",
        "Activated Energy (MWh)",
        "Total Energy Settlement (BAM)",
        "Weighted Average Price (BAM/MWh)",
        "Weighted Average Price (EUR/MWh)",
        "Weighted Average Price (USD/MWh)",
        "Monthly Extreme Max/Min Price (BAM/MWh)",
        "SERC Statutory Energy Cap/Floor (BAM/MWh)",
        "SERC Capacity Price Cap (BAM/MW/h)",
        "Share of Activated Volume (%)",
    )
    for col_idx, h_text in enumerate(headers_t2, start=1):
        c = ws2.cell(11, col_idx, h_text)
        c.font = f_header
        c.fill = fill_navy
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws2.freeze_panes = "A12"
    start_r2 = 12
    tot_r2 = start_r2 + len(bal_df)
    for i, row in bal_df.iterrows():
        r_idx = start_r2 + i
        ws2.cell(r_idx, 1, row["Reserve_Category"])
        ws2.cell(r_idx, 2, row["Direction"]).number_format = "0"
        ws2.cell(r_idx, 3, row["Activated_Energy_MWh"]).number_format = "#,##0.00"
        ws2.cell(r_idx, 4, row["Total_Energy_Cost_BAM"]).number_format = "#,##0.00"
        ws2.cell(r_idx, 5, f"=IFERROR(D{r_idx}/C{r_idx}, 0)").number_format = "#,##0.00"
        ws2.cell(r_idx, 6, f"=E{r_idx}/$B$7").number_format = "#,##0.00"
        ws2.cell(r_idx, 7, f"=E{r_idx}/$B$8").number_format = "#,##0.00"
        ws2.cell(r_idx, 8, row["Extreme_Max_Min_Price_BAM_MWh"]).number_format = "#,##0.00"
        ws2.cell(r_idx, 9, row["SERC_Energy_Price_Cap_BAM_MWh"]).number_format = "#,##0.00"
        ws2.cell(r_idx, 10, row["SERC_Capacity_Cap_BAM_MW_h"]).number_format = "#,##0.00"
        ws2.cell(r_idx, 11, f"=IFERROR(C{r_idx}/$C${tot_r2}, 0)").number_format = "0.0%"
        for c_i in range(1, 12):
            ws2.cell(r_idx, c_i).font = f_data
            ws2.cell(r_idx, c_i).border = thin_border

    ws2.cell(tot_r2, 1, "Total / Weighted System Balancing").font = f_bold
    ws2.cell(tot_r2, 3, f"=SUM(C{start_r2}:C{tot_r2-1})").number_format = "#,##0.00"
    ws2.cell(tot_r2, 4, f"=SUM(D{start_r2}:D{tot_r2-1})").number_format = "#,##0.00"
    ws2.cell(tot_r2, 5, f"=IFERROR(D{tot_r2}/C{tot_r2}, 0)").number_format = "#,##0.00"
    ws2.cell(tot_r2, 6, f"=E{tot_r2}/$B$7").number_format = "#,##0.00"
    ws2.cell(tot_r2, 7, f"=E{tot_r2}/$B$8").number_format = "#,##0.00"
    ws2.cell(tot_r2, 11, f"=SUM(K{start_r2}:K{tot_r2-1})").number_format = "0.0%"
    for c_i in (3, 4, 5, 6, 7, 11):
        ws2.cell(tot_r2, c_i).font = f_bold
    rec_r2 = tot_r2 + 1
    ws2.cell(rec_r2, 1, "Reconciliation Check (Shares = 100%)").font = f_bold
    ws2.cell(rec_r2, 11, f"=ROUND(K{tot_r2},4)=1").font = f_bold

    # TAB 3: BiH_Macro_FX_Trade
    ws3 = wb.create_sheet("BiH_Macro_FX_Trade")
    add_insight_block(
        ws3,
        "BiH cumulative goods deficit stands at 10.76B BAM (5.50B EUR / 6.04B USD) for I-IX 2026; CBBH USD/BAM middle rate is 1.779645.",
        "CBBH Fixed Peg: 1 EUR = 1.955830 BAM | CBBH Official USD Middle Rate: 1 USD = 1.779645 BAM.",
        "Structure IPP PPA revenues in EUR while hedging USD-denominated PV module and BESS EPC capex.",
        "Agency for Statistics of BiH (BHAS) & Central Bank of BiH (CBBH) - VERIFIED",
    )
    fx_inputs = (
        ("CBBH Fixed Currency Board Peg (BAM per EUR)", fx_data["EUR_BAM_Middle"], "0.000000"),
        ("CBBH USD Buy Rate (BAM per USD)", fx_data["USD_BAM_Buy"], "0.000000"),
        ("CBBH USD Middle Rate (BAM per USD)", fx_data["USD_BAM_Middle"], "0.000000"),
        ("CBBH USD Sell Rate (BAM per USD)", fx_data["USD_BAM_Sell"], "0.000000"),
    )
    for idx_fx, (lbl, val, fmt) in enumerate(fx_inputs):
        r_fx = 7 + idx_fx
        ws3.cell(r_fx, 1, lbl).font = f_bold
        c_fx = ws3.cell(r_fx, 2, val)
        c_fx.font = f_bold
        c_fx.fill = fill_input
        c_fx.number_format = fmt

    ws3.cell(12, 1, "TABLE 3 (FEEDS CHART 3): BHAS EXTERNAL TRADE IN BAM, EUR, AND USD (I-IX 2026)").font = f_banner
    ws3.cell(12, 1).fill = fill_banner
    headers_t3 = ("External Trade Indicator", "Value (BAM Million)", "Value (EUR Million)", "Value (USD Million)", "Share of Total Trade (%)")
    for col_idx, h_text in enumerate(headers_t3, start=1):
        c = ws3.cell(13, col_idx, h_text)
        c.font = f_header
        c.fill = fill_navy
    ws3.freeze_panes = "A14"
    exp_m = (bhas_kpis.get("Exports_Goods_I_IX_Thousand_BAM") or 13397484.0) / 1000.0
    imp_m = (bhas_kpis.get("Imports_Goods_I_IX_Thousand_BAM") or 24153874.0) / 1000.0
    for idx, (label, val_bam_m) in enumerate((("Exports_Goods_I_IX_2026", exp_m), ("Imports_Goods_I_IX_2026", imp_m)), start=14):
        ws3.cell(idx, 1, label).font = f_data
        ws3.cell(idx, 2, val_bam_m).number_format = "#,##0.00"
        ws3.cell(idx, 3, f"=B{idx}/$B$7").number_format = "#,##0.00"
        ws3.cell(idx, 4, f"=B{idx}/$B$9").number_format = "#,##0.00"
        ws3.cell(idx, 5, f"=IFERROR(B{idx}/$B$16, 0)").number_format = "0.0%"
        for c_i in range(1, 6):
            ws3.cell(idx, c_i).font = f_data
            ws3.cell(idx, c_i).border = thin_border
    ws3.cell(16, 1, "Total_Trade_Volume_I_IX_2026").font = f_bold
    ws3.cell(16, 2, "=SUM(B14:B15)").number_format = "#,##0.00"
    ws3.cell(16, 3, "=SUM(C14:C15)").number_format = "#,##0.00"
    ws3.cell(16, 4, "=SUM(D14:D15)").number_format = "#,##0.00"
    ws3.cell(16, 5, "=SUM(E14:E15)").number_format = "0.0%"
    for c_i in range(2, 6):
        ws3.cell(16, c_i).font = f_bold
    ws3.cell(17, 1, "Net_Trade_Balance_I_IX_2026").font = f_bold
    ws3.cell(17, 2, "=B14-B15").number_format = "#,##0.00"
    ws3.cell(17, 3, "=C14-C15").number_format = "#,##0.00"
    ws3.cell(17, 4, "=D14-D15").number_format = "#,##0.00"
    for c_i in range(2, 5):
        ws3.cell(17, c_i).font = f_bold
    ws3.cell(18, 1, "Reconciliation Check (Shares = 100%)").font = f_bold
    ws3.cell(18, 5, "=ROUND(E16,4)=1").font = f_bold

    # TAB 4: Demographics_Worldometer_WB
    ws4 = wb.create_sheet("Demographics_Worldometer_WB")
    add_insight_block(
        ws4,
        "Worldometer estimates BiH 2026 mid-year population at 3,114,242 (live counter: 3,106,384; -0.82% YoY) vs BHAS 2013 Census baseline of 3,531,159.",
        "UN DESA Population Division (Worldometer Elaboration) Cross-Checked Against BHAS 2013 Census & World Bank WDI.",
        "Model long-term domestic load growth using UN/Worldometer demographic contraction alongside industrial electrification.",
        "Worldometer (https://www.worldometers.info/world-population/bosnia-and-herzegovina-population/) - ESTIMATED / VERIFIED",
    )
    pop_inputs = (
        ("BHAS Official 2013 Census Baseline (Headcount)", bhas_kpis["Population_Census_Headcount"], "#,##0"),
        ("Worldometer Live RTS Counter (Headcount)", wm_live_pop, "#,##0"),
        ("Reference CBBH USD Middle Rate (BAM per USD)", "=BiH_Macro_FX_Trade!B9", "0.000000"),
    )
    for idx_p, (lbl, val, fmt) in enumerate(pop_inputs):
        r_p = 7 + idx_p
        ws4.cell(r_p, 1, lbl).font = f_bold
        c_p = ws4.cell(r_p, 2, val)
        c_p.font = f_bold
        c_p.fill = fill_input
        c_p.number_format = fmt

    ws4.cell(11, 1, "TABLE 4 (FEEDS CHART 4): WORLDOMETER BIH POPULATION (2020-2050 FORECAST) & WORLD BANK MACRO").font = f_banner
    ws4.cell(11, 1).fill = fill_banner
    headers_t4 = (
        "Year",
        "Worldometer Mid-Year Population (Headcount)",
        "Variance vs BHAS 2013 Census (Headcount)",
        "Yearly Population Growth (%)",
        "Yearly Net Change (Headcount)",
        "Net Migrants (Headcount)",
        "Median Age (Years)",
        "Fertility Rate (Births/Woman)",
        "Urban Population Share (%)",
        "Urban Population (Headcount)",
        "Rural Population Derived (Headcount)",
        "World Bank Nominal GDP (USD Million)",
        "World Bank Nominal GDP (BAM Million)",
    )
    for col_idx, h_text in enumerate(headers_t4, start=1):
        c = ws4.cell(12, col_idx, h_text)
        c.font = f_header
        c.fill = fill_navy
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws4.freeze_panes = "A13"
    wb_gdp_map = {int(w_row["Year"]): w_row.get("GDP_USD_Million") for _, w_row in wb_macro_df.iterrows()}
    start_r4 = 13
    tot_r4 = start_r4 + len(wm_pop_df)
    for i, row in wm_pop_df.iterrows():
        r_idx = start_r4 + i
        yr_int = int(row["Year"])
        ws4.cell(r_idx, 1, yr_int).number_format = "0"
        ws4.cell(r_idx, 2, row["WM_Population_Headcount"]).number_format = "#,##0"
        ws4.cell(r_idx, 3, f"=B{r_idx}-$B$7").number_format = "#,##0"
        ws4.cell(r_idx, 4, row["Yearly_Growth_Fraction"]).number_format = "0.00%"
        ws4.cell(r_idx, 5, row["Yearly_Net_Change_Headcount"]).number_format = "#,##0"
        ws4.cell(r_idx, 6, row["Net_Migrants_Headcount"]).number_format = "#,##0"
        ws4.cell(r_idx, 7, row["Median_Age_Years"]).number_format = "0.0"
        ws4.cell(r_idx, 8, row["Fertility_Rate_Births_Per_Woman"]).number_format = "0.00"
        ws4.cell(r_idx, 9, row["Urban_Share_Fraction"]).number_format = "0.0%"
        ws4.cell(r_idx, 10, row["Urban_Population_Headcount"]).number_format = "#,##0"
        ws4.cell(r_idx, 11, f"=B{r_idx}-J{r_idx}").number_format = "#,##0"
        ws4.cell(r_idx, 12, wb_gdp_map.get(yr_int)).number_format = "#,##0.00"
        ws4.cell(r_idx, 13, f"=IF(ISNUMBER(L{r_idx}), L{r_idx}*$B$9, 0)").number_format = "#,##0.00"
        for c_i in range(1, 14):
            ws4.cell(r_idx, c_i).font = f_data
            ws4.cell(r_idx, c_i).border = thin_border

    ws4.cell(tot_r4, 1, "Multi-Period Average / Cumulative").font = f_bold
    for col_i in (2, 3, 4, 7, 8, 9, 10, 11):
        col_let = get_column_letter(col_i)
        cell = ws4.cell(tot_r4, col_i, f"=AVERAGE({col_let}{start_r4}:{col_let}{tot_r4-1})")
        cell.font = f_bold
        cell.number_format = "0.00%" if col_i in (4, 9) else ("0.00" if col_i in (7, 8) else "#,##0")
    for col_i in (5, 6, 12, 13):
        col_let = get_column_letter(col_i)
        cell = ws4.cell(tot_r4, col_i, f"=SUM({col_let}{start_r4}:{col_let}{tot_r4-1})")
        cell.font = f_bold
        cell.number_format = "#,##0" if col_i in (5, 6) else "#,##0.00"
    rec_r4 = tot_r4 + 1
    ws4.cell(rec_r4, 1, "Reconciliation Check (Urban + Rural = Total)").font = f_bold
    ws4.cell(rec_r4, 11, f"=ROUND(J{tot_r4}+K{tot_r4},0)=ROUND(B{tot_r4},0)").font = f_bold

    # TAB 5: Sources
    ws5 = wb.create_sheet("Sources")
    add_insight_block(
        ws5,
        "Master registry tracking all institutional endpoints, FX bulletins, balancing reports, and demographic series.",
        "Strict Source Hierarchy: National TSO (NOS BiH), State Regulator (DERK), Central Bank (CBBH), BHAS, and UN/Worldometer.",
        "Audit monthly after the 20th to capture new NOS BiH balancing PDFs and BHAS foreign trade releases.",
        "Internal Audit & Traceability Framework - VERIFIED",
    )
    ws5.cell(7, 1, "TABLE 5 (FEEDS CHART 5): MASTER SOURCE TRACEABILITY REGISTER").font = f_banner
    ws5.cell(7, 1).fill = fill_banner
    s_headers = ("Input Variable", "Exact URL", "Publisher", "Publication / Update Date", "Page / Table / Section", "Status Label")
    for col_idx, h_text in enumerate(s_headers, start=1):
        c = ws5.cell(8, col_idx, h_text)
        c.font = f_header
        c.fill = fill_navy
    ws5.freeze_panes = "A9"
    sources_list = (
        ("Hourly Dispatch Table (24h Prod & Cons MW)", NOS_PAGE, "NOS BiH", datetime.date.today().isoformat(), "Graph Consumption page, admin-ajax productionTable", "LIVE"),
        ("FCR, aFRR & mFRR Balancing Energy Prices", "https://www.nosbih.ba/files/balansno/izvjestaji/mjeseci/202508-en-balansno-trziste-izvjestaj.pdf", "NOS BiH", "2025-09-15", "Page 1, Table: Activated balancing energy", "VERIFIED"),
        ("SERC Statutory Price Caps (aFRR & mFRR)", "https://www.nosbih.ba/files/balansno/izvjestaji/godine/2024-en-balansno-trziste-izvjestaj.pdf", "NOS BiH / SERC", "2025-03-01", "Page 4, Section: Ancillary services", "VERIFIED"),
        ("CBBH Daily FX List (USD/BAM & EUR/BAM Peg)", "https://cbbh.ba/CurrencyExchange/", "Central Bank of BiH (CBBH)", "2026-11-21", "Daily Exchange Rates List No. 230", "VERIFIED"),
        ("BiH Population (2020-2050 & Live Counter)", "https://www.worldometers.info/world-population/bosnia-and-herzegovina-population/", "Worldometer (UN DESA)", "2026-11-21", "Historical & Forecast Tables", "ESTIMATED"),
        ("BHAS 2013 Census & Cumulative Trade", "https://bhas.gov.ba/Calendar/Category/11", "Agency for Statistics of BiH (BHAS)", "2026-11-20", "Foreign Trade Goods Release", "VERIFIED"),
        ("World Bank Multi-Year Macro Series", "https://api.worldbank.org/v2/country/BIH", "World Bank WDI", datetime.date.today().isoformat(), "Indicators: SP.POP.TOTL, NY.GDP.MKTP.CD", "VERIFIED"),
        ("Hourly Day-Ahead Market (DAM) Reference Curve", "https://api.energy-charts.info/price?bzn=RS", "Energy-Charts / SEEPEX-CROPEX", datetime.date.today().isoformat(), "Tab 1 Column H (Regional SEE Coupled Spot)", "ESTIMATED"),
    )
    for r_i, s_row in enumerate(sources_list, start=9):
        for c_i, val in enumerate(s_row, start=1):
            cell = ws5.cell(r_i, c_i, val)
            cell.font = f_data
            cell.border = thin_border

    for sheet in wb.worksheets:
        for col in sheet.columns:
            first_cell = next(iter(col))
            max_len = max(len(str(cell.value or "")) for cell in col)
            sheet.column_dimensions[get_column_letter(first_cell.column)].width = min(max(max_len + 4, 14), 52)

    wb.save(output_filename)
    return output_filename


def main():
    """Update CSVs and status. Legacy standalone XLSX generation is opt-in.

    That workbook still contains static narrative/labels and is not the user's
    macro dashboard. Do not treat it as validated output of these new feeds.
    """
    def call(name, func, default):
        try:
            return func()
        except Exception as exc:
            record(name, "FAILED", detail=type(exc).__name__)
            return default

    df_bal = call("balancing", fetch_nosbih_balancing_prices, pd.DataFrame())
    fx_rates = call("fx", fetch_cbbh_fx_rates, {})
    wm_live, df_wm_pop = call("population", fetch_worldometer_bih_population,
                              (None, pd.DataFrame(columns=["Year"])))
    bhas_data = call("bhas", fetch_bhas_headline_kpis, {})
    wb_macro = call("worldbank", fetch_worldbank_bih_macro, pd.DataFrame())
    nos_status = call("nos", update_nos_history, {"nos_error": "Fetch failed"})
    if "nos" not in DATASETS:
        record("nos", "FAILED" if nos_status.get("nos_days_failed") else "OK",
               detail="See NOS request metadata", **nos_status)
    # Existing DAM parser drops timestamps and cannot prove a delivery day.
    # Disable publication until a timestamp-aware adapter is reviewed.
    record("dam", "UNVERIFIED", detail="Legacy positional price parser disabled for publication")
    dam_curve = None
    flows_status = call("flows", update_flows_history, {"flows_error": "Fetch failed"})
    if "flows" not in DATASETS:
        stamp = datetime.datetime.fromisoformat(flows_status["flows_timestamp"]).replace(tzinfo=NOS_TZ)
        age = (datetime.datetime.now(NOS_TZ).astimezone(datetime.timezone.utc) - stamp.astimezone(datetime.timezone.utc)).total_seconds()
        state = "OK" if flows_status.get("flows_checks_ok") and 0 <= age <= 7200 else "STALE_OR_INVALID"
        record("flows", state, detail="Two-hour freshness ceiling; separate from historical ENTSO-E", **flows_status)
    nos_status.update(flows_status)
    # ENTSO-E runs in its own daily workflow (entsoe_daily.yml), not here.
    try:
        sync_github_master_csv(dam_curve, fx_rates, wm_live, df_wm_pop, nos_status)
    except Exception as exc:
        record("master", "FAILED", detail=type(exc).__name__)
    # Status still lands if the master CSV is absent or sync failed.
    rows = len(pd.read_csv("bih_data.csv")) if os.path.exists("bih_data.csv") else None
    write_status("status.json", rows=rows, legacy=nos_status)
    if os.environ.get("BUILD_LEGACY_XLSX") == "1":
        nos_hist = load_nos_history()
        nos_day_df, nos_day_date = nos_day_for_workbook(nos_hist, dam_curve)
        build_bih_intelligence_workbook(nos_day_df, nos_day_date, nos_status,
            df_bal, fx_rates, bhas_data, wb_macro, wm_live, df_wm_pop)
    print("CSV update finished. See per-dataset status; this is not full-master verification.")


if __name__ == "__main__":
    main()
