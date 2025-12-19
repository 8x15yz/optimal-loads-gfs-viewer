from __future__ import annotations

from datetime import datetime, timedelta, timezone
from ecmwf.opendata import Client
import boto3
from pymongo import MongoClient, ASCENDING
import os
import sys
import time
import argparse
import json
from pathlib import Path

# --------------------- 사용자 설정 ---------------------
SOURCE_REAL = "aws"
SOURCE = "ecmwf"
MODEL  = "ifs"
RESOL  = "0p25"

PRODUCT_CODE = "original"

# 다운로드 파라미터(ECMWF 내부 코드)
TYPE_CODE = "fc"  # forecast (ECMWF request value)

# ✅ 메타데이터에는 사람이 읽기 쉬운 값으로
ASSET_TYPE = "forecast"  # (raw/forecast 데이터용)

# ✅ 변수별로 stream 지정
PARAMS = {
    "10u":  {"unit": "m/s", "name_en": "Eastward velocity vector component at 10 m", "stream": "oper", "product_code": "original"},
    "10v":  {"unit": "m/s", "name_en": "Northward velocity vector component at 10 m", "stream": "oper", "product_code": "original"},
    "swh":  {"unit": "m",   "name_en": "Significant height of combined wind waves and swell", "stream": "wave", "product_code": "original"},
    "pp1d": {"unit": "s",   "name_en": "Peak wave period", "stream": "wave", "product_code": "original"},
    "mwp":  {"unit": "s",   "name_en": "Mean wave period", "stream": "wave", "product_code": "original"},
}

# ✅ 파생 변수(바람) 메타데이터 "미리" 생성용(파일 없이도 메타 생성)
DERIVED_VARS = {
    "wind_speed_10m": {
        "unit": "m/s",
        "name_en": "10 metre wind speed",
        "depends_on": ["10u", "10v"],
        "method": "sqrt(u^2 + v^2)",
        "product_code": "computed",
    },
    "wind_dir_10m": {
        "unit": "degree",
        "name_en": "10 metre wind direction (meteorological)",
        "depends_on": ["10u", "10v"],
        "method": "wind_dir_deg = (atan2(-u, -v) * 180/pi + 360) % 360",
        "convention": "meteorological_from",
        "product_code": "computed",
    },
}

DEFAULT_MAX_STEP = 360
UTC = timezone.utc

SCRIPT_DIR = Path(__file__).resolve().parent

# ✅ 저장 루트(원본 폴더 구조 유지)
DATA_ROOT = SCRIPT_DIR / "ecmwf" / MODEL / TYPE_CODE

# --------------------- S3 설정 ---------------------
# 예: s3://optimal-loads/ecmwf/original/ifs/fc/YYYY/MM/DD/00Z/oper/10u/file.grib2
BUCKET = os.getenv("S3_BUCKET", "optimal-loads")
REGION = os.getenv("AWS_REGION", "ap-northeast-2")
S3_PREFIX_ROOT = f"ecmwf/{MODEL}/{TYPE_CODE}"

# 로컬 파일 업로드 후 지울지
DELETE_LOCAL_AFTER_UPLOAD = True

# --------------------- Mongo 설정 ---------------------
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB  = os.getenv("MONGO_DB", "optimal_loads")
MONGO_COL = os.getenv("MONGO_COL", "assets_metadata")

mongo = None
col = None
if MONGO_URI:
    mongo = MongoClient(MONGO_URI)
    col = mongo[MONGO_DB][MONGO_COL]
    try:
        col.create_index([("natural_key", ASCENDING)], unique=True)
        col.create_index([("valid_time_utc", ASCENDING)])
        col.create_index([("run_time_utc", ASCENDING)])
        col.create_index([("valid_key", ASCENDING)])
    except Exception:
        pass
else:
    print("⚠️ MONGO_URI 환경변수가 비어있어서 Mongo 메타 저장은 비활성화됩니다.")

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

# --------------------- step 스케줄 생성 (IFS 규칙) ---------------------
def build_ifs_steps(run_hour: int, max_step: int) -> list[int]:
    """
    IFS Open Data 규칙:
    - 00Z/12Z: 0~144 by 3, 150~360 by 6
    - 06Z/18Z: 0~144 by 3
    """
    steps: list[int] = []

    if run_hour in (0, 12):
        s1_end = min(144, max_step)
        steps.extend(range(0, s1_end + 1, 3))
        if max_step >= 150:
            s2_end = min(360, max_step)
            steps.extend(range(150, s2_end + 1, 6))

    elif run_hour in (6, 18):
        s1_end = min(144, max_step)
        steps.extend(range(0, s1_end + 1, 3))
    else:
        steps.extend(range(0, max_step + 1, 3))

    return sorted(set(steps))

# --------------------- ✅ 폴더 구조 생성 (원본 유지) ---------------------
def get_run_set_dir(run_dt: datetime) -> Path:
    yyyy = run_dt.strftime("%Y")
    mm   = run_dt.strftime("%m")
    dd   = run_dt.strftime("%d")
    hhZ  = f"{run_dt:%H}Z"
    return DATA_ROOT / yyyy / mm / dd / hhZ

def get_out_path(run_set_dir: Path, stream: str, param: str, filename: str) -> Path:
    return run_set_dir / stream / param / filename

def ensure_dirs(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)

# --------------------- S3 업로드 ---------------------
def build_s3_key(run_dt: datetime, product_code: str, param: str, filename: str) -> str:
    yyyy = run_dt.strftime("%Y")
    mm   = run_dt.strftime("%m")
    dd   = run_dt.strftime("%d")
    hhZ  = f"{run_dt:%H}Z"
    return f"{S3_PREFIX_ROOT}/{yyyy}/{mm}/{dd}/{hhZ}/{product_code}/{param}/{filename}"

def upload_to_s3(s3_client, local_path: Path, s3_key: str) -> None:
    s3_client.upload_file(
        str(local_path),
        BUCKET,
        s3_key,
        ExtraArgs={"ContentType": "application/x-grib"},
    )

# --------------------- Mongo 메타 키 ---------------------
def build_keys(
    source: str,
    dataset_code: str,
    model: str,
    asset_type: str,
    stream: str,
    param: str,
    run_time: datetime,
    step: int,
    valid_time: datetime,
) -> tuple[str, str]:
    natural_key = (
        f"{dataset_code}|{source}|{model}|{asset_type}|{stream}|{param}"
        f"|run={iso_z(run_time)}|step={step}"
    )
    valid_key = (
        f"{dataset_code}|{source}|{model}|{asset_type}|{stream}|{param}"
        f"|valid={iso_z(valid_time)}"
    )
    return natural_key, valid_key

# --------------------- Mongo 메타 upsert: raw/forecast ---------------------
def upsert_metadata_mongo(
    source: str,
    dataset_code: str,
    model: str,
    resol: str,
    asset_type: str,
    type_code: str,
    stream: str,
    param: str,
    unit: str,
    name_en: str,
    run_time: datetime,
    step: int,
    valid_time: datetime,
    filename: str,
    local_path: Path,
    s3_key: str | None,
):
    if col is None:
        return

    size_bytes = local_path.stat().st_size
    natural_key, valid_key = build_keys(
        source=source,
        dataset_code=dataset_code,
        model=model,
        asset_type=asset_type,
        stream=stream,
        param=param,
        run_time=run_time,
        step=step,
        valid_time=valid_time,
    )

    doc = {
        "source": source,
        "dataset_code": dataset_code,

        "variable": param,
        "name_en": name_en,
        "unit": unit,

        "model": model,
        "type": asset_type,  # "forecast"
        "stream": stream,

        "resolution": {"lon_deg": 0.25, "lat_deg": 0.25},

        "run_time_utc": iso_z(run_time),
        "step_hours": step,
        "valid_time_utc": iso_z(valid_time),

        "year": int(valid_time.strftime("%Y")),
        "month": int(valid_time.strftime("%m")),

        "name": filename,
        "format": "grib2",
        "content_type": "application/x-grib",
        "size_bytes": size_bytes,

        "created_at": iso_z_now(),

        "natural_key": natural_key,
        "valid_key": valid_key,

        "source_parameters": {
            "ecmwf": {
                "type": type_code,  # "fc"
                "stream": stream,
                "time": f"{run_time:%H}",
                "step": step,
                "param": param,
                "resol": resol,
            }
        },
        
        "inventory_directory": f'{source}/{model}/{run_time:%Y/%Y-%m/%Y-%m-%d/%H}Z/{dataset_code}/{param}/',
        "inventory_name": f'{dataset_code}_{param}_{run_time:%Y%m%d_%H}Z_step{step:03}',
    }

    if s3_key:
        doc["s3"] = {"bucket": BUCKET, "region": REGION, "key": s3_key}

    col.update_one({"natural_key": natural_key}, {"$setOnInsert": doc}, upsert=True)
    print(f"🧾 mongo upsert: {natural_key}")

# --------------------- Mongo 메타 upsert: derived(planned) ---------------------
def upsert_derived_metadata_mongo(
    source: str,
    dataset_code: str,
    model: str,
    resol: str,
    type_code: str,
    stream: str,            # 보통 "oper"
    derived_var: str,       # wind_speed_10m 등
    derived_meta: dict,     # DERIVED_VARS[...]
    run_time: datetime,
    step: int,
    valid_time: datetime,
):
    if col is None:
        return

    asset_type = "derived"

    natural_key, valid_key = build_keys(
        source=source,
        dataset_code=dataset_code,
        model=model,
        asset_type=asset_type,
        stream=stream,
        param=derived_var,
        run_time=run_time,
        step=step,
        valid_time=valid_time,
    )

    doc = {
        "source": source,
        "dataset_code": dataset_code,

        "variable": derived_var,
        "name_en": derived_meta.get("name_en", derived_var),
        "unit": derived_meta.get("unit", ""),

        "model": model,
        "type": asset_type,  # "derived"
        "stream": stream,

        "resolution": {"lon_deg": 0.25, "lat_deg": 0.25},

        "run_time_utc": iso_z(run_time),
        "step_hours": step,
        "valid_time_utc": iso_z(valid_time),

        "year": int(valid_time.strftime("%Y")),
        "month": int(valid_time.strftime("%m")),

        "created_at": iso_z_now(),

        "natural_key": natural_key,
        "valid_key": valid_key,

        "derivation": {
            "depends_on": derived_meta.get("depends_on", []),
            "method": derived_meta.get("method", ""),
        },

        "source_parameters": {
            "ecmwf": {
                "type": type_code,
                "stream": stream,
                "time": f"{run_time:%H}",
                "step": step,
                "resol": resol,
            }
        },

        "status": "planned",

        "inventory_directory": f'{source}/{model}/{run_time:%Y/%Y-%m/%Y-%m-%d/%H}Z/{dataset_code}/{derived_var}/',
        "inventory_name": f'{dataset_code}_{derived_var}_{run_time:%Y%m%d_%H}Z_step{step:03}',
    }

    conv = derived_meta.get("convention")
    if conv:
        doc["derivation"]["convention"] = conv

    col.update_one({"natural_key": natural_key}, {"$setOnInsert": doc}, upsert=True)
    print(f"🧾 mongo upsert (derived): {natural_key}")

# --------------------- jsonl 메타 로그 ---------------------
def append_metadata_to_jsonl(metadata_log_path: Path, doc: dict):
    with open(metadata_log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False) + "\n")

def main():
    ap = argparse.ArgumentParser(
        description="ECMWF IFS → run-based folder structure + GRIB2 + S3 + Mongo metadata (+ derived planned metadata)"
    )
    ap.add_argument("RUN_UTC", help="런 시각 예: 2025-12-16T00:00:00Z (15일치면 00Z/12Z 권장)")
    ap.add_argument("--max_step", type=int, default=DEFAULT_MAX_STEP, help="예측 step 최대 (기본: 360)")
    ap.add_argument("--sleep", type=float, default=0.2, help="요청 간 sleep(초)")
    ap.add_argument("--no_s3", action="store_true", help="S3 업로드 비활성화")
    ap.add_argument("--no_mongo", action="store_true", help="Mongo 메타 저장 비활성화")
    ap.add_argument("--no_jsonl", action="store_true", help="jsonl 메타 로그 비활성화")
    args = ap.parse_args()

    run_dt = parse_utc(args.RUN_UTC)
    steps = build_ifs_steps(run_dt.hour, args.max_step)

    run_set_dir = get_run_set_dir(run_dt)
    ensure_dirs(run_set_dir)

    metadata_log_path = run_set_dir / "metadata_log.jsonl"

    print(f"▶ ECMWF RUN : {run_dt.isoformat()}")
    print(f"▶ run_hour  : {run_dt.hour}Z")
    print(f"▶ max_step  : {args.max_step}")
    print(f"▶ steps_cnt : {len(steps)}")
    print(f"▶ Params    : {', '.join(PARAMS.keys())}")
    print(f"▶ Run set   : {run_set_dir}")
    print(f"▶ S3        : {'OFF' if args.no_s3 else f's3://{BUCKET}/{S3_PREFIX_ROOT}/...'}")
    print(f"▶ Mongo     : {'OFF' if args.no_mongo else ('ON' if MONGO_URI else 'OFF (no MONGO_URI)')}")
    print("")

    client = Client(source=SOURCE_REAL, model=MODEL, resol=RESOL)

    s3 = None
    if not args.no_s3:
        s3 = boto3.client("s3", region_name=REGION)

    downloaded = existed = uploaded = mongo_written = jsonl_written = failed = 0

    # ✅ derived 메타는 (run, step)당 1번만 생성하기 위한 가드
    # - 이유: 10u/10v 처리 순서가 바뀌거나 재시도/부분실패가 있어도 중복 생성 시도를 줄이고,
    #        API에서 파생변수 목록을 안정적으로 노출할 수 있게 함
    derived_written_steps: set[tuple[str, int]] = set()

    for param, meta in PARAMS.items():
        unit = meta["unit"]
        name_en = meta["name_en"]
        stream = meta.get("stream", "oper")
        product_code = meta.get("product_code")

        for step in steps:
            valid_time = run_dt + timedelta(hours=step)

            filename = f"{PRODUCT_CODE}_{param}_{run_dt:%Y%m%d_%H}Z_step{step:03}.grib2"
            out_path = get_out_path(run_set_dir, stream, param, filename)
            ensure_dirs(out_path.parent)

            # 1) 다운로드
            if out_path.exists():
                print(f"⏭️ exists locally: {out_path.relative_to(run_set_dir)}")
                existed += 1
            else:
                print(f"⏬ retrieve param={param} stream={stream} step={step} → {out_path.relative_to(run_set_dir)}")
                try:
                    client.retrieve(
                        date=run_dt.date(),
                        type=TYPE_CODE,
                        stream=stream,
                        time=f"{run_dt:%H}",
                        step=step,
                        param=param,
                        target=str(out_path),
                    )
                    downloaded += 1
                except Exception as e:
                    print(f"  ❌ retrieve failed: {e}")
                    failed += 1
                    time.sleep(args.sleep)
                    continue

            # 2) S3 업로드
            s3_key = None
            if s3 is not None:
                try:
                    s3_key = build_s3_key(run_dt, product_code, param, filename)
                    print(f"  📤 upload s3://{BUCKET}/{s3_key}")
                    upload_to_s3(s3, out_path, s3_key)
                    uploaded += 1
                except Exception as e:
                    print(f"  ❌ s3 upload failed: {e}")
                    failed += 1
                    time.sleep(args.sleep)
                    continue

            # 3) Mongo 메타 upsert (raw)
            if (not args.no_mongo) and (col is not None):
                try:
                    upsert_metadata_mongo(
                        source=SOURCE,
                        dataset_code=PRODUCT_CODE,
                        model=MODEL,
                        resol=RESOL,
                        asset_type=ASSET_TYPE,
                        type_code=TYPE_CODE,
                        stream=stream,
                        param=param,
                        unit=unit,
                        name_en=name_en,
                        run_time=run_dt,
                        step=step,
                        valid_time=valid_time,
                        filename=filename,
                        local_path=out_path,
                        s3_key=s3_key,
                    )
                    mongo_written += 1
                except Exception as e:
                    print(f"  ❌ mongo upsert failed: {e}")
                    failed += 1

            # ✅ 3.5) 파생(바람) 메타데이터 planned 미리 생성 (run+step 당 1번)
            # - API에서 wind_speed_10m / wind_dir_10m 요청이 들어오면
            #   실제 계산은 10u/10v로 수행할 것이므로,
            #   Mongo에는 derived 메타를 "planned"로 미리 넣어둔다.
            if (not args.no_mongo) and (col is not None):
                if stream == "oper" and param in ("10u", "10v"):
                    step_key = (iso_z(run_dt), step)
                    if step_key not in derived_written_steps:
                        for dvar, dmeta in DERIVED_VARS.items():
                            try:
                                upsert_derived_metadata_mongo(
                                    source=SOURCE,
                                    dataset_code="computed",
                                    model=MODEL,
                                    resol=RESOL,
                                    type_code=TYPE_CODE,
                                    stream="oper",
                                    derived_var=dvar,
                                    derived_meta=dmeta,
                                    run_time=run_dt,
                                    step=step,
                                    valid_time=valid_time,
                                )
                                mongo_written += 1
                            except Exception as e:
                                print(f"  ❌ derived mongo upsert failed: {e}")
                                failed += 1

                        derived_written_steps.add(step_key)

            # 5) 업로드 후 로컬 삭제
            if DELETE_LOCAL_AFTER_UPLOAD and s3_key:
                try:
                    out_path.unlink(missing_ok=True)
                except Exception:
                    pass

            time.sleep(args.sleep)

    print("")
    print(f"✅ Done. downloaded={downloaded}, existed={existed}, uploaded={uploaded}, mongo={mongo_written}, jsonl={jsonl_written}, failed={failed}")
    if failed > 0:
        sys.exit(2)

if __name__ == "__main__":
    main()
