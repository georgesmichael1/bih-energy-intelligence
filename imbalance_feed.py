"""NOS BiH 15-minute imbalance prices (daily and monthly Excel files).

Published in KM/MWh per 15-minute settlement period. Saved as-is plus an EUR copy
(fixed rate 1.95583). Empty cells stay empty, never zero. No ENTSO-E involved.
History comes from the monthly files, recent days from the daily files; the last
few days are re-read each run because NOS BiH revises them (Intermediate -> later versions).
"""
import csv
import datetime as dt
import io
import re
from pathlib import Path

import openpyxl
from pipeline_status import atomic_text, record

BASE = 'https://www.nosbih.ba/files/balansno/cijena/'
PAGE = 'https://www.nosbih.ba/en/trziste/balansno-trziste/cijene-debalansa/'
CSV_PATH = 'nos_imbalance_15min.csv'
START_DATE = dt.date(2025, 1, 1)
PEG = 1.95583
RECHECK_DAYS = 4
COLUMNS = ['Date', 'Seq', 'Start', 'End', 'aFRR_Up_MWh', 'aFRR_Down_MWh', 'mFRR_Up_MWh',
           'mFRR_Down_MWh', 'Price_Positive_KM_MWh', 'Price_Negative_KM_MWh',
           'Price_Positive_EUR_MWh', 'Price_Negative_EUR_MWh', 'Version_Note', 'Source']
TIME = re.compile(r'^\d{1,2}:\d{2}$')
DATE = re.compile(r'(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})')


def day_url(d):
    return f'{BASE}dani/{d:%Y%m%d}-en-imbalance-price.xlsx'


def month_url(d):
    return f'{BASE}mjeseci/{d:%Y%m}-en-imbalance-price.xlsx'


def num(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, str):
        v = v.strip().replace(',', '.')
    x = float(v)
    if x != x or x in (float('inf'), float('-inf')):
        raise ValueError('Nonfinite value')
    return x


def parse_sheet(ws, source):
    """Return (date, rows) for one day sheet. Raises ValueError on an unexpected layout."""
    rows = list(ws.iter_rows(values_only=True))
    day = None
    note = ''
    data = []
    for r in rows:
        cells = list(r) + [None] * (12 - len(r))
        if cells[1] == 'Date:' and isinstance(cells[2], str):
            m = DATE.search(cells[2])
            if m:
                day = dt.date(int(m[3]), int(m[2]), int(m[1]))
        elif isinstance(cells[1], str) and 'REGULATION-DATA' in cells[1]:
            note = cells[1].strip()
        elif isinstance(cells[1], str) and TIME.match(cells[1].strip()) and isinstance(cells[2], str):
            data.append(cells)
    if day is None:
        raise ValueError('No date in sheet')
    header = ' '.join(str(c) for c in rows[5][:12] if c) if len(rows) > 6 else ''
    for need in ('aFRR', 'mFRR', 'POSITIVE', 'NEGATIVE'):
        if need not in header:
            raise ValueError('Unexpected column header')
    if not 92 <= len(data) <= 100:
        raise ValueError(f'{day}: {len(data)} periods, expected 92 to 100')
    out = []
    for i, c in enumerate(data, 1):
        pos, neg = num(c[10]), num(c[11])
        out.append({
            'Date': day.isoformat(), 'Seq': i, 'Start': c[1].strip(), 'End': c[2].strip(),
            'aFRR_Up_MWh': num(c[4]), 'aFRR_Down_MWh': num(c[5]),
            'mFRR_Up_MWh': num(c[7]), 'mFRR_Down_MWh': num(c[8]),
            'Price_Positive_KM_MWh': pos, 'Price_Negative_KM_MWh': neg,
            'Price_Positive_EUR_MWh': None if pos is None else round(pos / PEG, 2),
            'Price_Negative_EUR_MWh': None if neg is None else round(neg / PEG, 2),
            'Version_Note': note, 'Source': source,
        })
    return day, out


def parse_workbook(content, source):
    if not content.startswith(b'PK'):
        raise ValueError('Content is not an Excel file')
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=False)
    result = {}
    for ws in wb.worksheets:
        day, rows = parse_sheet(ws, source)
        result[day] = rows
    if not result:
        raise ValueError('No sheets')
    return result


def load(path):
    p = Path(path)
    if not p.exists():
        return {}
    days = {}
    with p.open(encoding='utf-8', newline='') as f:
        for r in csv.DictReader(f):
            days.setdefault(r['Date'], []).append(r)
    return days


def save(path, days):
    out = io.StringIO(newline='')
    w = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator='\n')
    w.writeheader()
    for d in sorted(days):
        for r in days[d]:
            w.writerow({k: ('' if r.get(k) is None else r.get(k)) for k in COLUMNS})
    atomic_text(path, out.getvalue())


def fetch(get, url):
    resp = get(url)
    if getattr(resp, 'status_code', 200) == 404:
        return None
    resp.raise_for_status()
    return resp.content


def update_imbalance(get, today=None, path=CSV_PATH, start=START_DATE):
    """Update the CSV. Missing days that NOS BiH has not published yet are not an error."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    try:
        days = load(path)
        got, missing = [], []
        # 1) Complete past months from monthly files (history).
        month = start.replace(day=1)
        this_month = today.replace(day=1)
        while month < this_month:
            last = (month.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
            need = [month + dt.timedelta(n) for n in range((last - month).days + 1)]
            gaps = [d for d in need if d.isoformat() not in days]
            recent = (this_month - month).days <= 31  # re-read the latest completed month once per run
            if gaps or recent:
                url = month_url(month)
                content = fetch(get, url)
                if content is not None:
                    for d, rows in parse_workbook(content, url).items():
                        if start <= d <= today:
                            days[d.isoformat()] = rows
                            got.append(d.isoformat())
            month = last + dt.timedelta(days=1)
        # 2) Days of the current month from daily files; always re-read the last few days.
        d = max(start, this_month)
        while d < today:
            key = d.isoformat()
            if key not in days or (today - d).days <= RECHECK_DAYS:
                url = day_url(d)
                content = fetch(get, url)
                if content is None:
                    missing.append(key)
                else:
                    parsed = parse_workbook(content, url)
                    if d not in parsed:
                        raise ValueError(f'{key}: file holds another date')
                    days[key] = parsed[d]
                    got.append(key)
            d += dt.timedelta(days=1)
        # Gaps older than 2 days are a real problem; the latest day is normally published next morning.
        hard_missing = sorted(k for k in missing if (today - dt.date.fromisoformat(k)).days > 2)
        if not days:
            raise ValueError('No imbalance data available')
        save(path, days)
        latest = max(days)
        state = 'OK' if not hard_missing else 'PARTIAL'
        record('nos_imbalance_15min', state,
               detail=('Published 15-minute imbalance prices from NOS BiH, KM/MWh; latest day is normally published next morning.'
                       + (f' Not yet published: {", ".join(sorted(missing))}.' if missing else '')),
               latest_date=latest, days=len(days), rows=sum(len(v) for v in days.values()),
               source=PAGE)
        return days
    except Exception as exc:
        record('nos_imbalance_15min', 'FAILED',
               detail='Saved data retained; ' + type(exc).__name__ + ': ' + str(exc), source=PAGE)
        return None


def main():
    import json
    import requests
    from pipeline_status import DATASETS, write_status
    session = requests.Session()
    session.headers['User-Agent'] = 'Mozilla/5.0 (compatible; bih-energy-intelligence)'

    def get(url):
        last = None
        for _ in range(3):
            try:
                return session.get(url, timeout=(10, 60))
            except requests.RequestException as exc:
                last = exc
        raise last
    path = Path('status.json')
    old = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    DATASETS.clear()
    DATASETS.update(old.get('dataset_details', {}))
    update_imbalance(get)
    reserved = {'schema_version', 'run_finished_at', 'last_verified_date',
                'last_verified_date_is_legacy', 'verification_note', 'rows',
                'datasets', 'dataset_details'}
    legacy = {k: v for k, v in old.items() if k not in reserved}
    write_status(path, rows=old.get('rows'), legacy=legacy)


if __name__ == '__main__':
    main()
