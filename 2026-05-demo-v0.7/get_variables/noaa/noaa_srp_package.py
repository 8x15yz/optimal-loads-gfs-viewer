#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local-only NOAA SRP ZIP + seven-variable extraction (Python 3.9+, no packages).

Quick test: python noaa_srp_package.py --sample-files 4
Full run:   python noaa_srp_package.py
Probe:      python noaa_srp_package.py --probe

Select latest complete 209-GRIB run. Download original GRIBs and available .idx
sidecars; create ZIP_STORED with originals, indices and manifest.json. Then read
89 selected steps from the ZIP and copy WIND/WDIR/UGRD/VGRD/HTSGW/PERPW/DIRPW
messages unchanged into the existing ingest naming/layout. No JPEG decoding.
Missing/invalid local variable or index: fetch that variable from NOMADS for
the SAME cycle/step, unless --no-fallback. Every output is checked against its
parameter code, surface, cycle and step. No S3 upload, Mongo or S-100 conversion.

Default originals: f000..f120 hourly, f123..f384 every 3 hours (209 files).
Split steps: f000..f144 every 3 hours, f150..f384 every 6 hours (89 steps).
--sample-files 4 downloads f000..f003, splits f000/f003: 14 variable files.
Each invocation downloads afresh. This TEST VERSION has no cron deduplication.
Final ZIP survives extraction failure; extraction_report.json records results.
Exit 0=success, 1=failure (including partial extraction), 130=Ctrl+C.
--keep-grib retains originals too; otherwise temp originals are removed.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import threading
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import uuid
import xml.etree.ElementTree as ET
import zipfile

BASE_URL = 'https://noaa-gfs-bdp-pds.s3.amazonaws.com'
STEPS = tuple(range(121)) + tuple(range(123, 385, 3))
SPLIT_STEPS = tuple(range(0, 145, 3)) + tuple(range(150, 385, 6))
PARAMETERS = {
    'WIND': (0, 2, 1), 'WDIR': (0, 2, 0),
    'UGRD': (0, 2, 2), 'VGRD': (0, 2, 3),
    'HTSGW': (10, 0, 3), 'PERPW': (10, 0, 11), 'DIRPW': (10, 0, 10),
}
FILTER_URL = 'https://nomads.ncep.noaa.gov/cgi-bin/filter_gfswave.pl'
UTC = timezone.utc
CHUNK = 1024 * 1024
NS = {'s': 'http://s3.amazonaws.com/doc/2006-03-01/'}


def log(message):
    print(message, flush=True)


def iso(dt):
    return dt.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_run(value):
    try:
        dt = datetime.strptime(value, '%Y%m%d%H').replace(tzinfo=UTC)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Use YYYYMMDDHH in UTC, e.g. 2026100300') from exc
    if dt.hour not in (0, 6, 12, 18):
        raise argparse.ArgumentTypeError('Run hour must be 00, 06, 12 or 18 UTC')
    return dt


class SourceChanged(RuntimeError):
    pass


class Client:
    def __init__(self, timeout=30, retries=3, max_minutes=90):
        self.timeout = timeout
        self.retries = retries
        self.deadline = time.monotonic() + max_minutes * 60

    def check(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError('Overall time limit exceeded')

    def fetch(self, url, *, target=None, etag=None):
        """Retry whole file with bounded memory; conditional GET pins listed ETag."""
        for attempt in range(1, self.retries + 1):
            self.check()
            headers = {'User-Agent': 'BlueMap-SRP-local-test/1.0', 'Accept-Encoding': 'identity'}
            if etag:
                headers['If-Match'] = etag
            try:
                timeout = max(0.1, min(self.timeout, self.deadline - time.monotonic()))
                with urlopen(Request(url, headers=headers), timeout=timeout) as response:
                    if response.status != 200:
                        raise RuntimeError(f'Unexpected HTTP {response.status}')
                    if etag and response.headers.get('ETag') != etag:
                        raise SourceChanged('Source ETag changed; rerun packaging')
                    if target is None:
                        return response.read()
                    digest = hashlib.sha256()
                    size = 0
                    with target.open('wb') as out:
                        while True:
                            self.check()
                            block = response.read(CHUNK)
                            if not block:
                                break
                            out.write(block)
                            digest.update(block)
                            size += len(block)
                    length = response.headers.get('Content-Length')
                    if length is not None and size != int(length):
                        raise RuntimeError(f'Short download: {size} vs {length}')
                    return size, digest.hexdigest()
            except HTTPError as exc:
                if exc.code in (404, 412):
                    raise SourceChanged(f'Source missing/changed (HTTP {exc.code}); rerun') from exc
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    raise
                error = exc
            except (URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
                error = exc
            if attempt == self.retries:
                raise RuntimeError(f'Download failed after {attempt} attempts: {error}') from error
            log(f'[retry {attempt}/{self.retries}] {error}')
            time.sleep(min(2 ** attempt, 8))

    def inventory(self, run):
        prefix = f'gfs.{run:%Y%m%d}/{run:%H}/wave/gridded/'
        pattern = re.compile(rf'gfswave\.t{run:%H}z\.global\.0p25\.f(\d{{3}})\.grib2(\.idx)?')
        objects = {}
        indices = {}
        token = None
        while True:
            params = {'list-type': '2', 'prefix': prefix, 'max-keys': '1000'}
            if token:
                params['continuation-token'] = token
            root = ET.fromstring(self.fetch(BASE_URL + '/?' + urlencode(params)))
            for obj in root.findall('s:Contents', NS):
                key = obj.findtext('s:Key', namespaces=NS)
                match = pattern.fullmatch(key[len(prefix):])
                if match and int(match[1]) in STEPS:
                    size = int(obj.findtext('s:Size', namespaces=NS))
                    if size > 0:
                        table = indices if match[2] else objects
                        table[int(match[1])] = {
                            'key': key, 'size_bytes': size,
                            'etag': obj.findtext('s:ETag', namespaces=NS),
                            'last_modified': obj.findtext('s:LastModified', namespaces=NS),
                        }
            if root.findtext('s:IsTruncated', namespaces=NS) != 'true':
                break
            token = root.findtext('s:NextContinuationToken', namespaces=NS)
            if not token:
                raise RuntimeError('Truncated S3 listing without continuation token')
        for step, obj in objects.items():
            obj['idx'] = indices.get(step)
        return objects


def discover(client, run=None, lookback_runs=12):
    now = datetime.now(UTC)
    newest = run or now.replace(hour=(now.hour // 6) * 6, minute=0, second=0, microsecond=0)
    for index in range(1 if run else lookback_runs):
        candidate = newest - timedelta(hours=6 * index)
        objects = client.inventory(candidate)
        log(f'[discover] {iso(candidate)}: {len(objects)}/209 files')
        if all(step in objects and objects[step].get("idx") for step in STEPS):
            return candidate, objects
    raise RuntimeError('No complete 209-file run found. Try later or increase --lookback-runs.')


def validate_grib(path):
    """Check concatenated GRIB2 framing/length/end markers; no field decoding."""
    size = path.stat().st_size
    offset = count = 0
    with path.open('rb') as source:
        while offset < size:
            source.seek(offset)
            header = source.read(16)
            if len(header) != 16 or header[:4] != b'GRIB' or header[7] != 2:
                raise RuntimeError(f'Invalid GRIB2 header at byte {offset}: {path.name}')
            length = int.from_bytes(header[8:16], 'big')
            if length < 20 or offset + length > size:
                raise RuntimeError(f'Invalid GRIB2 message length: {path.name}')
            source.seek(offset + length - 4)
            if source.read(4) != b'7777':
                raise RuntimeError(f'Invalid GRIB2 end marker: {path.name}')
            offset += length
            count += 1
    if not count:
        raise RuntimeError('Empty GRIB2 file')
    return count


def parse_idx(text, file_size):
    """Parse NOAA wgrib2 inventory: record:offset:d=cycle:variable:level:..."""
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.strip().split(':')
        if len(fields) < 6 or not fields[0].isdigit() or not fields[1].isdigit():
            raise ValueError(f'Unsupported IDX row: {line[:120]}')
        offset = int(fields[1])
        if offset < 0 or offset >= file_size or (rows and offset <= rows[-1]['offset']):
            raise ValueError('IDX offsets outside file or not increasing')
        rows.append({'offset': offset, 'variable': fields[3], 'level': fields[4]})
    if not rows or rows[0]['offset'] != 0:
        raise ValueError('IDX must start at byte 0')
    for index, row in enumerate(rows):
        end = rows[index + 1]['offset'] if index + 1 < len(rows) else file_size
        row['length'] = end - row['offset']
    return rows


def message_info(data):
    """Read uncompressed headers only. Support one GRIB2 template-4.0 field.

    This is intentionally a narrow GFS-Wave reader, not a generic GRIB decoder.
    Packed section 7 is never decoded or altered. Unsupported structures fail.
    """
    if (len(data) < 20 or data[:4] != b'GRIB' or data[7] != 2
            or int.from_bytes(data[8:16], 'big') != len(data) or data[-4:] != b'7777'):
        raise ValueError('Invalid GRIB2 message framing')
    sections = {}
    offset = 16
    while offset < len(data) - 4:
        if offset + 5 > len(data) - 4:
            raise ValueError('Truncated GRIB section')
        length = int.from_bytes(data[offset:offset + 4], 'big')
        number = data[offset + 4]
        if length < 5 or offset + length > len(data) - 4 or number in sections:
            raise ValueError('Invalid/repeated GRIB section (multi-field messages unsupported)')
        sections[number] = data[offset:offset + length]
        offset += length
    if offset != len(data) - 4 or not all(n in sections for n in (1, 3, 4, 5, 6, 7)):
        raise ValueError('Missing GRIB sections')
    ident, product = sections[1], sections[4]
    if len(ident) < 21 or len(product) < 34 or int.from_bytes(product[7:9], 'big') != 0:
        raise ValueError('Only product template 4.0 supported')
    cycle = datetime(int.from_bytes(ident[12:14], 'big'), *ident[14:19], tzinfo=UTC)
    if product[17] != 1:
        raise ValueError('Forecast time unit must be hours')
    return {'codes': (data[6], product[9], product[10]), 'cycle': cycle,
            'step': int.from_bytes(product[18:22], 'big'), 'surface': product[22]}


def check_field(data, variable, cycle, step):
    info = message_info(data)
    if (info['codes'] != PARAMETERS[variable] or info['cycle'] != cycle
            or info['step'] != step or info['surface'] != 1):
        raise ValueError(f'Field metadata mismatch for {variable} f{step:03}: {info}')
    return info


def local_field(path, rows, variable, cycle, step):
    candidates = [row for row in rows if row['variable'] == variable and row['level'] == 'surface']
    if len(candidates) != 1:
        raise ValueError(f'Expected one surface {variable} in IDX, found {len(candidates)}')
    row = candidates[0]
    if row['length'] > 128 * 1024**2:
        raise ValueError('Unexpectedly large message')
    with path.open('rb') as source:
        source.seek(row['offset'])
        data = source.read(row['length'])
    check_field(data, variable, cycle, step)
    return data


def fallback_field(client, variable, cycle, step, target):
    """NOMADS variable filter for the exact same cycle, step and surface."""
    params = {'file': f'gfswave.t{cycle:%H}z.global.0p25.f{step:03}.grib2',
              'dir': f'/gfs.{cycle:%Y%m%d}/{cycle:%H}/wave/gridded',
              f'var_{variable}': 'on', 'lev_surface': 'on',
              'subregion': '', 'leftlon': '0', 'rightlon': '360',
              'toplat': '90', 'bottomlat': '-90'}
    client.check()
    time.sleep(1)  # Space out fallback requests.
    size, sha = client.fetch(FILTER_URL + '?' + urlencode(params), target=target)
    if size > 128 * 1024**2:
        raise ValueError('Unexpectedly large NOMADS response')
    data = target.read_bytes()
    check_field(data, variable, cycle, step)  # also rejects HTML or wrong-run data
    return data


def extract_package(zip_path, split_dir, cycle, downloaded_steps, client, *, no_fallback=False, run_set_dir=None):
    """Read selected ZIP entries one at a time, write atomic variable files.

    Returns the report; raises after writing a partial report if any field failed.
    A previously finalized original ZIP is never deleted by this function.
    """
    split_dir = Path(split_dir)
    split_dir.mkdir(parents=True, exist_ok=True)
    steps = [s for s in downloaded_steps if s in SPLIT_STEPS]
    report = {'forecast_cycle': iso(cycle), 'source_zip': Path(zip_path).name,
              'split_steps': steps, 'full_split_steps': list(SPLIT_STEPS),
              'expected_files': len(steps) * len(PARAMETERS),
              'full_expected_files': 623, 'sample': set(steps) != set(SPLIT_STEPS),
              'local_files': 0, 'fallback_files': 0, 'failed_files': 0,
              'complete': False, 'files': []}
    report_path = split_dir / 'extraction_report.json'
    log(f'[split] {len(steps)} steps x 7 variables = {report["expected_files"]} files')
    try:
        with zipfile.ZipFile(zip_path) as archive, tempfile.TemporaryDirectory(prefix='.split-work-', dir=split_dir) as tmp:
            work = Path(tmp)
            for step in steps:
                client.check()
                filename = f'gfswave.t{cycle:%H}z.global.0p25.f{step:03}.grib2'
                raw = work / 'step.grib2'
                rows, local_error = None, None
                try:
                    # At most one full step temporarily on disk; no full-ZIP extraction.
                    with archive.open(filename) as src, raw.open('wb') as dst:
                        while True:
                            client.check()
                            block = src.read(CHUNK)
                            if not block:
                                break
                            dst.write(block)
                    if archive.getinfo(filename + '.idx').file_size > 4 * 1024**2:
                        raise ValueError('Unexpectedly large index')
                    idx_text = archive.read(filename + '.idx').decode('utf-8')
                    rows = parse_idx(idx_text, raw.stat().st_size)
                except (KeyError, ValueError, UnicodeError, zipfile.BadZipFile) as exc:
                    local_error = str(exc)
                for variable in PARAMETERS:
                    client.check()
                    rel = (Path('noaa/gfs/fc') / cycle.strftime('%Y/%m/%d/%HZ') / 'wave' / variable
                           / f'original_{variable}_{cycle:%Y%m%d_%H}Z_step{step:03}.grib2')
                    output = (Path(run_set_dir) / "wave" / variable / rel.name) if run_set_dir else split_dir / rel
                    output.parent.mkdir(parents=True, exist_ok=True)
                    partial = output.with_suffix('.grib2.part')
                    record = {'variable': variable, 'step_hours': step, 'path': rel.as_posix()}
                    reason = local_error
                    try:
                        if rows is None:
                            raise ValueError(reason or 'No index')
                        data = local_field(raw, rows, variable, cycle, step)
                    except ValueError as exc:
                        reason = str(exc)
                        data = None
                    try:
                        origin = 'local'
                        if data is None:
                            if no_fallback:
                                raise ValueError(f'Local extraction unavailable; fallback disabled: {reason}')
                            log(f'[fallback] {variable} f{step:03}: {reason}')
                            data = fallback_field(client, variable, cycle, step, partial)
                            origin = 'nomads'
                        else:
                            partial.write_bytes(data)
                        # Original message bytes preserved; atomic publish per output.
                        partial.replace(output)
                        record.update(status='ok', source=origin, size_bytes=len(data),
                                      sha256=hashlib.sha256(data).hexdigest())
                        if reason:
                            record['fallback_reason'] = reason
                        report['local_files' if origin == 'local' else 'fallback_files'] += 1
                        log(f'[split OK] f{step:03} {variable:5} | {origin} | {len(data):,} bytes')
                    except Exception as exc:
                        partial.unlink(missing_ok=True)
                        record.update(status='failed', error=str(exc), fallback_reason=reason)
                        report['failed_files'] += 1
                        log(f'[split FAILED] f{step:03} {variable}: {exc}')
                    finally:
                        report['files'].append(record)
                        data = None
                raw.unlink(missing_ok=True)
        report['complete'] = (len(report['files']) == report['expected_files'] and report['failed_files'] == 0)
    finally:
        report['finished_at'] = iso(datetime.now(UTC))
        report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
        log(f'[split report] {report_path}')
    log(f'[split DONE] local={report["local_files"]}, fallback={report["fallback_files"]}, failed={report["failed_files"]}')
    if not report['complete']:
        raise RuntimeError(f'Extraction incomplete; ZIP kept. See {report_path}')
    return report


def _build_package(output_dir=None, *, run=None, lookback_runs=12,
                  sample_files=0, probe=False, timeout=30, retries=3,
                  max_minutes=90, keep_grib=False, no_fallback=False, zip_only=False):
    """Return final local ZIP Path (None for probe). Failures raise exceptions.

    Full ZIPs contain 209 untouched source GRIB2 files, available .idx files,
    and manifest.json. Split results are saved in a sibling *_split folder.
    Temporary data is cleaned on normal failure/Ctrl+C; force-kill/power loss
    may leave a .srp-work-* folder that can be removed after process exit.
    """
    if not 0 <= sample_files <= 209:
        raise ValueError('sample_files must be 0..209')
    if min(lookback_runs, timeout, retries, max_minutes) <= 0:
        raise ValueError('Limits must be positive')
    client = Client(timeout, retries, max_minutes)
    cycle, inventory = discover(client, run, lookback_runs)
    steps = STEPS[:sample_files] if sample_files else STEPS
    total = sum(inventory[s]['size_bytes'] for s in steps)
    log(f'[selected] {iso(cycle)} | {len(steps)} files | {total / 1024**3:.2f} GiB')
    if probe:
        log('[probe] Listing only. No GRIB files downloaded.')
        return None
    root = Path(output_dir) if output_dir else Path(__file__).resolve().parent / 'srp_output'
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    split_budget = sum(inventory[s]['size_bytes'] for s in steps if s in SPLIT_STEPS)
    required = total + split_budget + max(inventory[s]['size_bytes'] for s in steps)
    if keep_grib:
        required += total
    required += 256 * 1024**2
    if shutil.disk_usage(root).free < required:
        raise RuntimeError(f'Need at least {required / 1024**3:.2f} GiB FREE disk space in {root}')
    suffix = '_SAMPLE' if sample_files else ''
    name = f'gfswave_global_0p25_{cycle:%Y%m%d_%HZ}{suffix}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}'
    final = root / (name + '.zip')
    started = time.monotonic()
    manifest = {'source': BASE_URL, 'forecast_cycle': iso(cycle),
                'complete_package': not bool(sample_files), 'expected_full_file_count': 209,
                'file_count': len(steps), 'steps_hours': list(steps),
                'compression': 'ZIP_STORED', 'files': []}
    with tempfile.TemporaryDirectory(prefix='.srp-work-', dir=root) as tmp:
        work = Path(tmp)
        grib_dir = work / 'grib'
        grib_dir.mkdir()
        zip_path = work / 'package.zip.part'
        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for index, step in enumerate(steps, 1):
                client.check()
                item = inventory[step]
                filename = item['key'].rsplit('/', 1)[1]
                local = grib_dir / filename
                log(f'[{index:03}/{len(steps)}] downloading {filename}')
                size, sha = client.fetch(BASE_URL + '/' + quote(item['key'], safe='/'), target=local, etag=item['etag'])
                if size != item['size_bytes']:
                    raise SourceChanged('Downloaded size differs from inventory; rerun')
                count = validate_grib(local)
                archive.write(local, filename)
                idx_info = item.get('idx')
                idx_status, idx_error, idx_sha = 'missing', None, None
                if idx_info:
                    idx_path = grib_dir / (filename + '.idx')
                    try:
                        idx_size, idx_sha = client.fetch(
                            BASE_URL + '/' + quote(idx_info['key'], safe='/'),
                            target=idx_path, etag=idx_info['etag'])
                        if idx_size != idx_info['size_bytes']:
                            raise SourceChanged('IDX size changed')
                        # Preserve even an invalid sidecar for inspection; extraction
                        # checks offsets AND GRIB field metadata before using it.
                        archive.write(idx_path, filename + '.idx')
                        idx_status = 'downloaded'
                    except SourceChanged:
                        raise
                    except Exception as exc:
                        client.check()
                        idx_status, idx_error = 'download_failed', str(exc)
                        log(f'[idx warning] {filename}: {exc}; extraction may use NOMADS')
                    finally:
                        if not keep_grib:
                            idx_path.unlink(missing_ok=True)
                else:
                    log(f'[idx warning] {filename}: no index listed; extraction may use NOMADS')
                manifest['files'].append(dict(item, filename=filename, step_hours=step,
                                              sha256=sha, grib_message_count=count,
                                              idx_status=idx_status, idx_error=idx_error,
                                              idx_sha256=idx_sha))
                if not keep_grib:
                    local.unlink()
                log(f'           OK {size / 1024**2:.1f} MiB | {count} GRIB messages')
            # Recheck only this selected run, never switch cycles midway.
            log('[verify] Rechecking source inventory for changes...')
            after = client.inventory(cycle)
            if any(after.get(s) != inventory[s] for s in steps):
                raise SourceChanged('Source inventory changed during packaging; rerun')
            manifest['packaged_at'] = iso(datetime.now(UTC))
            archive.writestr('manifest.json', json.dumps(manifest, indent=2))
        if keep_grib:
            shutil.move(str(grib_dir), str(root / (name + '_grib')))
        zip_path.replace(final)
    log(f'[ZIP DONE] {final}')
    if zip_only:
        return final
    # Future S3 upload belongs HERE. Its failure must not suppress extraction.
    split_dir = root / (name + '_split')
    extract_package(final, split_dir, cycle, steps, client, no_fallback=no_fallback)
    log(f'       {final.stat().st_size / 1024**3:.2f} GiB | {(time.monotonic() - started) / 60:.1f} minutes')
    return final



def process_memory():
    """Return current RSS and OS peak RSS in bytes (this process only)."""
    if sys.platform == 'win32':
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                        ('PeakWorkingSetSize', ctypes.c_size_t),
                        ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t),
                        ('PeakPagefileUsage', ctypes.c_size_t)]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        info = Counters()
        info.cb = ctypes.sizeof(info)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(info), info.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.WorkingSetSize, info.PeakWorkingSetSize
    if sys.platform.startswith('linux'):
        values = {}
        with open('/proc/self/status', encoding='ascii') as source:
            for line in source:
                if line.startswith(('VmRSS:', 'VmHWM:')):
                    key, value, _ = line.split()
                    values[key.rstrip(':')] = int(value) * 1024
        return values['VmRSS'], values['VmHWM']
    raise RuntimeError('RAM monitor supports Windows and Linux')


class MemoryMonitor:
    def __init__(self, interval=10):
        self.interval = interval
        self.stop_event = threading.Event()
        self.peak = 0
        self.current = 0
        self.error = None
        self.started = time.monotonic()
        self.thread = None

    def sample(self):
        self.current, peak = process_memory()
        self.peak = max(self.peak, peak, self.current)

    def report(self, label):
        log(f'[RAM {label}] current={self.current / 1024**2:.1f} MiB | '
            f'peak={self.peak / 1024**2:.1f} MiB | '
            f'elapsed={time.monotonic() - self.started:.0f}s | PID={os.getpid()}')

    def loop(self):
        next_print = time.monotonic() + self.interval
        while not self.stop_event.wait(0.5):
            try:
                self.sample()
                if time.monotonic() >= next_print:
                    self.report('progress')
                    next_print = time.monotonic() + self.interval
            except Exception as exc:
                self.error = str(exc)
                log(f'[RAM] Monitor unavailable: {exc}')
                return

    def __enter__(self):
        try:
            self.sample()
            self.report('start')
            self.thread = threading.Thread(target=self.loop, daemon=True)
            self.thread.start()
        except Exception as exc:
            self.error = str(exc)
            log(f'[RAM] Monitor unavailable: {exc}')
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        if not self.error:
            try:
                self.sample()
                self.report('final')
            except Exception as error:
                log(f'[RAM] Final sample unavailable: {error}')


def build_package(output_dir=None, *, run=None, lookback_runs=12,
                  sample_files=0, probe=False, timeout=30, retries=3,
                  max_minutes=90, keep_grib=False, memory_interval=10, no_fallback=False):
    """Public entry point; monitor this process's RSS while creating a local ZIP.

    RAM includes Python and native allocations, but not total system RAM,
    filesystem cache or other processes. OS peak is process-lifetime peak;
    when imported by a long-running gate, it may include earlier work.
    Use a separate child process for isolated package memory measurements.
    """
    if memory_interval <= 0:
        raise ValueError('memory_interval must be positive')
    with MemoryMonitor(memory_interval):
        return _build_package(output_dir, run=run, lookback_runs=lookback_runs,
                              sample_files=sample_files, probe=probe, timeout=timeout,
                              retries=retries, max_minutes=max_minutes, keep_grib=keep_grib, no_fallback=no_fallback)

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output-dir', help='Default: srp_output beside this script')
    parser.add_argument('--run', type=parse_run, help='Exact cycle YYYYMMDDHH (UTC); default latest complete')
    parser.add_argument('--lookback-runs', type=int, default=12)
    parser.add_argument('--probe', action='store_true', help='List/check only; no GRIB download')
    parser.add_argument('--sample-files', type=int, default=0, help='1..209 for a SAMPLE zip; 0 = full package')
    parser.add_argument('--keep-grib', action='store_true', help='Also retain individual GRIBs (about twice the disk)')
    parser.add_argument('--timeout', type=int, default=30, help='Network timeout seconds per operation')
    parser.add_argument('--retries', type=int, default=3)
    parser.add_argument('--max-minutes', type=int, default=90, help='Overall cooperative deadline')
    parser.add_argument('--memory-interval', type=int, default=10, help='Print process RAM every N seconds')
    parser.add_argument('--no-fallback', action='store_true', help='Disable NOMADS fallback; report missing fields as failed')
    args = parser.parse_args(argv)
    try:
        build_package(**vars(args))
        return 0
    except KeyboardInterrupt:
        log('[CANCELLED] Temporary files cleaned. Completed earlier ZIPs are kept.')
        return 130
    except Exception as exc:
        log(f'[ERROR] {type(exc).__name__}: {exc}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
