"""Bosnia ENTSO-E actual total load (A65/A16) and actual generation per type (A75/A16).

Rolling 31-day window, hourly as published. Gaps are never zero-filled: a load day is saved
only when every hour is present; generation keeps only the points ENTSO-E published and the
status records which days are short. Reuse terms follow the ENTSO-E Terms of Use and the
primary data owner; this is not a blanket open licence.
"""
import csv, datetime as dt, io, math, os, re, time
import xml.etree.ElementTree as ET
from pathlib import Path
from entsoe_adapter import (AREAS, ENDPOINT, DataError, APIError, StopAPI, day_bounds, fetch,
                            instant, iso, make_session, text, TZ, UTC, _safe_text)
from pipeline_status import atomic_text, record, utc_now

PSR = {'B01': 'Biomass', 'B02': 'Fossil Brown coal/Lignite', 'B03': 'Fossil Coal-derived gas',
       'B04': 'Fossil Gas', 'B05': 'Fossil Hard coal', 'B06': 'Fossil Oil', 'B07': 'Fossil Oil shale',
       'B08': 'Fossil Peat', 'B09': 'Geothermal', 'B10': 'Hydro Pumped Storage',
       'B11': 'Hydro Run-of-river and poundage', 'B12': 'Hydro Water Reservoir', 'B13': 'Marine',
       'B14': 'Nuclear', 'B15': 'Other renewable', 'B16': 'Solar', 'B17': 'Waste',
       'B18': 'Wind Offshore', 'B19': 'Wind Onshore', 'B20': 'Other'}
LOAD_COLS = ['Dataset', 'Area', 'Interval_Start_UTC', 'Interval_End_UTC', 'Interval_Start_Local',
             'Resolution', 'Value_MW', 'Source_Document', 'Revision', 'Fetched_At_UTC']
GEN_COLS = ['Dataset', 'Area', 'PSR_Code', 'PSR_Type', 'Flow', 'Interval_Start_UTC', 'Interval_End_UTC',
            'Interval_Start_Local', 'Resolution', 'Value_MW', 'Source_Document', 'Revision', 'Fetched_At_UTC']
WINDOW_DAYS = 31


def _periods(ts, start, end, allow_negative=False):
    """Yield (left, right, resolution, value) for every published point; A01 curves only."""
    if text(ts, '{*}quantity_Measure_Unit.name') != 'MAW':
        raise DataError('Unexpected unit, MW required')
    curve = text(ts, '{*}curveType')
    if curve not in ('A01', 'A03'):
        raise DataError('Unsupported curve type')
    for period in ts.findall('{*}Period'):
        pstart = instant(text(period, '{*}timeInterval/{*}start'))
        pend = instant(text(period, '{*}timeInterval/{*}end'))
        resolution = text(period, '{*}resolution')
        m = re.fullmatch(r'PT(\d+)M', resolution)
        if not m or int(m[1]) not in (15, 30, 60):
            raise DataError('Unsupported interval resolution')
        step = dt.timedelta(minutes=int(m[1]))
        count, rem = divmod(pend - pstart, step)
        if rem or count <= 0 or count > 10000:
            raise DataError('Invalid Period bounds')
        points = {}
        for point in period.findall('{*}Point'):
            pos = int(text(point, '{*}position'))
            val = float(text(point, '{*}quantity'))
            if pos in points or not 1 <= pos <= count or not math.isfinite(val) or (val < 0 and not allow_negative):
                raise DataError('Duplicate/invalid position or quantity')
            points[pos] = val
        if curve == 'A03':
            # Variable-sized blocks: a value holds until the next stated position.
            if points and 1 not in points:
                raise DataError('Missing first block')
            current = None
            order = range(1, count + 1) if points else []
        else:
            order = sorted(points)
        for pos in order:
            if pos in points:
                current = points[pos]
            left = pstart + step * (pos - 1)
            if left < start or left + step > end:
                raise DataError('Point outside requested window')
            yield left, left + step, resolution, current


def _root(body, expected_type):
    up = body.upper()
    if b'<!DOCTYPE' in up or b'<!ENTITY' in up:
        raise DataError('XML declarations not supported')
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise DataError('Invalid XML') from exc
    if root.tag.split('}')[-1] == 'Acknowledgement_MarketDocument':
        codes = [n.text or '' for n in root.findall('.//{*}Reason/{*}code')]
        reasons = [n.text or '' for n in root.findall('.//{*}Reason/{*}text')]
        raise DataError('API acknowledgement, code ' + ','.join(codes) + ': ' + _safe_text(' '.join(reasons)))
    if text(root, '{*}type') != expected_type or text(root, '{*}process.processType') != 'A16':
        raise DataError('Unexpected document or process type')
    return root, text(root, '{*}mRID'), int(text(root, '{*}revisionNumber'))


def parse_load(body, start, end, fetched_at):
    root, doc, rev = _root(body, 'A65')
    series = root.findall('{*}TimeSeries')
    if len(series) != 1:
        raise DataError('Expected exactly one actual-load series')
    if text(series[0], '{*}outBiddingZone_Domain.mRID') != AREAS['BA']:
        raise DataError('Unexpected load area')
    rows = {}
    for left, right, res, val in _periods(series[0], start, end):
        if left in rows:
            raise DataError('Overlapping Periods')
        rows[left] = {'Dataset': 'actual_load', 'Area': 'BA', 'Interval_Start_UTC': iso(left),
                      'Interval_End_UTC': iso(right), 'Interval_Start_Local': left.astimezone(TZ).isoformat(),
                      'Resolution': res, 'Value_MW': val, 'Source_Document': doc, 'Revision': rev,
                      'Fetched_At_UTC': fetched_at}
    out = [rows[k] for k in sorted(rows)]
    if (not out or instant(out[0]['Interval_Start_UTC']) != start or instant(out[-1]['Interval_End_UTC']) != end
            or any(a['Interval_End_UTC'] != b['Interval_Start_UTC'] for a, b in zip(out, out[1:]))):
        raise DataError('Incomplete load day; not saved')
    return out


def parse_generation(body, start, end, fetched_at):
    root, doc, rev = _root(body, 'A75')
    rows = {}
    for ts in root.findall('{*}TimeSeries'):
        code = text(ts, '{*}MktPSRType/{*}psrType')
        if code not in PSR:
            raise DataError('Unknown production type')
        gen = ts.findtext('{*}inBiddingZone_Domain.mRID')
        con = ts.findtext('{*}outBiddingZone_Domain.mRID')
        if gen == AREAS['BA'] and con is None:
            flow = 'generation'
        elif con == AREAS['BA'] and gen is None:
            flow = 'consumption'
        else:
            raise DataError('Unexpected generation area')
        for left, right, res, val in _periods(ts, start, end, allow_negative=True):
            key = (code, flow, left)
            if key in rows:
                raise DataError('Overlapping Periods')
            rows[key] = {'Dataset': 'actual_generation', 'Area': 'BA', 'PSR_Code': code, 'PSR_Type': PSR[code],
                         'Flow': flow, 'Interval_Start_UTC': iso(left), 'Interval_End_UTC': iso(right),
                         'Interval_Start_Local': left.astimezone(TZ).isoformat(), 'Resolution': res,
                         'Value_MW': val, 'Source_Document': doc, 'Revision': rev, 'Fetched_At_UTC': fetched_at}
    if not rows:
        raise DataError('No generation points returned')
    return [rows[k] for k in sorted(rows, key=lambda k: (k[2], k[0], k[1]))]


def _read(path, cols):
    path = Path(path)
    if not path.exists():
        return []
    with path.open(newline='', encoding='utf-8') as h:
        r = csv.DictReader(h)
        if r.fieldnames != cols:
            raise DataError('Existing CSV schema mismatch')
        return list(r)


def _persist(path, cols, new, keyf, cutoff):
    merged = {keyf(r): r for r in _read(path, cols) if instant(r['Interval_Start_UTC']) >= cutoff}
    # a re-fetched day replaces that day's saved rows for the same key (later revision of same series)
    for r in new:
        merged[keyf(r)] = r
    out = io.StringIO(newline='')
    w = csv.DictWriter(out, fieldnames=cols, lineterminator='\n')
    w.writeheader()
    w.writerows(merged[k] for k in sorted(merged))
    atomic_text(path, out.getvalue())


def _day_rows(rows, start, end):
    return [r for r in rows if start <= instant(r['Interval_Start_UTC']) < end]


def _load_complete(rows, day):
    s, e = day_bounds(day)
    d = sorted(_day_rows(rows, s, e), key=lambda r: r['Interval_Start_UTC'])
    return bool(d) and instant(d[0]['Interval_Start_UTC']) == s and instant(d[-1]['Interval_End_UTC']) == e and \
        all(a['Interval_End_UTC'] == b['Interval_Start_UTC'] for a, b in zip(d, d[1:]))


def _gen_present(rows, day):
    s, e = day_bounds(day)
    return bool(_day_rows(rows, s, e))


def update_actuals(output_dir='.', now=None, session=None, token=None, max_backfill=10, pause=1):
    """Refresh yesterday; recover up to max_backfill earlier days in the 31-day window per dataset."""
    now = now or dt.datetime.now(UTC)
    yday = now.astimezone(TZ).date() - dt.timedelta(days=1)
    token = token if token is not None else os.environ.get('ENTSOE_API_TOKEN', '').strip()
    own = session is None
    session = session or make_session()
    stopped = False
    specs = {
        'actual_load': ('A65', LOAD_COLS, parse_load, lambda r: (r['Interval_Start_UTC'],), _load_complete,
                        {'outBiddingZone_Domain': AREAS['BA']}),
        'actual_generation': ('A75', GEN_COLS, parse_generation,
                              lambda r: (r['PSR_Code'], r['Flow'], r['Interval_Start_UTC']), None,
                              {'in_Domain': AREAS['BA']}),
    }
    try:
        for name, (doc, cols, parser, keyf, complete, extra) in specs.items():
            path = Path(output_dir) / ('entsoe_' + name + '.csv')
            meta = {'source': ENDPOINT, 'target_local_day': yday.isoformat()}
            if not token or stopped:
                record('entsoe_' + name, 'FAILED', detail='Token missing' if not token else 'Stopped after authentication/rate limit', **meta)
                continue
            cutoff = day_bounds(yday - dt.timedelta(days=WINDOW_DAYS - 2))[0]

            def get_day(d):
                s, e = day_bounds(d)
                p = {'documentType': doc, 'processType': 'A16', 'periodStart': s.strftime('%Y%m%d%H%M'),
                     'periodEnd': e.strftime('%Y%m%d%H%M')}
                p.update(extra)
                rows = parser(fetch(session, token, p), s, e, utc_now())
                time.sleep(pause)
                return rows
            try:
                _persist(path, cols, get_day(yday), keyf, cutoff)
            except StopAPI as exc:
                stopped = True
                record('entsoe_' + name, 'FAILED', detail=str(exc), **meta)
                continue
            except (APIError, DataError) as exc:
                record('entsoe_' + name, 'FAILED', detail='Request/validation failed: ' + str(exc).replace(token, '[redacted]'), **meta)
                continue
            except (ValueError, OSError, KeyError, TypeError) as exc:
                record('entsoe_' + name, 'FAILED', detail='Fetch/validation/write failed: ' + type(exc).__name__, **meta)
                continue
            saved = _read(path, cols)
            window = [yday - dt.timedelta(days=n) for n in range(1, WINDOW_DAYS - 1)]
            test = complete if complete else (lambda rows, d: _gen_present(rows, d))
            missing = [d for d in window if not test(saved, d)]
            attempt, skipped = missing[:max_backfill], missing[max_backfill:]
            recovered, unavailable = [], [d.isoformat() for d in skipped]
            for d in attempt:
                if stopped:
                    unavailable.append(d.isoformat()); continue
                try:
                    _persist(path, cols, get_day(d), keyf, cutoff)
                    recovered.append(d.isoformat())
                except StopAPI:
                    stopped = True; unavailable.append(d.isoformat())
                except (APIError, DataError, ValueError, OSError, KeyError, TypeError):
                    unavailable.append(d.isoformat())
            meta.update(recovered_days=recovered, unavailable_days=sorted(unavailable), unit='MW',
                        detail_note='Hourly as published by ENTSO-E; gaps are not zero')
            state = 'PARTIAL' if unavailable else 'OK'
            record('entsoe_' + name, state, detail=('Yesterday saved; ' + str(len(unavailable)) +
                   ' earlier day(s) unavailable or incomplete at source, gaps not zero') if unavailable
                   else 'Complete 31-day window saved; historical day data, not live', **meta)
    finally:
        if own:
            session.close()


if __name__ == '__main__':
    update_actuals()
