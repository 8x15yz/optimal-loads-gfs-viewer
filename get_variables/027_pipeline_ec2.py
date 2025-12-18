from datetime import datetime, timedelta, timezone
from copernicusmarine import subset
import boto3
import os
import sys
import time
import argparse

# === Mongo (Atlas) ===
from pymongo import MongoClient, ASCENDING

# ---- 사용자 설정 ----
DATASET_ID = "cmems_mod_glo_wav_anfc_0.083deg_PT3H-i"
PRODUCT_CODE = "original"
VARS = {
    "VHM0": {"unit": "m", "name_en": "Significant wave height"},
    "VMDR": {"unit": "degree", "name_en": "Mean wave direction"},
    "VTPK": {"unit": "s", "name_en": "Peak wave period"}
}
BUCKET = "optimal-loads"
S3_PREFIX_ROOT = f"cmems/{PRODUCT_CODE}"        # s3://optimal-loads/cmems/027/<var>/<YYYY>/<MM>/...
REGION = "ap-northeast-2"

# 전구(전체) 다운로드 → bbox 없으면 None 유지
AREA = None  # (min_lon, max_lon, min_lat, max_lat) 예: (120, 150, 20, 45)

UTC = timezone.utc
STEP = timedelta(hours=3)

# ---- MongoDB Atlas 연결 (임시: 직접 문자열) ----
MONGO_URI = "mongodb+srv://8x15yz_db_user:3WprrHmmFJiWcVEr@cluster0.oirpleh.mongodb.net/?appName=Cluster0"
mongo = MongoClient(MONGO_URI)
col = mongo["optimal_loads"]["assets_metadata"]

# 최초 1회만(자동 인덱스)
try:
    col.create_index([("natural_key", ASCENDING)], unique=True)
    col.create_index([("valid_time_utc", ASCENDING)])
except Exception:
    pass

# ---- 유틸 ----
def parse_utc(s: str) -> datetime:
    """'2025-11-03T00:00:00Z' 같은 ISO8601 문자열을 UTC aware datetime으로."""
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

def snap_to_3h_floor(dt: datetime) -> datetime:
    """3시간 그리드(00,03,06,...)로 내림 스냅."""
    h = (dt.hour // 3) * 3
    return dt.replace(hour=h, minute=0, second=0, microsecond=0)

def snap_to_3h_ceil(dt: datetime) -> datetime:
    """3시간 그리드로 올림 스냅(이미 그리드면 그대로)."""
    if dt.minute == dt.second == dt.microsecond == 0 and dt.hour % 3 == 0:
        return dt
    floored = snap_to_3h_floor(dt)
    return floored + STEP

def save_metadata(source: str, dataset_code: str, var: str, unit: str, name_en: str, t: datetime, filename: str, s3_key: str):
    size_bytes = os.path.getsize(filename)
    doc = {
        "source": source,
        "dataset_code": dataset_code,
        "variable": var,
        "unit": unit,
        "name_en": name_en,      # ← 추가
        "valid_time_utc": iso_z(t),
        "year": int(t.strftime("%Y")),
        "month": int(t.strftime("%m")),
        "name": filename,
        "s3": {
            "bucket": BUCKET,
            "region": REGION,
            "key": s3_key
        },
        "resolution": {"lon_deg": 0.083, "lat_deg": 0.083},
        "size_bytes": size_bytes,
        "created_at": iso_z_now(),
        "natural_key": f"{source}|{dataset_code}|{var}|{iso_z(t)}"
    }
    col.update_one({"natural_key": doc["natural_key"]}, {"$setOnInsert": doc}, upsert=True)
    print(f"🧾 metadata inserted: {doc['natural_key']}")

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

    # 3시간 그리드 정렬 (시작=내림, 끝=올림)
    START_UTC = snap_to_3h_floor(start_raw)
    END_UTC   = snap_to_3h_ceil(end_raw)

    if START_UTC != start_raw or END_UTC != end_raw:
        print(f"ℹ️  입력 시간이 3시간 그리드와 달라 스냅되었습니다.")
        print(f"   ▶ START_UTC: {start_raw.isoformat()}  →  {START_UTC.isoformat()}")
        print(f"   ▶ END_UTC  : {end_raw.isoformat()}    →  {END_UTC.isoformat()}")

    s3 = boto3.client("s3", region_name=REGION)

    print(f"▶ RUN (UTC)     : {START_UTC.isoformat()}  →  {END_UTC.isoformat()}  (3-hourly)")
    print(f"▶ S3 prefix     : s3://{BUCKET}/{S3_PREFIX_ROOT}/<var>/<YYYY>/<MM>/")
    print(f"▶ Variables     : " + ", ".join([f"{v} ({u})" for v,u in VARS.items()]))
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
                s3.upload_file(
                    out, BUCKET, s3_key,
                    ExtraArgs={"ContentType": "application/x-netcdf", "ACL": "private"}
                )
                created += 1

                # ✅ 단위 포함 메타데이터 저장
                save_metadata("cmems", PRODUCT_CODE, var, unit, name_en, t, out, s3_key)


                os.remove(out)
            except Exception as e:
                print(f"  ❌ upload failed: {e}")
                failed += 1
                try:
                    os.remove(out)
                except Exception:
                    pass

            time.sleep(0.3)
        t += STEP

    print("")
    print(f"✅ Done. created={created}, skipped={skipped}, failed={failed}")
    if failed > 0:
        sys.exit(2)

if __name__ == "__main__":
    main()
