"""Bounded Bosnia ENTSO-E feeds (load forecast, physical flows, HR day-ahead schedules,
day-ahead NTC, daily total nominated capacity and total commercial schedules including
intraday). No actual-load or generation publishing here.

All timestamps in the CSV primary key are UTC; local ISO timestamps include the
UTC offset, including repeated fall-back hours. Positive directional quantities
mean From_Area -> To_Area, not a signed net flow. MAW is the ENTSO-E code for MW.
"""
import csv
import datetime as dt
import io
import math
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import threading
import time
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pipeline_status import atomic_text, record, utc_now

ENDPOINT = 'https://web-api.tp.entsoe.eu/api'
TZ = ZoneInfo('Europe/Sarajevo')
UTC = dt.timezone.utc
AREAS = {'BA': '10YBA-JPCC-----D', 'HR': '10YHR-HEP------M',
         'RS': '10YCS-SERBIATSOV', 'ME': '10YCS-CG-TSO---S'}
COLUMNS = ['Dataset', 'From_Area', 'To_Area', 'Interval_Start_UTC',
           'Interval_End_UTC', 'Interval_Start_Local', 'Resolution', 'Value_MW',
           'Source_Document', 'Revision', 'Fetched_At_UTC']

class DataError(ValueError):
    pass

class APIError(RuntimeError):
    pass

class StopAPI(APIError):
    """Stop this run on authentication or rate limiting, without leaking headers."""


def instant(text):
    value = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise DataError('Timezone missing')
    return value.astimezone(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def day_bounds(day):
    start = dt.datetime.combine(day, dt.time(), TZ).astimezone(UTC)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), TZ).astimezone(UTC)
    return start, end


def text(node, path):
    value = node.findtext(path)
    if value is None:
        raise DataError('Missing required XML field: ' + path)
    return value.strip()

def _safe_text(value):
    # Keep only plain letters, digits and simple punctuation, and cut it short.
    # This stops URLs, XML and anything else from reaching the log.
    return re.sub(r"[^A-Za-z0-9 .,:()\-]", '', value).strip()[:150]

def parse_document(body, dataset, source, target, start, end, fetched_at):
    if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
        raise DataError('XML declarations not supported')
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise DataError('Invalid XML') from exc
    if root.tag.split('}')[-1] == 'Acknowledgement_MarketDocument':
        codes = [n.text or '' for n in root.findall('.//{*}Reason/{*}code')]
        reasons = [n.text or '' for n in root.findall('.//{*}Reason/{*}text')]
        raise DataError('API acknowledgement, code ' + ','.join(codes) +
                        ': ' + _safe_text(' '.join(reasons)))
    expected_type = {'load_forecast': 'A65', 'physical_flows': 'A11',
                     'hr_schedules': 'A09', 'ntc_day_ahead': 'A61',
                     'nominated_daily': 'A26', 'scheduled_rs_me': 'A09',
                     'scheduled_total': 'A09'}[dataset]
    if text(root, '{*}type') != expected_type:
        raise DataError('Unexpected document type')
    if dataset == 'load_forecast' and text(root, '{*}process.processType') != 'A01':
        raise DataError('Unexpected forecast process')
    document = text(root, '{*}mRID')
    revision = int(text(root, '{*}revisionNumber'))
    series = root.findall('{*}TimeSeries')
    if dataset == 'nominated_daily':
        # The response can carry several series for one border direction (other
        # business types or contract types). Keep only the total nominated
        # capacity (B08) series; day-ahead/daily contract when it is stated.
        series = [t for t in series if t.findtext('{*}businessType') in (None, 'B08') and
                  t.findtext('{*}contract_MarketAgreement.type') in (None, 'A01')]
    # Do not silently add duplicate/alternative series or choose a revision.
    if len(series) != 1:
        raise DataError('Expected exactly one aggregate directional series, found ' + str(len(series)) +
                        ' (business types ' + _safe_text(','.join(sorted({t.findtext('{*}businessType') or '-' for t in root.findall('{*}TimeSeries')}))) +
                        '; contract types ' + _safe_text(','.join(sorted({t.findtext('{*}contract_MarketAgreement.type') or '-' for t in root.findall('{*}TimeSeries')}))) + ')')
    ts = series[0]
    if text(ts, '{*}quantity_Measure_Unit.name') != 'MAW':
        raise DataError('Unexpected unit, MW required')
    if dataset == 'load_forecast':
        if text(ts, '{*}outBiddingZone_Domain.mRID') != AREAS['BA']:
            raise DataError('Unexpected forecast area')
    else:
        if (text(ts, '{*}out_Domain.mRID') != AREAS[source] or
                text(ts, '{*}in_Domain.mRID') != AREAS[target]):
            raise DataError('Direction mismatch')
        if dataset in ('hr_schedules', 'scheduled_rs_me') and \
                text(ts, '{*}contract_MarketAgreement.type') != 'A01':
            raise DataError('Not a day-ahead/daily contract series')
        # Total commercial schedules (12.1.F "Total"): all time horizons including
        # intraday. ENTSO-E serves them as documentType A09 with contract type A05.
        if dataset == 'scheduled_total' and \
                text(ts, '{*}contract_MarketAgreement.type') != 'A05':
            raise DataError('Not a total (all horizons) contract series')
        # NTC and nominated responses may omit the contract element; when it is
        # present it must still be the day-ahead/daily contract.
        if dataset in ('ntc_day_ahead', 'nominated_daily') and \
                ts.findtext('{*}contract_MarketAgreement.type') not in (None, 'A01'):
            raise DataError('Not a day-ahead/daily contract series')
        if dataset == 'nominated_daily' and ts.findtext('{*}businessType') not in (None, 'B08'):
            raise DataError('Not total nominated capacity')
    curve = text(ts, '{*}curveType')
    if curve not in ('A01', 'A03'):
        raise DataError('Unsupported curve type')
    result = {}
    for period in ts.findall('{*}Period'):
        pstart = instant(text(period, '{*}timeInterval/{*}start'))
        pend = instant(text(period, '{*}timeInterval/{*}end'))
        resolution = text(period, '{*}resolution')
        match = re.fullmatch(r'PT(\d+)M', resolution)
        if not match or int(match[1]) not in (15, 30, 60):
            raise DataError('Unsupported interval resolution')
        step = dt.timedelta(minutes=int(match[1]))
        count, remainder = divmod(pend - pstart, step)
        if remainder or count <= 0 or count > 10000:
            raise DataError('Invalid Period bounds')
        points = {}
        for point in period.findall('{*}Point'):
            position = int(text(point, '{*}position'))
            value = float(text(point, '{*}quantity'))
            if position in points or not 1 <= position <= count or not math.isfinite(value) or value < 0:
                raise DataError('Duplicate/invalid position or quantity')
            points[position] = value
        if not points or (curve == 'A03' and 1 not in points):
            raise DataError('Missing first block')
        current = None
        for position in range(1, count + 1):
            if position in points:
                current = points[position]
            elif curve == 'A01':
                current = None
            left = pstart + step * (position - 1)
            right = left + step
            if left < start or right > end:
                raise DataError('Period outside requested window')
            if left in result:
                raise DataError('Overlapping Periods')
            if current is None:
                raise DataError('Missing sequential interval')
            result[left] = {
                'Dataset': dataset, 'From_Area': source, 'To_Area': target,
                'Interval_Start_UTC': iso(left), 'Interval_End_UTC': iso(right),
                'Interval_Start_Local': left.astimezone(TZ).isoformat(),
                'Resolution': resolution, 'Value_MW': current,
                'Source_Document': document, 'Revision': revision,
                'Fetched_At_UTC': fetched_at,
            }
    rows = [result[key] for key in sorted(result)]
    if (not rows or instant(rows[0]['Interval_Start_UTC']) != start or
            instant(rows[-1]['Interval_End_UTC']) != end):
        raise DataError('Incomplete requested window')
    for previous, following in zip(rows, rows[1:]):
        if previous['Interval_End_UTC'] != following['Interval_Start_UTC']:
            raise DataError('Gap between Periods')
    return rows


def make_session():
    session = requests.Session()
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504, 599],
        allowed_methods=['GET'], raise_on_status=False)))
    return session


def fetch(session, token, parameters):
    # ENTSO-E's Transparency API is a GET request. The token travels only as the
    # securityToken query parameter and is never copied into an error message.
    query = dict(parameters)
    query['securityToken'] = token
    try:
        response = session.get(ENDPOINT, params=query, timeout=(10, 45), allow_redirects=False)
    except requests.RequestException:
        # Discard the exception text: it can contain the full URL, including the token.
        raise APIError('Network request failed') from None
    if response.status_code in (401, 403, 429):
        raise StopAPI('Authentication/rate-limit HTTP ' + str(response.status_code))
    if response.status_code != 200:
        raise APIError('API HTTP ' + str(response.status_code))
    return response.content


def persist(path, rows, cutoff):
    # Replace only after EVERY direction in the dataset validates. Never erase
    # previously valid rows because a new request failed or had no data.
    path = Path(path)
    old = []
    if path.exists():
        with path.open(newline='', encoding='utf-8') as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != COLUMNS:
                raise DataError('Existing CSV schema mismatch')
            old = list(reader)
    def key(row):
        return tuple(row[k] for k in ('Dataset', 'From_Area', 'To_Area', 'Interval_Start_UTC'))
    merged = {key(row): row for row in old if instant(row['Interval_Start_UTC']) >= cutoff}
    for row in rows:
        prior = merged.get(key(row))
        if prior and prior['Source_Document'] == row['Source_Document']:
            if int(prior['Revision']) > int(row['Revision']):
                raise DataError('Revision regression, stored CSV retained')
        # Different document IDs cannot be revision-ordered; this requires
        # reconciliation before enabling multi-day backfills.
        merged[key(row)] = row
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator='\n')
    writer.writeheader()
    writer.writerows(merged[k] for k in sorted(merged))
    atomic_text(path, out.getvalue())


BACKFILL_MAX_PER_RUN = 10      # older incomplete days recovered per dataset per run, newest first
BACKFILL_TIME_BUDGET_S = 600   # stop recovering older days after this many seconds (per dataset)


def _saved_complete_days(path, directions, days):
    """Local days whose saved rows fully cover day_bounds for every direction. Gaps are never zero."""
    path = Path(path)
    if not path.exists():
        return set()
    with path.open(newline='', encoding='utf-8') as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != COLUMNS:
            return set()
        saved = list(reader)
    done = set()
    for day in days:
        start, end = day_bounds(day)
        ok = True
        for source, target in directions:
            rows = sorted((r for r in saved if r['From_Area'] == source and r['To_Area'] == target
                           and start <= instant(r['Interval_Start_UTC']) < end),
                          key=lambda r: r['Interval_Start_UTC'])
            if (not rows or instant(rows[0]['Interval_Start_UTC']) != start or
                    instant(rows[-1]['Interval_End_UTC']) != end or
                    any(a['Interval_End_UTC'] != b['Interval_Start_UTC'] for a, b in zip(rows, rows[1:]))):
                ok = False
                break
        if ok:
            done.add(day)
    return done


MAX_WORKERS = 4
PACE_MIN_INTERVAL_S = 0.35   # token-wide spacing when several workers run: at most ~170 requests/min (ENTSO-E allows 400)


class _Pacer:
    """Token-wide request spacing shared by all workers of one run."""
    def __init__(self, interval):
        self.interval = interval
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        if self.interval <= 0:
            return
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_at)
            self.next_at = start + self.interval
        if start > now:
            time.sleep(start - now)


def update_entsoe(output_dir='.', day=None, session=None, token=None, include_physical=True, include_capacity=False, workers=None):
    """Explicit day: fetch exactly that day. No day: refresh yesterday and also recover any
    incomplete older day in the 31-day local window (yesterday plus 29 earlier, today excluded).
    A day is saved only after every direction validates; failed days keep prior saved rows."""
    auto = day is None
    day = day or (dt.datetime.now(TZ).date() - dt.timedelta(days=1))
    start, end = day_bounds(day)
    groups = {
        'load_forecast': [('BA', 'BA')],
        'physical_flows': [(a, b) for other in ('HR', 'RS', 'ME')
                           for a, b in [('BA', other), (other, 'BA')]],
        'hr_schedules': [('BA', 'HR'), ('HR', 'BA')],
        'ntc_day_ahead': [(a, b) for other in ('HR', 'RS', 'ME')
                          for a, b in [('BA', other), (other, 'BA')]],
        'nominated_daily': [(a, b) for other in ('HR', 'RS', 'ME')
                            for a, b in [('BA', other), (other, 'BA')]],
        'scheduled_rs_me': [(a, b) for other in ('RS', 'ME')
                            for a, b in [('BA', other), (other, 'BA')]],
        'scheduled_total': [(a, b) for other in ('HR', 'RS', 'ME')
                            for a, b in [('BA', other), (other, 'BA')]],
    }
    if not include_physical:
        groups.pop('physical_flows')
    if not include_capacity:
        groups.pop('ntc_day_ahead'); groups.pop('nominated_daily'); groups.pop('scheduled_rs_me')
        groups.pop('scheduled_total')
    only = os.environ.get('ENTSOE_DATASETS', '').strip()   # optional: fetch only these datasets (parallel workflow legs)
    if only:
        keep = {x.strip() for x in only.split(',') if x.strip()}
        groups = {k: v for k, v in groups.items() if k in keep}
    token = token if token is not None else os.environ.get('ENTSOE_API_TOKEN', '').strip()
    if workers is None:
        workers = int(os.environ.get('ENTSOE_WORKERS', '1') or 1)
    workers = max(1, min(int(workers), MAX_WORKERS))   # 1 = original sequential behaviour
    owns_session = session is None
    session = session or make_session()
    stop = threading.Event()                    # set on 401/403/429: no worker sends another request
    pacer = _Pacer(PACE_MIN_INTERVAL_S if workers > 1 else 0.0)
    local = threading.local()
    extra_sessions = []
    def sess():
        if workers == 1 or not owns_session:
            return session
        if not hasattr(local, 's'):
            local.s = make_session()
            extra_sessions.append(local.s)
        return local.s
    def fetch_day(dataset, directions, target_day):
        d_start, d_end = day_bounds(target_day)
        rows = []
        for source, target in directions:
            parameters = {
                'documentType': {'load_forecast': 'A65', 'physical_flows': 'A11', 'hr_schedules': 'A09',
                                 'ntc_day_ahead': 'A61', 'nominated_daily': 'A26', 'scheduled_rs_me': 'A09',
                                 'scheduled_total': 'A09'}[dataset],
                'periodStart': d_start.strftime('%Y%m%d%H%M'),
                'periodEnd': d_end.strftime('%Y%m%d%H%M'),
            }
            if dataset == 'load_forecast':
                parameters.update({'processType': 'A01', 'outBiddingZone_Domain': AREAS['BA']})
            else:
                parameters.update({'in_Domain': AREAS[target], 'out_Domain': AREAS[source]})
                if dataset in ('hr_schedules', 'scheduled_rs_me', 'ntc_day_ahead', 'nominated_daily'):
                    parameters['contract_MarketAgreement.type'] = 'A01'
                if dataset == 'scheduled_total':
                    parameters['contract_MarketAgreement.type'] = 'A05'
                if dataset == 'nominated_daily':
                    parameters['businessType'] = 'B08'
            if stop.is_set():
                raise StopAPI('Stopped after authentication/rate limit')
            pacer.wait()
            try:
                body = fetch(sess(), token, parameters)
            except StopAPI:
                stop.set()
                raise
            rows.extend(parse_document(body, dataset, source, target, d_start, d_end, utc_now()))
            time.sleep(1)
        return rows
    def process(dataset, directions):
        name = 'entsoe_' + dataset
        meta = {'target_local_day': day.isoformat(), 'source': ENDPOINT,
                'coverage_start_utc': iso(start), 'coverage_end_utc': iso(end)}
        if not token or stop.is_set():
            record(name, 'FAILED', detail='Token missing' if not token else 'Stopped after authentication/rate limit', **meta)
            return
        path = Path(output_dir) / (name + '.csv')
        backfill = []
        if auto:
            window = [day - dt.timedelta(days=n) for n in range(1, 30)]
            have = _saved_complete_days(path, directions, window)
            backfill = [d for d in window if d not in have]  # newest first
            skipped = backfill[BACKFILL_MAX_PER_RUN:]
            backfill = backfill[:BACKFILL_MAX_PER_RUN]
        cutoff = day_bounds(day - dt.timedelta(days=29))[0] if auto else start - dt.timedelta(days=29)
        try:
            rows = fetch_day(dataset, directions, day)
            persist(path, rows, cutoff)
        except StopAPI as exc:
            stop.set()
            record(name, 'FAILED', detail=str(exc), **meta)
            return
        except (APIError, DataError) as exc:
            # These messages are fixed strings from this code, or ENTSO-E reason
            # codes. They never contain the token or raw response text.
            record(name, 'FAILED', detail='Request/validation failed: ' + str(exc).replace(token, '[redacted]'), **meta)
            return
        except (ValueError, OSError, KeyError, TypeError) as exc:
            # Generic errors may echo input text, so only the type name is recorded.
            record(name, 'FAILED', detail='Fetch/validation/write failed: ' + type(exc).__name__, **meta)
            return
        recovered, unavailable = [], []
        began = time.monotonic()
        for older in backfill:
            if stop.is_set() or time.monotonic() - began > BACKFILL_TIME_BUDGET_S:
                unavailable.append(older.isoformat())
                continue
            try:
                persist(path, fetch_day(dataset, directions, older), cutoff)
                recovered.append(older.isoformat())
            except StopAPI:
                stop.set()
                unavailable.append(older.isoformat())
            except (APIError, DataError, ValueError, OSError, KeyError, TypeError):
                unavailable.append(older.isoformat())
        if auto:
            unavailable += [d.isoformat() for d in skipped]  # not yet attempted, still incomplete
            meta.update(recovered_days=recovered, unavailable_days=unavailable)
        if unavailable:
            record(name, 'PARTIAL', detail='Yesterday saved; ' + str(len(unavailable)) +
                   ' earlier day(s) still unavailable or incomplete at the source. Saved rows kept, gaps are not zero',
                   row_count=len(rows), directions=len(directions), unit='MW', **meta)
        else:
            record(name, 'OK', detail='Complete requested historical day; not current-day data',
                   row_count=len(rows), directions=len(directions), unit='MW', **meta)

    try:
        if workers == 1:
            for dataset, directions in groups.items():
                process(dataset, directions)
        else:
            # Largest groups first so the pool finishes together. Each dataset writes only its own CSV.
            items = sorted(groups.items(), key=lambda kv: -len(kv[1]))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for _ in pool.map(lambda kv: process(*kv), items):
                    pass
    finally:
        for extra in extra_sessions:
            extra.close()
        if owns_session:
            session.close()

