from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import argparse
import json
import os
import subprocess
import sys

import boto3
from pymongo import ASCENDING, MongoClient

# --------------------- 설정 ---------------------
SOURCE = "ecmwf"
MODEL = "ifs"
TYPE_CODE = "fc"
DATASET_CODE_ORIGINAL = "original"
DATASET_CODE_ALL_STEPS = "original-all-steps"

PARAMS = {
    "10u":  {"unit": "m/s", "name_en": "Eastward velocity vector component at 10 m", "stream": "oper"},
    "10v":  {"unit": "m/s", "name_en": "Northward velocity vector component at 10 m", "stream": "oper"},
    "swh":  {"unit": "m",   "name_en": "Significant height of combined wind waves and swell", "stream": "wave"},
    "mwp":  {"unit": "s",   "name_en": "Mean wave period", "stream": "wave"},
    "mwd":  {"unit": "degree", "name_en": "Mean wave direction", "stream": "wave"},
}

UTC = timezone.utc

# --------------------- S3 설정 ---------------------
BUCKET = os.getenv("S3_BUCKET", "optimal-loads")
REGION = os.getenv("AWS_REGION", "ap-northeast-2")
S3_PREFIX_ROOT = f"ecmwf/{MODEL}/{TYPE_CODE}"

# --------------------- Mongo 설정 ---------------------
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB = os.getenv("MONGO_DB", "optimal_loads")
MONGO_COL = os.getenv("MONGO_COL", "assets_metadata")
DIR_COL_NAME = os.getenv("MONGO_DIR_COL", "directories")

mongo: Optional[MongoClient] = None
col = None
dir_col = None

ROOT_DIR = f"{SOURCE}/{MODEL}/"

if MONGO_URI:
    mongo = MongoClient(MONGO_URI)
    col = mongo[MONGO_DB][MONGO_COL]
    dir_col = mongo[MONGO_DB][DIR_COL_NAME]
    try:
        col.create_index([("natural_key", ASCENDING)], unique=True)
        col.create_index([("valid_time_utc", ASCENDING)])
        col.create_index([("run_time_utc", ASCENDING)])
        col.create_index([("inventory_directory", ASCENDING), ("name", ASCENDING)])
        dir_col.create_index([("parent", ASCENDING), ("name", ASCENDING)])
        dir_col.create_index([("last_modified", ASCENDING)])
    except Exception:
        pass
else:
    print("⚠️ MONGO_URI 환경변수가 비어있어서 Mongo 메타 저장은 비활성화됩니다.")

# --------------------- 유틸 ---------------------
def parse_utc(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)

def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

def utc_now() -> datetime:
    return datetime.now(UTC)

# --------------------- 파일명 / S3 키 생성 ---------------------
def _steps_tag(steps: List[int]) -> str:
    """스텝 범위 태그: steps000-240"""
    lo = min(steps)
    hi = max(steps)
    return f"steps{lo:03d}-{hi:03d}"

def build_filename_all_steps(run_dt: datetime, param: str, steps: List[int]) -> str:
    """
    예) original_swh_20250701_00Z_steps000-240.grib2
    """
    date_str = run_dt.strftime("%Y%m%d")
    run_str  = f"{run_dt:%H}Z"
    return f"original_{param}_{date_str}_{run_str}_{_steps_tag(steps)}.grib2"

def build_s3_key_original(run_dt: datetime, param: str, filename: str) -> str:
    """원본 grib2 파일 S3 키"""
    yyyy = run_dt.strftime("%Y")
    mm   = run_dt.strftime("%m")
    dd   = run_dt.strftime("%d")
    hhZ  = f"{run_dt:%H}Z"
    return f"{S3_PREFIX_ROOT}/{yyyy}/{mm}/{dd}/{hhZ}/{DATASET_CODE_ORIGINAL}/{param}/{filename}"

def build_s3_key_all_steps(run_dt: datetime, param: str, steps: List[int]) -> str:
    """
    all-steps grib2 파일 S3 키
    예) ecmwf/ifs/fc/2025/07/01/00Z/original-all-steps/swh_20250701_00Z_steps000-240.grib2
    """
    yyyy     = run_dt.strftime("%Y")
    mm       = run_dt.strftime("%m")
    dd       = run_dt.strftime("%d")
    hhZ      = f"{run_dt:%H}Z"
    filename = build_filename_all_steps(run_dt, param, steps)
    return f"{S3_PREFIX_ROOT}/{yyyy}/{mm}/{dd}/{hhZ}/{DATASET_CODE_ALL_STEPS}/{filename}"

# --------------------- MongoDB 쿼리 ---------------------
def get_run_steps(run_dt: datetime, param: str) -> List[Dict[str, Any]]:
    if col is None:
        return []
    query = {
        "source": SOURCE,
        "model": MODEL,
        "dataset_code": DATASET_CODE_ORIGINAL,
        "variable": param,
        "run_time_utc": iso_z(run_dt),
    }
    return list(col.find(query).sort("step_hours", ASCENDING))

def check_all_steps_exists(run_dt: datetime, param: str) -> bool:
    if col is None:
        return False
    query = {
        "source": SOURCE,
        "model": MODEL,
        "dataset_code": DATASET_CODE_ALL_STEPS,
        "variable": param,
        "run_time_utc": iso_z(run_dt),
    }
    return col.count_documents(query) > 0

# --------------------- GRIB2 병합 ---------------------
def merge_grib2_files(input_paths: List[Path], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as outfile:
        for fpath in input_paths:
            if not fpath.exists():
                raise FileNotFoundError(f"Missing file: {fpath}")
            with open(fpath, "rb") as infile:
                outfile.write(infile.read())

# --------------------- S3 다운로드/업로드 ---------------------
def download_from_s3(s3_client, s3_key: str, local_path: Path) -> bool:
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        s3_client.download_file(BUCKET, s3_key, str(local_path))
        return True
    except Exception as e:
        print(f"  ❌ S3 download failed: {s3_key} - {e}")
        return False

def upload_to_s3(s3_client, local_path: Path, s3_key: str) -> bool:
    try:
        s3_client.upload_file(
            str(local_path),
            BUCKET,
            s3_key,
            ExtraArgs={"ContentType": "application/x-grib"},
        )
        return True
    except Exception as e:
        print(f"  ❌ S3 upload failed: {s3_key} - {e}")
        return False

# --------------------- directories 유틸 ---------------------
def _norm_dir(d: str) -> str:
    d = (d or "").strip().lstrip("/")
    if d and not d.endswith("/"):
        d += "/"
    return d

def _split_dir(d: str) -> tuple[Optional[str], str]:
    d = _norm_dir(d)
    if not d:
        return None, ""
    parts = [p for p in d.strip("/").split("/") if p]
    if not parts:
        return None, ""
    name = parts[-1]
    if len(parts) == 1:
        return None, name
    parent = "/".join(parts[:-1]) + "/"
    return parent, name

def upsert_directories_from_doc(doc: Dict[str, Any]) -> None:
    if dir_col is None:
        return
    inv_dir = _norm_dir(doc.get("inventory_directory", ""))
    if not inv_dir or not inv_dir.startswith(ROOT_DIR):
        return
    lm = doc.get("created_at")
    if not isinstance(lm, datetime):
        try:
            lm = parse_utc(str(lm))
        except Exception:
            lm = utc_now()
    now = utc_now()
    cur = inv_dir
    while cur and cur.startswith(ROOT_DIR):
        parent, name = _split_dir(cur)
        dir_col.update_one(
            {"_id": cur},
            {
                "$setOnInsert": {"dir": cur, "parent": parent, "name": name, "children_dirs": [], "created_at": now},
                "$max": {"last_modified": lm},
                "$set": {"updated_at": now},
            },
            upsert=True,
        )
        if parent and parent.startswith(ROOT_DIR):
            p_parent, p_name = _split_dir(parent)
            dir_col.update_one(
                {"_id": parent},
                {
                    "$setOnInsert": {"dir": parent, "parent": p_parent, "name": p_name, "children_dirs": [], "created_at": now},
                    "$max": {"last_modified": lm},
                    "$set": {"updated_at": now},
                },
                upsert=True,
            )
            dir_col.update_one(
                {"_id": parent},
                {"$addToSet": {"children_dirs": name + "/"}, "$max": {"last_modified": lm}, "$set": {"updated_at": now}},
            )
        cur = parent

# --------------------- 메타데이터 생성 ---------------------
def build_all_steps_metadata(
    run_dt: datetime,
    param: str,
    stream: str,
    unit: str,
    name_en: str,
    steps: List[int],
    s3_key: str,
    file_size: int,
) -> Dict[str, Any]:
    filename = build_filename_all_steps(run_dt, param, steps)  # ← 통일된 파일명

    natural_key = (
        f"{DATASET_CODE_ALL_STEPS}|{SOURCE}|{MODEL}|forecast|{stream}|{param}"
        f"|run={iso_z(run_dt)}"
    )

    return {
        "source": SOURCE,
        "dataset_code": DATASET_CODE_ALL_STEPS,
        "variable": param,
        "name_en": name_en,
        "unit": unit,
        "model": MODEL,
        "type": "forecast",
        "stream": stream,
        "resolution": {"lon_deg": 0.25, "lat_deg": 0.25},

        "run_time_utc": iso_z(run_dt),
        "run_date": run_dt.strftime("%Y%m%d"),   # ← 추가
        "run_hour": run_dt.hour,                  # ← 추가
        "step_range": [min(steps), max(steps)],
        "step_count": len(steps),
        "steps_included": sorted(steps),

        "year":  int(run_dt.strftime("%Y")),
        "month": int(run_dt.strftime("%m")),

        "name": filename,                         # ← 파일명 통일
        "format": "grib2",
        "content_type": "application/x-grib",
        "size_bytes": file_size,

        "created_at": utc_now(),
        "natural_key": natural_key,

        "s3": {"bucket": BUCKET, "region": REGION, "key": s3_key},

        "inventory_directory": (
            f"{SOURCE}/{MODEL}/{run_dt:%Y/%Y-%m/%Y-%m-%d/%H}Z/{DATASET_CODE_ALL_STEPS}/"
        ),
        "inventory_name": (
            f"{DATASET_CODE_ALL_STEPS}_{param}_{run_dt:%Y%m%d}_{run_dt:%H}Z"
            f"_{_steps_tag(steps)}"                # ← 스텝 범위 포함
        ),
    }

def upsert_mongo(doc: dict) -> None:
    if col is None:
        return
    nk = doc["natural_key"]
    col.update_one({"natural_key": nk}, {"$set": doc}, upsert=True)
    print(f"🧾 mongo upsert: {nk}")
    upsert_directories_from_doc(doc)

# --------------------- 메인 로직 ---------------------
def process_run(
    s3_client,
    run_dt: datetime,
    param: str,
    work_dir: Path,
    skip_existing: bool = True,
) -> bool:
    meta    = PARAMS[param]
    stream  = meta["stream"]
    unit    = meta["unit"]
    name_en = meta["name_en"]

    print(f"\n▶ Processing: {iso_z(run_dt)} | {param} ({stream})")

    if skip_existing and check_all_steps_exists(run_dt, param):
        print(f"  ⏭️  all-steps already exists in MongoDB, skipping")
        return True

    docs = get_run_steps(run_dt, param)
    if not docs:
        print(f"  ⚠️  No original steps found in MongoDB")
        return False

    steps = [doc["step_hours"] for doc in docs]
    print(f"  📊 Found {len(steps)} steps: {min(steps)} ~ {max(steps)}")

    # S3에서 원본 grib2 다운로드
    download_dir = work_dir / "downloads" / iso_z(run_dt).replace(":", "-") / param
    download_dir.mkdir(parents=True, exist_ok=True)

    local_files   = []
    missing_files = []

    for doc in docs:
        s3_key   = (doc.get("s3") or {}).get("key")
        if not s3_key:
            print(f"  ⚠️  Missing S3 key for step {doc['step_hours']}")
            missing_files.append(doc["step_hours"])
            continue

        local_path = download_dir / Path(s3_key).name
        if local_path.exists():
            print(f"  ✓ cached: step {doc['step_hours']}")
        else:
            print(f"  ⏬ downloading: step {doc['step_hours']}")
            if not download_from_s3(s3_client, s3_key, local_path):
                missing_files.append(doc["step_hours"])
                continue
        local_files.append(local_path)

    if missing_files:
        print(f"  ❌ Missing {len(missing_files)} files: {missing_files}")
        return False

    # GRIB2 병합 — 파일명에 날짜·런·스텝 범위 반영
    filename   = build_filename_all_steps(run_dt, param, steps)
    output_dir = work_dir / "merged" / iso_z(run_dt).replace(":", "-")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / filename

    print(f"  🔗 Merging {len(local_files)} files → {filename}")
    try:
        merge_grib2_files(local_files, output_file)
    except Exception as e:
        print(f"  ❌ Merge failed: {e}")
        return False

    file_size = output_file.stat().st_size
    print(f"  ✓ Merged file size: {file_size:,} bytes ({file_size / 1024 / 1024:.2f} MB)")

    # S3 업로드
    s3_key = build_s3_key_all_steps(run_dt, param, steps)  # ← steps 전달
    print(f"  📤 Uploading to s3://{BUCKET}/{s3_key}")
    if not upload_to_s3(s3_client, output_file, s3_key):
        return False

    # 메타데이터 저장
    metadata = build_all_steps_metadata(
        run_dt=run_dt, param=param, stream=stream,
        unit=unit, name_en=name_en, steps=steps,
        s3_key=s3_key, file_size=file_size,
    )
    upsert_mongo(metadata)

    print(f"  ✅ Success: {filename}")
    return True

# --------------------- 런 리스트 생성 ---------------------
def build_run_list(start_utc: str, end_utc: str, run_hours: set[int]) -> List[datetime]:
    start_dt = parse_utc(start_utc)
    end_dt   = parse_utc(end_utc)
    runs, t  = [], start_dt
    while t <= end_dt:
        if t.hour in run_hours:
            runs.append(t)
        t += timedelta(hours=6)
    return runs

def parse_run_hours(s: str) -> set[int]:
    return {int(x.strip()) for x in s.split(",") if x.strip()}

# --------------------- 메인 ---------------------
def main():
    ap = argparse.ArgumentParser(description="ECMWF all-steps GRIB2 생성")
    ap.add_argument("--start_utc",      required=True)
    ap.add_argument("--end_utc",        required=True)
    ap.add_argument("--run_hours",      default="0,6,12,18")
    ap.add_argument("--params",         default="10u,10v,swh,mwp,mwd")
    ap.add_argument("--work_dir",       default="./work_all_steps")
    ap.add_argument("--skip_existing",  action="store_true")
    args = ap.parse_args()

    if not MONGO_URI:
        print("❌ MONGO_URI 환경변수가 필요합니다")
        sys.exit(1)

    run_hours = parse_run_hours(args.run_hours)
    params    = [p.strip() for p in args.params.split(",") if p.strip()]

    for p in params:
        if p not in PARAMS:
            print(f"❌ Unknown parameter: {p}")
            sys.exit(1)

    run_list = build_run_list(args.start_utc, args.end_utc, run_hours)
    if not run_list:
        print("⚠️ 실행할 RUN이 0개입니다")
        return

    print(f"📅 Period: {args.start_utc} ~ {args.end_utc}")
    print(f"⏰ Run hours: {sorted(run_hours)}")
    print(f"📊 Total runs: {len(run_list)}")
    print(f"🔧 Parameters: {params}")
    print(f"📁 Work dir: {args.work_dir}")
    print(f"⏭️  Skip existing: {args.skip_existing}\n")

    s3       = boto3.client("s3", region_name=REGION)
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    total = len(run_list) * len(params)
    success = failed = 0

    for run_dt in run_list:
        for param in params:
            try:
                if process_run(s3, run_dt, param, work_dir, args.skip_existing):
                    success += 1
                else:
                    failed += 1
            except Exception as e:
                import traceback
                print(f"  ❌ Unexpected error: {e}")
                traceback.print_exc()
                failed += 1

    print("\n" + "=" * 60)
    print(f"✅ SUMMARY  total={total}  success={success}  failed={failed}")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)

if __name__ == "__main__":
    main()