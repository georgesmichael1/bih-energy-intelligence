"""Extra NOS BiH datasets for the Bosnia dashboard (public pages and files, no token).

1. nos_imbalance_hourly.csv   last 60 days, hourly sums/means from nos_imbalance_15min.csv (EUR/MWh)
2. nos_reserve_providers.csv  who delivered reserve capacity (annual Excel tables 2018 on), MW
3. nos_balance_register.csv   register of balanced users, one block per published snapshot
4. nos_generation_plan.csv    planned new generation by year (indicative generation plan PDF)
   nos_generation_installed.csv installed capacity by source per year (same PDF)
KM prices are converted at 1.95583 (only the 15-minute file carries prices here).
Missing values stay empty, never zero. A failed dataset keeps its saved file and turns the run red.
No ENTSO-E involved.
"""
import csv
import datetime as dt
import io
import re
from pathlib import Path

BASE = 'https://www.nosbih.ba'
BAL_INDEX = BASE + '/en/trziste/balansno-trziste/izvjestaji-o-balansnom-trzistu/'
PLANS_PAGE = BASE + '/en/dokumenti/planovi/'
REGISTER_PAGE = BASE + '/en/dokumenti/registri/bilansirani-korisnici/'

HOURLY_PATH = 'nos_imbalance_hourly.csv'
HOURLY_COLUMNS = ['Date', 'Hour', 'aFRR_Up_MWh', 'aFRR_Down_MWh', 'mFRR_Up_MWh', 'mFRR_Down_MWh',
                  'Shortage_Mean_EUR_MWh', 'Surplus_Mean_EUR_MWh', 'Periods']
HOURLY_DAYS = 60

PROVIDERS_PATH = 'nos_reserve_providers.csv'
PROVIDER_COLUMNS = ['Year', 'Product', 'Provider', 'Delivered_MW_avg', 'Share_pct', 'Source']
FIRST_PROVIDER_YEAR = 2018

REGISTER_PATH = 'nos_balance_register.csv'
REGISTER_COLUMNS = ['Snapshot_Date', 'Reg_No', 'Owner', 'Owner_Group', 'Plant', 'Type', 'MW', 'MWh',
                    'Application_Date', 'Note', 'Source']

PLAN_PATH = 'nos_generation_plan.csv'
PLAN_COLUMNS = ['Plan_Period', 'Plant', 'Type', 'Year', 'MW_New', 'Source']
INSTALLED_PATH = 'nos_generation_installed.csv'
INSTALLED_COLUMNS = ['Plan_Period', 'Year', 'Thermal_MW', 'Pumped_Hydro_MW', 'Renewables_MW', 'Total_MW', 'Source']


def save_csv(path, columns, rows):
    from pipeline_status import atomic_text
    out = io.StringIO(newline='')
    w = csv.DictWriter(out, fieldnames=columns, lineterminator='\n', extrasaction='ignore')
    w.writeheader()
    for r in rows:
        w.writerow({k: ('' if r.get(k) is None else r.get(k)) for k in columns})
    atomic_text(path, out.getvalue())


def fnum(v):
    """Number from a cell: Excel float, or text with decimal comma. None when blank or not a number."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        s = str(v).strip().replace('\u00a0', '').replace(' ', '')
        if not re.fullmatch(r'-?\d+(?:[.,]\d+)?', s):
            return None
        x = float(s.replace(',', '.'))
    if x != x or abs(x) == float('inf'):
        raise ValueError('Nonfinite value')
    return x


# ---------------------------------------------------------------- 1. hourly imbalance
def update_hourly(src='nos_imbalance_15min.csv', out=HOURLY_PATH, days=HOURLY_DAYS):
    from pipeline_status import record
    try:
        byday = {}
        for r in csv.DictReader(open(src, encoding='utf-8')):
            byday.setdefault(r['Date'], []).append(r)
        if not byday:
            raise ValueError('No 15-minute data')
        rows = []
        for d in sorted(byday)[-days:]:
            hours = {}
            for r in byday[d]:
                m = re.match(r'^(\d{1,2}):\d{2}$', r['Start'])
                if not m:
                    raise ValueError('Unexpected start time')
                hours.setdefault(int(m[1]), []).append(r)
            for h in sorted(hours):
                rs = hours[h]

                def total(k):
                    v = [float(r[k]) for r in rs if r[k] not in ('', None)]
                    return round(sum(v), 3) if v else None

                def mean(k):
                    v = [float(r[k]) for r in rs if r[k] not in ('', None)]
                    return round(sum(v) / len(v), 2) if v else None
                rows.append({'Date': d, 'Hour': h,
                             'aFRR_Up_MWh': total('aFRR_Up_MWh'), 'aFRR_Down_MWh': total('aFRR_Down_MWh'),
                             'mFRR_Up_MWh': total('mFRR_Up_MWh'), 'mFRR_Down_MWh': total('mFRR_Down_MWh'),
                             'Shortage_Mean_EUR_MWh': mean('Price_Negative_EUR_MWh'),
                             'Surplus_Mean_EUR_MWh': mean('Price_Positive_EUR_MWh'),
                             'Periods': len(rs)})
        save_csv(out, HOURLY_COLUMNS, rows)
        record('nos_imbalance_hourly', 'OK',
               detail='Hourly activated aFRR/mFRR energy (sum) and imbalance prices (mean of the 15-minute prices), EUR/MWh, last 60 days. FCR is not published per hour.',
               first_date=sorted(byday)[-days:][0], latest_date=max(byday), rows=len(rows))
        return rows
    except Exception as exc:
        record('nos_imbalance_hourly', 'FAILED', detail='Saved data retained; ' + type(exc).__name__ + ': ' + str(exc))
        return None


# ---------------------------------------------------------------- 2. reserve providers
def provider_name(raw):
    s = re.sub(r'\s+', ' ', str(raw)).strip()
    low = s.lower()
    if 'stanari' in low or low.startswith('eft'):
        return 'EFT Stanari'
    for key, name in (('ep bih', 'EP BiH'), ('ers', 'ERS'), ('hzhb', 'EP HZHB'), ('eal', 'EAL')):
        if low == key or low.startswith(key + ' '):
            return name
    return s


def product_of(title):
    t = re.sub(r'[^a-z]', '', title.lower())
    if t.startswith(('reg', 'bal', 'xb', 'loss', 'dev', 'saldo', 'sheet', 'gubici')):
        return None
    if 'fcr' in t:
        return 'FCR'
    if 'off' in t or 'nevrsno' in t:
        return 'aFRR_Night'
    if 'peak' in t or 'vrsno' in t:
        return 'aFRR_Day'
    if 'down' in t or 'nadole' in t:
        return 'mFRR_Down'
    if 'up' in t or 'nagore' in t:
        return 'mFRR_Up'
    return None


def parse_providers(content, year, source):
    import openpyxl
    if not content.startswith(b'PK'):
        raise ValueError('Annual file is not Excel')
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    found = {}
    for ws in wb.worksheets:
        product = product_of(ws.title)
        if not product:
            continue
        rows = [[c.value for c in r] for r in ws.iter_rows()]
        start = None
        for i, r in enumerate(rows):
            if r and r[0] and 'share' in str(r[0]).lower() and 'delivered' in str(r[0]).lower():
                start = i
                break
        if start is None:
            continue
        for r in rows[start + 1:]:
            if not r or not r[0]:
                if found.get(product):
                    break
                continue
            if len(r) > 14 and str(r[1]).strip() == 'MW':
                found.setdefault(product, {})[provider_name(r[0])] = fnum(r[14])
            elif str(r[1]).strip() == '%':
                continue
            elif found.get(product):
                break
    out = []
    for product, provs in found.items():
        total = sum(v for v in provs.values() if v is not None)
        for name, mw in provs.items():
            share = round(100 * mw / total, 1) if mw is not None and total > 0 else None
            out.append({'Year': year, 'Product': product, 'Provider': name,
                        'Delivered_MW_avg': None if mw is None else round(mw, 3),
                        'Share_pct': share, 'Source': source})
    return out


def update_providers(get, path=PROVIDERS_PATH, today=None):
    from pipeline_status import record
    from bs4 import BeautifulSoup
    today = today or dt.datetime.now(dt.timezone.utc).date()
    try:
        links = {}
        for a in BeautifulSoup(get(BAL_INDEX).text, 'html.parser').find_all('a', href=True):
            m = re.search(r'/godine/(\d{4})-en-balansno-trziste-tabele\.xlsx$', a['href'])
            if m:
                h = a['href']
                links[int(m[1])] = BASE + h if h.startswith('/') else h
        rows, years = [], []
        for y in sorted(links):
            if y < FIRST_PROVIDER_YEAR or y >= today.year:
                continue
            got = parse_providers(get(links[y]).content, y, links[y])
            if not got:
                raise ValueError(f'{y}: no provider tables found')
            rows += got
            years.append(y)
        if not rows or not years:
            raise ValueError('No provider data')
        if Path(path).exists():
            old = {int(r['Year']) for r in csv.DictReader(open(path, encoding='utf-8'))}
            if old and max(old) > max(years):
                raise ValueError('Latest year would roll backwards')
        save_csv(path, PROVIDER_COLUMNS, rows)
        record('nos_reserve_providers', 'OK',
               detail='Average delivered reserve capacity (MW) per provider, annual NOS BiH tables; one value per year',
               first_year=min(years), latest_year=max(years), rows=len(rows), source=BAL_INDEX)
        return rows
    except Exception as exc:
        record('nos_reserve_providers', 'FAILED', detail='Saved data retained; ' + type(exc).__name__ + ': ' + str(exc), source=BAL_INDEX)
        return None


# ---------------------------------------------------------------- 3. register of balanced users
NUM = r'\d+(?:,\d+)?'
TERM = rf'(?:{NUM}\s*[x×]\s*)?{NUM}'
EXPR = re.compile(rf'(?:^|[\s,])({TERM}(?:\s*\+\s*{TERM})*)\s*$')
MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
          'september', 'october', 'november', 'december']


def power_mw(text):
    """MW from a plant text. None when the text is not a single clear MW figure."""
    t = re.sub(r'\([^)]*\)', ' ', text)
    t = re.sub(r'(\d)MW\b', r'\1 MW', t)
    if len(re.findall(r'\bMW\b', t)) != 1:
        return None
    head = t[:re.search(r'\bMW\b', t).start()].rstrip()
    m = EXPR.search(head)
    if not m:
        return None
    total = 0.0
    for term in m[1].split('+'):
        parts = [float(p.replace(',', '.')) for p in re.split(r'[x×]', term)]
        total += parts[0] * parts[1] if len(parts) == 2 else parts[0]
    return round(total, 4)


def plant_type(plant, note):
    p = plant.strip()
    up = p.upper()
    if 'consumer' in (note or '').lower():
        return 'Consumer'
    if up.startswith('BESS'):
        return 'Storage (BESS)'
    if up.startswith('PHPP'):
        return 'Pumped hydro'
    if up.startswith('HPP') or up.startswith('HE '):
        return 'Hydro'
    if up.startswith('TPP') or up.startswith('HP '):
        return 'Thermal'
    if up.startswith('SPP AND WPP') or up.startswith('WPP AND SPP'):
        return 'Solar + wind'
    if up.startswith('WPP'):
        return 'Wind'
    if up.startswith('SPP'):
        return 'Solar'
    return 'Other'


def owner_group(owner):
    low = owner.lower()
    for key, name in (('republike srpske', 'ERS'), ('hrvatske zajednice', 'EP HZHB'), ('ep hzhb', 'EP HZHB'),
                      ('bosne i hercegovine', 'EP BiH')):
        if key in low and 'elektroprivreda' in low or (key == 'ep hzhb' and key in low):
            return name
    return owner


def parse_register(html, source):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text(' ')
    m = re.search(r'updated\s+(\d{1,2})\s*(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})', text)
    if not m or m[2].lower() not in MONTHS:
        raise ValueError('Register update date not found')
    snap = dt.date(int(m[3]), MONTHS.index(m[2].lower()) + 1, int(m[1])).isoformat()
    table = None
    for t in soup.find_all('table'):
        if 'Power plant name' in t.get_text(' '):
            table = t
            break
    if table is None:
        raise ValueError('Register table not found')
    out = []
    for tr in table.find_all('tr'):
        c = [re.sub(r'\s+', ' ', x.get_text(' ', strip=True)).strip() for x in tr.find_all(['td', 'th'])]
        if len(c) < 9 or not re.fullmatch(r'\d{1,3}', c[0]):
            continue
        owner = re.split(r',', c[2])[0].strip()
        plant, note = c[3].strip(), c[8].strip()
        ptype = plant_type(plant, note)
        mw = None if ptype == 'Consumer' else power_mw(plant)
        mwh = None
        mm = re.search(rf'MW,\s*({NUM})\s*MWh', plant)
        if mm:
            mwh = float(mm[1].replace(',', '.'))
        appl = c[1] if re.fullmatch(r'\d{2}\.\d{2}\.\d{4}\.?', c[1]) else ''
        out.append({'Snapshot_Date': snap, 'Reg_No': c[0], 'Owner': owner, 'Owner_Group': owner_group(owner),
                    'Plant': plant, 'Type': ptype, 'MW': mw, 'MWh': mwh,
                    'Application_Date': appl.rstrip('.'), 'Note': note[:140] if note != '-' else '',
                    'Source': source})
    if len(out) < 50:
        raise ValueError('Register has too few rows')
    return snap, out


def update_register(get, path=REGISTER_PATH):
    from pipeline_status import record
    try:
        snap, rows = parse_register(get(REGISTER_PAGE).text, REGISTER_PAGE)
        kept = []
        if Path(path).exists():
            kept = [r for r in csv.DictReader(open(path, encoding='utf-8')) if r['Snapshot_Date'] != snap]
            if kept and max(r['Snapshot_Date'] for r in kept) > snap:
                raise ValueError('Register snapshot would roll backwards')
        allrows = kept + rows
        save_csv(path, REGISTER_COLUMNS, allrows)
        snaps = sorted({r['Snapshot_Date'] for r in allrows})
        record('nos_balance_register', 'OK',
               detail='Register of balanced users (plants, owners, MW, planned BESS) as published by NOS BiH; one block per published update, history builds from first fetch',
               latest_snapshot=snaps[-1], snapshots=len(snaps), rows_in_latest=len(rows),
               mw_unparsed=sum(1 for r in rows if r['MW'] is None and r['Type'] != 'Consumer'), source=REGISTER_PAGE)
        return rows
    except Exception as exc:
        record('nos_balance_register', 'FAILED', detail='Saved data retained; ' + type(exc).__name__ + ': ' + str(exc), source=REGISTER_PAGE)
        return None


# ---------------------------------------------------------------- 4. generation plan
def plan_type(name):
    u = name.upper()
    if u.startswith('HE '):
        return 'Hydro'
    if u.startswith('VE '):
        return 'Wind'
    if u.startswith(('SE ', 'FNE ')):
        return 'Solar'
    if u.startswith(('TE ', 'TOPLANA')):
        return 'Thermal'
    return 'Other'


def latest_plan_link(html):
    from bs4 import BeautifulSoup
    best = None
    for a in BeautifulSoup(html, 'html.parser').find_all('a', href=True):
        m = re.search(r'/files/\d{4}/\d{2}/\d{8}-lat-Indikativni-plan-razvoja-proizvodnje-(\d{4})-(\d{4})\.pdf$', a['href'])
        if m:
            key = (int(m[1]), a['href'])
            h = a['href']
            if best is None or key > best[0]:
                best = (key, f'{m[1]}-{m[2]}', BASE + h if h.startswith('/') else h)
    if not best:
        raise ValueError('No generation plan link')
    return best[1], best[2]


def parse_plan(content, period, source):
    import pdfplumber
    if not content.startswith(b'%PDF'):
        raise ValueError('Plan is not PDF')
    first = int(period[:4])
    plants, total_row, installed = [], None, []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            for t in page.find_tables():
                data = t.extract()
                if not data or not data[0]:
                    continue
                head = ' '.join(str(x) for x in data[0] if x)
                if 'Novi kapaciteti' in head:
                    years = [int(x) for x in data[0] if x and re.fullmatch(r'20\d\d', str(x).strip())]
                    if years[0] != first - 1 and years[0] != first:
                        raise ValueError('Plan year header changed')
                    for r in data[1:]:
                        name = re.sub(r'\s+', ' ', str(r[1] or '')).strip()
                        if not name:
                            continue
                        vals = [fnum(r[3 + 3 * k]) if len(r) > 3 + 3 * k else None for k in range(len(years))]
                        if name.lower().startswith('novi bilansirano'):
                            total_row = dict(zip(years, vals))
                        elif name.lower().startswith(('kumulativno', 'postoje', 'ukupno')):
                            continue
                        elif any(v is not None for v in vals):
                            plants.append((name, dict(zip(years, vals))))
                elif 'PHE' in ' '.join(str(c[0]) for c in data if c and c[0]) and 'OBNOVLJIVI IZVORI (%)' in ' '.join(str(c[0] or '') for c in data):
                    ys = [int(x) for x in data[0] if x and re.fullmatch(r'20\d\d', str(x).strip())]
                    lab = {}
                    for r in data:
                        k = re.sub(r'\s+', ' ', str(r[0] or '')).strip().upper()
                        nums = [fnum(x) for x in r[1:] if fnum(x) is not None]
                        if k in ('TE', 'PHE ČAPLJINA', 'OBNOVLJIVI IZVORI', 'UKUPNO') and len(nums) >= len(ys):
                            lab.setdefault(k, nums[:len(ys)])
                    if ys and len(lab) == 4:
                        for i, y in enumerate(ys):
                            installed.append({'Plan_Period': period, 'Year': y, 'Thermal_MW': lab['TE'][i],
                                              'Pumped_Hydro_MW': lab['PHE ČAPLJINA'][i],
                                              'Renewables_MW': lab['OBNOVLJIVI IZVORI'][i],
                                              'Total_MW': lab['UKUPNO'][i], 'Source': source})
    if not plants or not total_row:
        raise ValueError('Plan table 8.8 not found')
    # Check: plant rows must add up to the plan's own "new balanced" row for every year.
    for y, tot in total_row.items():
        if tot is None:
            continue
        s = sum(v.get(y) or 0 for _, v in plants)
        if abs(s - tot) > 0.2:
            raise ValueError(f'Plan check failed for {y}: plants {s:.1f} vs total {tot:.1f}')
    rows = []
    for name, v in plants:
        for y, mw in v.items():
            if mw is not None:
                rows.append({'Plan_Period': period, 'Plant': name, 'Type': plan_type(name), 'Year': y,
                             'MW_New': mw, 'Source': source})
    return rows, installed


def update_plan(get, path=PLAN_PATH, installed_path=INSTALLED_PATH):
    from pipeline_status import record
    try:
        period, url = latest_plan_link(get(PLANS_PAGE).text)
        rows, installed = parse_plan(get(url).content, period, url)
        if Path(path).exists():
            old = {r['Plan_Period'] for r in csv.DictReader(open(path, encoding='utf-8'))}
            if old and max(old) > period:
                raise ValueError('Plan period would roll backwards')
        save_csv(path, PLAN_COLUMNS, rows)
        if installed:
            save_csv(installed_path, INSTALLED_COLUMNS, installed)
        record('nos_generation_plan', 'OK',
               detail='Indicative generation development plan: new balanced capacity per plant and year (MW). Plant rows add up to the plan total for every year. No storage is listed in this plan table.',
               plan_period=period, plants=len({r['Plant'] for r in rows}), rows=len(rows),
               installed_rows=len(installed), source=url)
        return rows
    except Exception as exc:
        record('nos_generation_plan', 'FAILED', detail='Saved data retained; ' + type(exc).__name__ + ': ' + str(exc), source=PLANS_PAGE)
        return None


NAMES = ('nos_imbalance_hourly', 'nos_reserve_providers', 'nos_balance_register', 'nos_generation_plan')


def main():
    import json
    import requests
    from pipeline_status import DATASETS, write_status
    try:  # same pattern as balancing_feed.py: NOS BiH pages answer a browser-like client
        from curl_cffi import requests as cffi_requests
        s = cffi_requests.Session(impersonate='chrome124')
        retry_errors = (Exception,)
    except ImportError:
        s = requests.Session()
        s.headers['User-Agent'] = 'Mozilla/5.0 (compatible; bih-energy-intelligence)'
        retry_errors = (requests.RequestException,)

    def get(url):
        last = None
        for _ in range(3):
            try:
                r = s.get(url, timeout=(10, 90))
                r.raise_for_status()
                return r
            except retry_errors as exc:
                last = exc
        raise last
    path = Path('status.json')
    old = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    DATASETS.clear()
    DATASETS.update(old.get('dataset_details', {}))
    update_hourly()
    update_providers(get)
    update_register(get)
    update_plan(get)
    reserved = {'schema_version', 'run_finished_at', 'last_verified_date',
                'last_verified_date_is_legacy', 'verification_note', 'rows',
                'datasets', 'dataset_details'}
    legacy = {k: v for k, v in old.items() if k not in reserved}
    write_status(path, rows=old.get('rows'), legacy=legacy)
    bad = [k for k in NAMES if DATASETS.get(k, {}).get('state') == 'FAILED']
    if bad:
        raise SystemExit('Failed: ' + ', '.join(bad))


if __name__ == '__main__':
    main()
