from datetime import datetime, timedelta, timezone
from copernicusmarine import subset
import boto3
from boto3.s3.transfer import TransferConfig
import os, sys, time, argparse, tempfile
import xarray as xr

# === Mongo (Atlas) ===
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError

# ---- 사용자 설정 ----
DATASET_ID = "cmems_obs-wind_glo_phy_nrt_l4_0.125deg_PT1H"  # 제품은 파랑이지만 예시 그대로 둠
PRODUCT_CODE = "original"
VARS = {
    "eastward_wind":  {"unit": "m/s", "name_en": "10 m eastward wind (U)"},
    "northward_wind": {"unit": "m/s", "name_en": "10 m northward wind (V)"},
}
BUCKET = "optimal-loads"
S3_PREFIX_ROOT = f"cmems/{PRODUCT_CODE}"  # s3://optimal-loads/cmems/027/<var>/<YYYY>/<MM>/
REGION = "ap-northeast-2"

# 전구(전체) 다운로드 → bbox 없으면 None 유지
# AREA = (min_lon, max_lon, min_lat, max_lat)
AREA = None

UTC = timezone.utc
STEP = timedelta(hours=1)

# ---- 환경변수 (권장) ----
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://8x15yz_db_user:3WprrHmmFJiWcVEr@cluster0.oirpleh.mongodb.net/?appName=Cluster0")
MONGO_DB = os.getenv("MONGO_DB", "optimal_loads")
MONGO_COLL = os.getenv("MONGO_COLL", "assets_metadata")

# ---- MongoDB ----
mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
col = mongo[MONGO_DB][MONGO_COLL]
try:
    col.create_index([("natural_key", ASCENDING)], unique=True)
    col.create_index([("valid_time_utc", ASCENDING)])
except Exception:
    pass

# ---- 유틸 ----
def parse_utc(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)

def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

def iso_z_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def snap_to_1h_floor(dt):
    return dt.replace(minute=0, second=0, microsecond=0)

def snap_to_1h_ceil(dt):
    if dt.minute == dt.second == dt.microsecond == 0:
        return dt
    return (dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))


def save_metadata(source: str, dataset_code: str, var: str, unit: str, name_en: str,
                  t: datetime, filename: str, s3_key: str):
    size_bytes = os.path.getsize(filename)
    doc = {
        "source": source,
        "dataset_code": dataset_code,
        "variable": var,
        "unit": unit,
        "name_en": name_en,
        "valid_time_utc": iso_z(t),
        "year": int(t.strftime("%Y")),
        "month": int(t.strftime("%m")),
        "name": os.path.basename(filename),
        "s3": {
            "bucket": BUCKET,
            "region": REGION,
            "key": s3_key,
            "acl": "private",
            "sse": "AES256"
        },
        # ★ 이 제품은 0.125°
        "resolution": {"lon_deg": 0.125, "lat_deg": 0.125},
        "size_bytes": size_bytes,
        "created_at": iso_z_now(),
        "natural_key": f"{source}|{dataset_code}|{var}|{iso_z(t)}"
    }
    try:
        col.update_one({"natural_key": doc["natural_key"]}, {"$setOnInsert": doc}, upsert=True)
        print(f"🧾 metadata upsert: {doc['natural_key']}")
    except DuplicateKeyError:
        print(f"🧾 metadata exists: {doc['natural_key']}")

def save_computed_metadata(source: str, t: datetime):
    ts = iso_z(t)
    base = {
        "source": source,
        "dataset_code": "computed",
        "valid_time_utc": ts,
        "year": int(t.strftime("%Y")),
        "month": int(t.strftime("%m")),
        "resolution": {"lon_deg": 0.125, "lat_deg": 0.125},
        "s3": None,
        "size_bytes": None,
        "created_at": iso_z_now(),
        "computed_from": {
            "variables": ["eastward_wind", "northward_wind"],
            "dataset_codes": ["original"],
            "mode": "on_the_fly"
        }
    }

    docs = [
        {
            **base,
            "variable": "wind_speed",
            "unit": "m s-1",
            "name_en": "10 m wind speed",
            "name": "generated-on-request",
            "aliases": ["wind"],
            "natural_key": f"{source}|computed|wind_speed|{ts}",
        },
        {
            **base,
            "variable": "wind_dir",
            "unit": "degree",
            "name_en": "10 m wind direction (from)",
            "name": "generated-on-request",
            "aliases": ["wdir"],
            "natural_key": f"{source}|computed|wind_dir|{ts}",
        },
    ]

    for d in docs:
        col.update_one({"natural_key": d["natural_key"]}, {"$setOnInsert": d}, upsert=True)
        print(f"🧾 computed metadata upsert: {d['natural_key']}")



def main():
    ap = argparse.ArgumentParser(description="CMEMS waves subset → S3 (사용자 지정 시/종 UTC)")
    ap.add_argument("START_UTC", help="예: 2025-11-03T00:00:00Z")
    ap.add_argument("END_UTC",   help="예: 2025-11-06T12:00:00Z (포함 범위)")
    ap.add_argument("--compress", type=int, default=None, help="NetCDF 압축레벨 1~9 (선택)")
    args = ap.parse_args()

    start_raw = parse_utc(args.START_UTC)
    end_raw   = parse_utc(args.END_UTC)
    if start_raw > end_raw:
        print("❌ START_UTC 가 END_UTC 보다 큽니다.")
        sys.exit(2)

    # 1시간 그리드 정렬 (시작=내림, 끝=올림)
    START_UTC = snap_to_1h_floor(start_raw)
    END_UTC   = snap_to_1h_ceil(end_raw)
    if START_UTC != start_raw or END_UTC != end_raw:
        print(f"ℹ️ 스냅 적용")
        print(f"   ▶ START_UTC: {start_raw.isoformat()}  →  {START_UTC.isoformat()}")
        print(f"   ▶ END_UTC  : {end_raw.isoformat()}    →  {END_UTC.isoformat()}")

    # S3 클라이언트 및 설정
    s3 = boto3.client("s3", region_name=REGION)
    tcfg = TransferConfig(multipart_threshold=32 * 1024 * 1024,  # 32MB 이상 멀티파트
                          max_concurrency=4,
                          multipart_chunksize=8 * 1024 * 1024,   # 8MB 청크
                          use_threads=True)

    print(f"▶ RUN (UTC)     : {START_UTC.isoformat()}  →  {END_UTC.isoformat()}  (hourly)")
    print(f"▶ S3 prefix     : s3://{BUCKET}/{S3_PREFIX_ROOT}/<var>/<YYYY>/<MM>/")
    print("▶ Variables     : " + ", ".join([f"{k} ({v['unit']})" for k, v in VARS.items()]))
    if AREA:
        print(f"▶ BBOX          : {AREA}")
    print("")

    # 다운로드 루프
    t = START_UTC
    created = skipped = failed = 0

    while t <= END_UTC:
        for var, meta in VARS.items():
            unit = meta["unit"]
            name_en = meta["name_en"]
            out = f"{PRODUCT_CODE}_{var}_{t:%Y%m%d_%H}Z.nc"
            yyyy = f"{t:%Y}"
            mm   = f"{t:%m}"
            s3_key = f"{S3_PREFIX_ROOT}/{var}/{yyyy}/{mm}/{out}"

            if os.path.exists(out):
                print(f"⏭️  exists, skip local: {out}")
                skipped += 1
            else:
                print(f"⏬ subset  var={var}  time={t.isoformat()}  → {out}")
                kwargs = {
                    "dataset_id": DATASET_ID,
                    "variables": [var],
                    "start_datetime": iso_z(t),
                    "end_datetime": iso_z(t),
                    "output_filename": out,
                }
                if args.compress:
                    kwargs["netcdf_compression_level"] = int(args.compress)
                if AREA:
                    kwargs.update({
                        "minimum_longitude": AREA[0],
                        "maximum_longitude": AREA[1],
                        "minimum_latitude": AREA[2],
                        "maximum_latitude": AREA[3],
                    })

                try:
                    subset(**kwargs)
                except Exception as e:
                    print(f"  ❌ subset failed: {e}")
                    failed += 1
                    continue

            # 업로드 및 메타데이터 저장
            try:
                print(f"  📤 upload  s3://{BUCKET}/{s3_key}")
                extra = {
                    "ContentType": "application/x-netcdf",
                    "ACL": "private",
                    "ServerSideEncryption": "AES256"
                }
                s3.upload_file(out, BUCKET, s3_key, ExtraArgs=extra, Config=tcfg)
                created += 1

                # 메타데이터 저장
                save_metadata("cmems", PRODUCT_CODE, var, unit, name_en, t, out, s3_key)
                # ✅ computed 메타도 같이 등록 (중복 방지 upsert)
                save_computed_metadata("cmems", t)  

            except Exception as e:
                print(f"  ❌ upload failed: {e}")
                failed += 1
            finally:
                try:
                    if os.path.exists(out):
                        os.remove(out)
                except Exception:
                    pass

            time.sleep(0.2)
        t += STEP

    print("")
    print(f"✅ Done. created={created}, skipped={skipped}, failed={failed}")
    if failed > 0:
        sys.exit(2)

if __name__ == "__main__":
    main()
