"""NOSBiH published monthly energy and annual capacity, never a live tariff.

Strict product/unit/period checks; failure retains the last dated CSV. No tokens,
no changes to the separate ENTSO-E generator, no synthetic missing-data values.
"""
import csv
import datetime as dt
import io
import math
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import openpyxl
import pdfplumber
import requests
from bs4 import BeautifulSoup
from pipeline_status import atomic_text, record

INDEX = 'https://www.nosbih.ba/en/trziste/balansno-trziste/izvjestaji-o-balansnom-trzistu/'
PEG = 1.95583  # NOSBiH report currency conversion, BAM per EUR.
COLUMNS = ['Key', 'Period', 'Value', 'Unit', 'Source', 'Retrieved_UTC']
PRODUCTS = {'FCP Up': 'FCR_UP', 'FCP Down': 'FCR_DOWN', 'aFRR Up': 'AFRR_UP',
            'aFRR Down': 'AFRR_DOWN', 'mFRR Up': 'MFRR_UP', 'mFRR Down': 'MFRR_DOWN',
            'Total Up': 'TOTAL_UP', 'Total Down': 'TOTAL_DOWN'}
CAPACITY = ['FCR', 'AFRR_NIGHT', 'AFRR_PEAK', 'MFRR_UP', 'MFRR_DOWN']
MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
          'September', 'October', 'November', 'December']


def number(token):
    if not re.fullmatch(r'-?\d[\d.]*(?:,\d+)?', token):
        raise ValueError('Unexpected NOSBiH numeric format')
    value = float(token.replace('.', '').replace(',', '.'))
    if not math.isfinite(value):
        raise ValueError('Nonfinite value')
    return value


def links(html, now):
    monthly, annual = {}, {}
    for a in BeautifulSoup(html, 'html.parser').find_all('a', href=True):
        url = urljoin(INDEX, a['href'])
        if urlparse(url).hostname != 'www.nosbih.ba':
            continue
        m = re.search(r'/mjeseci/(\d{4})(\d{2})-en-balansno-trziste-izvjestaj\.pdf$', url)
        if m:
            year, month = map(int, m.groups())
            if 1 <= month <= 12 and dt.date(year, month, 1) < now.replace(day=1):
                monthly[f'{year:04}-{month:02}'] = url
        m = re.search(r'/godine/(\d{4})-en-balansno-trziste-tabele\.xlsx$', url)
        if m and int(m[1]) < now.year:
            annual[m[1]] = url
    if not monthly or not annual:
        raise ValueError('No linked completed monthly/annual report')
    month, year = max(monthly), max(annual)
    return month, monthly[month], year, annual[year]


def parse_monthly(text, period, source, stamp):
    year, month = map(int, period.split('-'))
    heading = f'Balancing market report - {MONTHS[month-1]} {year}'
    if heading.lower() not in re.sub(r'\s+', ' ', text).lower():
        raise ValueError('PDF title period mismatch')
    compact = re.sub(r'\s+', '', text)
    if 'KM/MWh' not in compact or '1EUR=1,95583KM' not in compact:
        raise ValueError('PDF currency/unit contract changed')
    found = {}
    for line in text.splitlines():
        m = re.match(r'^\s*(FCP Up|FCP Down|aFRR Up|aFRR Down|mFRR Up|mFRR Down|Total Up|Total Down|Shortage|Surplus)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$', line)
        if m:
            label = m[1]
            if label in found:
                raise ValueError('Duplicate product')
            found[label] = list(map(number, m.groups()[1:]))
    if set(found) != set(PRODUCTS) | {'Shortage', 'Surplus'}:
        raise ValueError('Missing or unexpected monthly products')
    rows = []
    def add(key, value, unit):
        rows.append(dict(zip(COLUMNS, [key, period, round(value, 8), unit, source, stamp])))
    for label, key in PRODUCTS.items():
        energy, cost, average, extreme = found[label]
        if energy < 0 or not -10000 < average < 10000 or not -20000 < extreme < 20000:
            raise ValueError('Monthly physical/range check failed')
        # Monthly reports round energy to whole MWh. Permit the resulting cost/price discrepancy.
        if energy > 0 and abs(cost - energy * average) > abs(average) + energy * .02 + 2:
            raise ValueError('Energy/cost/average consistency check failed')
        add(key + '_AVG', average / PEG, 'EUR/MWh')
        add(key + '_EXTREME', extreme / PEG, 'EUR/MWh')
    for label, key in [('Shortage', 'IMBALANCE_SHORTAGE'), ('Surplus', 'IMBALANCE_SURPLUS')]:
        add(key + '_AVG', found[label][2] / PEG, 'EUR/MWh')
    return rows


def parse_capacity(content, year, source, stamp):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        book = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    sheets = [s for s in book if 'capacity' in s.title.lower() and 'reg' in s.title.lower()]
    if len(sheets) != 1:
        raise ValueError('Annual capacity sheet not uniquely identified')
    table = list(sheets[0].values)
    if not any(f'year {year}' in str(row[0]).lower() for row in table[:4]):
        raise ValueError('Annual table year mismatch')
    # Both product names AND day/night/direction headings must retain their order.
    if list(table[4][2:7]) != ['FCR', 'aFRR', 'aFRR', 'mFRR', 'mFRR']:
        raise ValueError('Capacity product headings changed')
    headings = [str(x).strip().lower() for x in table[5][2:7]]
    if not ('off-peak' in headings[1] and 'peak' in headings[2] and 'upward' in headings[3] and 'downward' in headings[4]):
        raise ValueError('Capacity direction/time headings changed')
    wanted = {('required capacity', 'MW'): ('REQUIRED', 'MW'),
              ('delivered capacity', 'MW'): ('DELIVERED', 'MW'),
              ('price of contracted capacity', 'KM/MW/h'): ('PRICE', 'EUR/MW/h')}
    result = {}
    for row in table:
        spec = wanted.get((str(row[0]).strip().lower(), str(row[1]).strip()))
        if not spec:
            continue
        suffix, unit = spec
        for key, value in zip(CAPACITY, row[2:7]):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                raise ValueError('Missing/invalid annual capacity cell')
            k = key + '_' + suffix
            if k in result:
                raise ValueError('Duplicate annual metric')
            result[k] = dict(zip(COLUMNS, [k, year, round(value / PEG if suffix == 'PRICE' else value, 8), unit, source, stamp]))
    if len(result) != 15:
        raise ValueError('Incomplete annual capacity block')
    for key in CAPACITY:
        if result[key+'_DELIVERED']['Value'] > result[key+'_REQUIRED']['Value'] + .01:
            raise ValueError('Delivered capacity exceeds requirement')
    return list(result.values())


def update_balancing(path='balancing_prices.csv', session=None, now=None):
    if session is None:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session(impersonate='chrome124')
    now = now or dt.datetime.now(dt.timezone.utc)
    stamp = now.isoformat(timespec='seconds')
    try:
        def get(url):
            r = session.get(url, timeout=(10, 40))
            r.raise_for_status()
            if urlparse(r.url).hostname != 'www.nosbih.ba':
                raise ValueError('Unexpected source redirect')
            return r
        month, monthly_url, year, annual_url = links(get(INDEX).text, now.date())
        raw = get(monthly_url).content
        if not raw.startswith(b'%PDF'):
            raise ValueError('Monthly content is not PDF')
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
        rows = parse_monthly(text, month, monthly_url, stamp)
        rows += parse_capacity(get(annual_url).content, year, annual_url, stamp)
        # No stale period rollback, including an index unexpectedly losing newer links.
        if Path(path).exists():
            saved = list(csv.DictReader(Path(path).open(encoding='utf-8')))
            old_month = max((r['Period'] for r in saved if len(r['Period']) == 7), default='')
            old_year = max((r['Period'] for r in saved if len(r['Period']) == 4), default='')
            if month < old_month or year < old_year:
                raise ValueError('Report period would roll backwards')
        out = io.StringIO(newline='')
        writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
        atomic_text(path, out.getvalue())
        record('balancing', 'OK', detail='Latest linked published reports, not real-time tariffs',
               monthly_period=month, capacity_year=year, source=monthly_url, capacity_source=annual_url,
               publication_index=INDEX, publication_date=None)
        return rows
    except Exception as exc:
        record('balancing', 'FAILED', detail='Saved dated data retained; ' + type(exc).__name__ + ': ' + str(exc), source=INDEX)
        return None
