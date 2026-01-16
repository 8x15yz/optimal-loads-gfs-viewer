from datetime import datetime, timedelta, timezone
from ecmwf.opendata import Client
import boto3
import os
import sys
import time
import argparse

# === Mongo (Atlas) ===
from pymongo import MongoClient, ASCENDING


# --------------------- 사용자 설정 ---------------------
SOURCE = "ecmwf"
MODEL  = "aifs-single"
RESOL  = "0p25"

PRODUCT_CODE = "original"
BUCKET = "optimal-loads"
REGION = "ap-northeast-2"
S3_PREFIX_ROOT = f"ecmwf/{PRODUCT_CODE}"

# 다운받을 파라미터 목록
PARAMS = {
    "10u": {"unit": "m/s", "name_en": "10 metre U wind component"},
    "10v": {"unit": "m/s", "name_en": "10 metre V wind component"},
    "2t":  {"unit": "K",   "name_en": "2 metre temperature"},
}

# 0z/12z 등 런 시각에서 몇 시간까지 받을지
DEFAULT_MAX_STEP = 48  # +48h 예측까지


# --------------- MongoDB 설정 ----------------
MONGO_URI = "mongodb+srv://8x15yz_db_user:3WprrHmmFJiWcVEr@cluster0.oirpleh.mongodb.net/?appName=Cluster0"
mongo = MongoClient(MONGO_URI)
col = mongo["optimal_loads"]["assets_metadata"]

try:
    col.create_index([("natural_key", ASCENDING)], unique=True)
    col.create_index([("valid_time_utc", ASCENDING)])
except Exception:
    pass


UTC = timezone.utc


# --------------------- 공통 유틸 ---------------------
def parse_utc(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_z_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------- 메타데이터 저장 ---------------------
def save_metadata(source, dataset_code, param, unit, name_en, valid_time, filename, s3_key):
    size_bytes = os.path.getsize(filename)
    doc = {
        "source": source,
        "dataset_code": dataset_code,
        "variable": param,
        "unit": unit,
        "name_en": name_en,
        "valid_time_utc": iso_z(valid_time),
        "year": int(valid_time.strftime("%Y")),
        "month": int(valid_time.strftime("%m")),
        "name": filename,
        "s3": {
            "bucket": BUCKET,
            "region": REGION,
            "key": s3_key
        },
        "resolution": {"lon_deg": 0.25, "lat_deg": 0.25},
        "size_bytes": size_bytes,
        "created_at": iso_z_now(),
        "natural_key": f"{source}|{dataset_code}|{param}|{iso_z(valid_time)}"
    }

    col.update_one({"natural_key": doc["natural_key"]}, {"$setOnInsert": doc}, upsert=True)
    print(f"🧾 metadata inserted: {doc['natural_key']}")


# --------------------- 메인 파이프라인 ---------------------
def main():
    ap = argparse.ArgumentParser(description="ECMWF AIFS Single → S3 → Mongo pipeline")
    ap.add_argument("RUN_UTC", help="런 시각 예: 2025-11-30T00:00:00Z (00z run)")
    ap.add_argument("--max_step", type=int, default=DEFAULT_MAX_STEP,
                    help="예측 step 범위 (기본: 48시간)")
    args = ap.parse_args()

    run_dt = parse_utc(args.RUN_UTC)

    print(f"▶ ECMWF RUN: {run_dt.isoformat()}")
    print(f"▶ Steps     : 0 → +{args.max_step}")
    print("▶ Params    :", ", ".join(PARAMS.keys()))
    print("")

    # ECMWF Client
    client = Client(source="ecmwf", model=MODEL, resol=RESOL)

    s3 = boto3.client("s3", region_name=REGION)

    created = skipped = failed = 0

    for param, meta in PARAMS.items():
        unit = meta["unit"]
        name_en = meta["name_en"]

        for step in range(0, args.max_step + 1):

            valid_time = run_dt + timedelta(hours=step)

            yyyy = valid_time.strftime("%Y")
            mm   = valid_time.strftime("%m")

            out = f"{PRODUCT_CODE}_{param}_{run_dt:%Y%m%d_%H}Z_step{step:03}.grib2"
            s3_key = f"{S3_PREFIX_ROOT}/{param}/{yyyy}/{mm}/{out}"

            if os.path.exists(out):
                print(f"⏭️ exists locally: {out}")
                skipped += 1
            else:
                print(f"⏬ retrieve param={param} step={step} → {out}")
                try:
                    client.retrieve(
                        type="fc",
                        stream="oper",
                        time=run_dt.hour,   # 0 → 00z, 12 → 12z
                        step=step,
                        param=param,
                        target=out,
                    )
                except Exception as e:
                    print(f"  ❌ retrieve failed: {e}")
                    failed += 1
                    continue

            # ------------ S3 업로드 ------------
            try:
                print(f"  📤 upload → s3://{BUCKET}/{s3_key}")
                s3.upload_file(
                    out, BUCKET, s3_key,
                    ExtraArgs={"ContentType": "application/x-grib2", "ACL": "private"}
                )
                created += 1

                # 메타데이터 저장
                save_metadata(SOURCE, PRODUCT_CODE, param, unit, name_en,
                              valid_time, out, s3_key)

                os.remove(out)
            except Exception as e:
                print(f"  ❌ upload failed: {e}")
                failed += 1
                try: os.remove(out)
                except: pass

            time.sleep(0.2)

    print("")
    print(f"✅ Done. created={created}, skipped={skipped}, failed={failed}")
    if failed > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
