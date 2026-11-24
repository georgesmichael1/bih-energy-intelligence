"""Bounded Bosnia ENTSO-E feeds. No actual-load, generation or price publishing.

All timestamps in the CSV primary key are UTC; local ISO timestamps include the
UTC offset, including repeated fall-back hours. Positive directional quantities
mean From_Area -> To_Area, not a signed net flow. MAW is the ENTSO-E code for MW.
"""
import csv
import datetime as dt
import io
import math
import os
from pathlib import Path
import re
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
AREAS = {'BA': '10YBA-JPCC-----B', 'HR': '10YHR-HEP------M',
         'RS': '10YCS-SERBIATSOV', 'ME': '10YCS-CG-TSO---S'}
NS = 'urn:iec62325.351:tc57wg16:451-5:statusrequestdocument:4:0'
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


def parse_document(body, dataset, source, target, start, end, fetched_at):
    if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
        raise DataError('XML declarations not supported')
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise DataError('Invalid XML') from exc
    if root.tag.split('}')[-1] == 'Acknowledgement_MarketDocument':
        # The free-text reason may contain request details. Do not log it.
        codes = [n.text for n in root.findall('.//{*}Reason/{*}code')]
        raise DataError('API acknowledgement, code ' + ','.join(codes))
    expected_type = {'load_forecast': 'A65', 'physical_flows': 'A11',
                     'hr_schedules': 'A09'}[dataset]
    if text(root, '{*}type') != expected_type:
        raise DataError('Unexpected document type')
    if dataset == 'load_forecast' and text(root, '{*}process.processType') != 'A01':
        raise DataError('Unexpected forecast process')
    document = text(root, '{*}mRID')
    revision = int(text(root, '{*}revisionNumber'))
    series = root.findall('{*}TimeSeries')
    # Do not silently add duplicate/alternative series or choose a revision.
    if len(series) != 1:
        raise DataError('Expected exactly one aggregate directional series')
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
        if dataset == 'hr_schedules' and text(ts, '{*}contract_MarketAgreement.type') != 'A01':
            raise DataError('Not a day-ahead schedule')
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


def request_xml(parameters):
    root = ET.Element('StatusRequest_MarketDocument', xmlns=NS)
    fields = [('mRID', 'BiHFeed'), ('type', 'A59'),
              ('sender_MarketParticipant.mRID', '10X1001A1001A450'),
              ('sender_MarketParticipant.marketRole.type', 'A07'),
              ('receiver_MarketParticipant.mRID', '10X1001A1001A450'),
              ('receiver_MarketParticipant.marketRole.type', 'A32'),
              ('createdDateTime', iso(dt.datetime.now(UTC)))]
    for tag, value in fields:
        node = ET.SubElement(root, tag)
        node.text = value
        if tag.endswith('.mRID'):
            node.set('codingScheme', 'A01')
    for key, value in parameters.items():
        component = ET.SubElement(root, 'AttributeInstanceComponent')
        ET.SubElement(component, 'attribute').text = key
        ET.SubElement(component, 'attributeValue').text = value
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def make_session():
    session = requests.Session()
    session.mount('https://', HTTPAdapter(max_retries=Retry(
        total=2, backoff_factor=1, status_forcelist=[500, 502, 503, 504],
        allowed_methods=['POST'], raise_on_status=False)))
    return session


def fetch(session, token, parameters):
    try:
        response = session.post(ENDPOINT, data=request_xml(parameters),
                                headers={'Content-Type': 'application/xml',
                                         'Accept': 'application/xml', 'SECURITY_TOKEN': token},
                                timeout=(10, 45), allow_redirects=False)
    except requests.RequestException:
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
    writer = csv.DictWriter(out, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(merged[k] for k in sorted(merged))
    atomic_text(path, out.getvalue())


def update_entsoe(output_dir='.', day=None, session=None, token=None):
    day = day or (dt.datetime.now(TZ).date() - dt.timedelta(days=1))
    start, end = day_bounds(day)
    groups = {
        'load_forecast': [('BA', 'BA')],
        'physical_flows': [(a, b) for other in ('HR', 'RS', 'ME')
                           for a, b in [('BA', other), (other, 'BA')]],
        'hr_schedules': [('BA', 'HR'), ('HR', 'BA')],
    }
    token = token if token is not None else os.environ.get('ENTSOE_API_TOKEN', '').strip()
    owns_session = session is None
    session = session or make_session()
    stopped = False
    try:
        for dataset, directions in groups.items():
            name = 'entsoe_' + dataset
            meta = {'target_local_day': day.isoformat(), 'source': ENDPOINT,
                    'coverage_start_utc': iso(start), 'coverage_end_utc': iso(end)}
            if not token or stopped:
                record(name, 'FAILED', detail='Token missing' if not token else 'Stopped after authentication/rate limit', **meta)
                continue
            try:
                rows = []
                for source, target in directions:
                    parameters = {'DocumentType': {'load_forecast': 'A65', 'physical_flows': 'A11', 'hr_schedules': 'A09'}[dataset],
                                  'TimeInterval': start.strftime('%Y-%m-%dT%H:%MZ') + '/' + end.strftime('%Y-%m-%dT%H:%MZ')}
                    if dataset == 'load_forecast':
                        parameters.update({'ProcessType': 'A01', 'OutBiddingZone_Domain': AREAS['BA']})
                    else:
                        parameters.update({'In_Domain': AREAS[target], 'Out_Domain': AREAS[source]})
                        if dataset == 'hr_schedules':
                            parameters['Contract_MarketAgreement.Type'] = 'A01'
                    body = fetch(session, token, parameters)
                    rows.extend(parse_document(body, dataset, source, target, start, end, utc_now()))
                    time.sleep(1)
                persist(Path(output_dir) / (name + '.csv'), rows, start - dt.timedelta(days=29))
                record(name, 'OK', detail='Complete requested historical day; not current-day data',
                       row_count=len(rows), directions=len(directions), unit='MW', **meta)
            except StopAPI as exc:
                stopped = True
                record(name, 'FAILED', detail=str(exc), **meta)
            except (APIError, DataError, ValueError, OSError, KeyError, TypeError) as exc:
                # No response text, request dump, headers or secret values in errors.
                record(name, 'FAILED', detail='Fetch/validation/write failed: ' + type(exc).__name__, **meta)
    finally:
        if owns_session:
            session.close()
