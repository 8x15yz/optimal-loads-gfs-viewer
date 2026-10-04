"""SRP publication and restart state. No network calls at import time."""
import hashlib
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from boto3.s3.transfer import TransferConfig
from noaa_srp_package import Client, STEPS, PARAMETERS, _build_package, extract_package, check_field, iso

PACKAGE_TYPE = 'noaa_gfswave_srp'

def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def valid_field(path, var, run, step):
    try:
        if not 20 <= path.stat().st_size <= 128 * 1024**2:
            return False
        check_field(path.read_bytes(), var, run, step)
        return True
    except (OSError, ValueError, IndexError):
        return False

class PackageStore:
    def __init__(self, run, run_dir, s3=None, collection=None, bucket=None, region=None):
        self.run, self.run_dir = run, Path(run_dir)
        self.root = self.run_dir / 'srp'
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'package.zip'
        self.receipt = self.root / 'receipt.json'
        self.state_path = self.root / 'state.json'
        self.s3, self.collection = s3, collection
        self.bucket = os.getenv('SRP_S3_BUCKET') or bucket or 'optimal-loads'
        self.region = os.getenv('SRP_S3_REGION') or region or 'ap-northeast-2'
        prefix = os.getenv('SRP_S3_PREFIX', 'noaa/gfs/fc').strip('/')
        self.key = f'{prefix}/{run:%Y/%m/%d/%HZ}/srp/noaa_gfswave_{run:%Y%m%d_%HZ}.zip'
        self.id = f'{PACKAGE_TYPE}|{iso(run)}'
        if collection is not None:
            collection.create_index([('package_type', 1), ('status', 1), ('run_time_utc', -1)])
        self.transfer = TransferConfig(max_concurrency=1, use_threads=False, multipart_chunksize=8*1024**2)

    def document(self):
        if self.collection is not None:
            return self.collection.find_one({'_id': self.id}) or {}
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def update(self, **fields):
        base = {'package_type': PACKAGE_TYPE, 'source': 'noaa', 'model': 'gfswave',
                'run_time_utc': iso(self.run), 'updated_at': iso(datetime.now(timezone.utc))}
        base.update(fields)
        if self.collection is not None:
            self.collection.update_one({'_id': self.id}, {'$set': base,
                '$setOnInsert': {'created_at': base['updated_at']}}, upsert=True)
        else:
            doc = self.document()
            for key, value in base.items():
                cursor = doc
                parts = key.split('.')
                for part in parts[:-1]:
                    cursor = cursor.setdefault(part, {})
                cursor[parts[-1]] = value
            tmp = self.state_path.with_suffix('.part')
            tmp.write_text(json.dumps(doc, indent=2))
            tmp.replace(self.state_path)

    def converted(self, product, s100_collection):
        record = self.document().get('conversions', {}).get(product, {})
        keys = record.get('keys', [])
        if record.get('status') != 'ready' or not keys or s100_collection is None:
            return False
        count = s100_collection.count_documents({'product': product,
            'run_time_utc': iso(self.run), 's3.key': {'$in': keys}, 'missing': {'$ne': True}})
        return count == len(keys)

    def mark_conversion(self, product, keys):
        if self.s3 is not None and self.collection is not None and keys:
            self.update(**{f'conversions.{product}': {'status': 'ready', 'keys': sorted(set(keys)),
                         'completed_at': iso(datetime.now(timezone.utc))}})

    def metadata(self):
        with zipfile.ZipFile(self.path) as z:
            m = json.loads(z.read('manifest.json'))
            rows = m.get('files', [])
            if (m.get('forecast_cycle') != iso(self.run) or not m.get('complete_package')
                or m.get('file_count') != len(STEPS) or len(rows) != len(STEPS)
                or m.get('steps_hours') != list(STEPS)
                or sorted(r['step_hours'] for r in rows) != list(STEPS)):
                raise ValueError('Not a complete ZIP for the selected run')
            variables = set()
            for row in rows:
                if row.get('idx_status') != 'downloaded' or row.get('grib_message_count') != 19:
                    raise ValueError('Expected 19 messages and IDX for every GRIB')
                if z.getinfo(row['filename']).file_size != row['size_bytes']:
                    raise ValueError('ZIP member size mismatch')
                idx = z.read(row['filename'] + '.idx')
                if hashlib.sha256(idx).hexdigest() != row['idx_sha256']:
                    raise ValueError('IDX checksum mismatch')
                for line in idx.decode().splitlines():
                    fields = line.split(':')
                    if len(fields) >= 5:
                        variables.add((fields[3], fields[4]))
            return {'grib_file_count': len(rows), 'idx_file_count': len(rows),
                'forecast_steps': list(STEPS), 'messages_per_grib': 19,
                'variables': [{'variable': v, 'level': level} for v, level in sorted(variables)],
                'size_bytes': self.path.stat().st_size, 'sha256': digest(self.path)}

    def ensure_local(self, doc):
        # The caller is protected by the ingest lock: orphan partials are safe to clear.
        for p in self.root.glob('.srp-work-*'):
            if p.is_dir():
                shutil.rmtree(p)
        for p in self.root.glob('.split-work-*'):
            if p.is_dir():
                shutil.rmtree(p)
        if self.path.exists():
            try:
                receipt = json.loads(self.receipt.read_text())
                if receipt['sha256'] == digest(self.path):
                    return self.metadata()
            except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                pass
            self.path.unlink()
        # A ready remote ZIP is reusable if conversion or extraction needs local sources.
        if doc.get('status') == 'ready' and self.s3 is not None:
            size = doc['size_bytes']
            if shutil.disk_usage(self.root).free < size + 768 * 1024**2:
                raise RuntimeError('Not enough free disk for ZIP and extracted fields')
            partial = self.path.with_suffix('.part')
            self.s3.download_file(doc['s3']['bucket'], doc['s3']['key'], str(partial), Config=self.transfer)
            if partial.stat().st_size != size or digest(partial) != doc['sha256']:
                partial.unlink(missing_ok=True)
                raise ValueError('Downloaded ZIP checksum mismatch')
            partial.replace(self.path)
        else:
            # Recover a finalized ZIP left by interruption before receipt persistence.
            recovered = False
            for old in self.root.glob('gfswave_global_0p25_*.zip'):
                old.replace(self.path)
                try:
                    self.metadata()
                    recovered = True
                    break
                except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                    self.path.unlink(missing_ok=True)
            if not recovered:
                final = _build_package(self.root, run=self.run, zip_only=True,
                    max_minutes=int(os.getenv('SRP_MAX_MINUTES', '70')))
                final.replace(self.path)
        meta = self.metadata()
        tmp = self.receipt.with_suffix('.part')
        tmp.write_text(json.dumps(meta))
        tmp.replace(self.receipt)
        return meta

    def prepare(self, need_fields=True):
        doc = self.document()
        if doc.get('status') == 'ready' and not need_fields:
            return True
        if doc.get('status') != 'ready':
            self.update(status='building', error=None)
        meta = self.ensure_local(doc)
        published = doc.get('status') == 'ready'
        if not published and self.s3 is not None and self.collection is not None:
            try:
                self.update(status='uploading', **meta)
                self.s3.upload_file(str(self.path), self.bucket, self.key,
                    ExtraArgs={'ContentType': 'application/zip', 'Metadata': {'sha256': meta['sha256']}},
                    Config=self.transfer)
                head = self.s3.head_object(Bucket=self.bucket, Key=self.key)
                if head['ContentLength'] != meta['size_bytes'] or head.get('Metadata', {}).get('sha256') != meta['sha256']:
                    raise ValueError('Uploaded object verification failed')
                self.update(status='ready', ready_at=iso(datetime.now(timezone.utc)), error=None,
                    s3={'bucket': self.bucket, 'key': self.key, 'region': self.region}, **meta)
                published = True
            except Exception as exc:
                self.update(status='failed', error=str(exc)[:1000], **meta)
                print(f'[SRP] publication failed; continuing field extraction: {exc}')
        elif not published:
            self.update(status='local', **meta)
        if need_fields:
            try:
                extract_package(self.path, self.root, self.run, STEPS,
                    Client(max_minutes=70), run_set_dir=self.run_dir)
            except Exception as exc:
                # ingest validates each existing field, and falls back only for invalid/missing fields.
                print(f'[SRP] extraction incomplete, ingest will repair missing fields: {exc}')
        return published

    def cleanup_zip(self):
        if self.document().get('status') == 'ready':
            self.path.unlink(missing_ok=True)
            self.receipt.unlink(missing_ok=True)
