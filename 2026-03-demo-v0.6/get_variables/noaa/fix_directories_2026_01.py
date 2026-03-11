#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_from_s3_2026_01.py

S3에 있는 파일 목록 기준으로
assets_metadata + directories 를 역으로 채우는 보정 스크립트.

사용법:
  python backfill_from_s3_2026_01.py --dry_run
  python backfill_from_s3_2026_01.py
  python backfill_from_s3_2026_01.py --start_day 24 --end_day 24
"""
from __future__ import annotations

import argparse
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import boto3
from pymongo import ASCENDING, MongoClient

# ── 설정 (noaa_pipeline.py 와 동일) ───────────────────────────────────────────
S3_BUCKET  = os.getenv("S3_BUCKET",  "optimal-loads")
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-2")

MONGO_URI    = os.getenv("MONGO_URI",     "")
MONGO_DB     = os.getenv("MONGO_DB",      "optimal_loads")
ASSETS_COL   = os.getenv("MONGO_COL",     "assets_metadata")
DIR_COL_NAME = os.getenv("MONGO_DIR_COL", "directories")

SOURCE       = "noaa"
MODEL        = "gfs"
TYPE_CODE    = "fc"
PRODUCT_CODE = "original"
ASSET_TYPE   = "forecast"
STREAM       = "wave"
RESOL        = "0p25"
WAVE_DOMAIN  = os.getenv("NOAA_GFS_WAVE_DOMAIN", "global")
SRC_BUCKET   = os.getenv("NOAA_GFS_BUCKET", "noaa-gfs-bdp-pds")

ROOT_DIR       = f"{SOURCE}/{MODEL}/"
S3_PREFIX_ROOT = f"{SOURCE}/{MODEL}/{TYPE_CODE}"

TARGET_YEAR  = 2026
TARGET_MONTH = 1

PARAMS: Dict[str, Dict[str, str]] = {
    "UGRD":  {"unit": "m/s",    "name_en": "Eastward Current"},
    "VGRD":  {"unit": "m/s",    "name_en": "Northward Current"},
    "WIND":  {"unit": "m/s",    "name_en": "Wind Speed"},
    "WDIR":  {"unit": "degree", "name_en": "Wind Direction"},
    "HTSGW": {"unit": "m",      "name_en": "Significant Wave Height"},
    "PERPW": {"unit": "s",      "name_en": "Peak Wave Period"},
    "DIRPW": {"unit": "degree", "name_en": "Peak Wave Direction"},
}

UTC = timezone.utc

# S3 key 패턴: noaa/gfs/fc/YYYY/MM/DD/HHZ/original/PARAM/original_PARAM_YYYYMMDD_HHZ_stepNNN.grib2
KEY_RE = re.compile(
    r"^noaa/gfs/fc/"
    r"(?P<yyyy>\d{4})/(?P<mm>\d{2})/(?P<dd>\d{2})/"
    r"(?P<hh>\d{2})Z/original/(?P<param>[^/]+)/"
    r"original_[^_]+_\d{8}_\d{2}Z_step(?P<step>\d{3})\.grib2$"
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_dir(d: str) -> str:
    d = (d or "").strip().lstrip("/")
    if d and not d.endswith("/"):
        d += "/"
    return d


def _split_dir(d: str) -> Tuple[Optional[str], str]:
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


def list_s3_objects(s3, start_day: int, end_day: int) -> list:
    objects = []
    paginator = s3.get_paginator("list_objects_v2")
    for day in range(start_day, end_day + 1):
        prefix = f"{S3_PREFIX_ROOT}/{TARGET_YEAR}/{TARGET_MONTH:02d}/{day:02d}/"
        for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
            for obj in page.get("Contents", []):
                objects.append({"key": obj["Key"], "size": obj["Size"]})
    return objects


def parse_s3_key(key: str) -> Optional[Dict[str, Any]]:
    m = KEY_RE.match(key)
    if not m:
        return None
    param = m.group("param")
    if param not in PARAMS:
        return None
    run_time   = datetime(int(m.group("yyyy")), int(m.group("mm")), int(m.group("dd")), int(m.group("hh")), tzinfo=UTC)
    step       = int(m.group("step"))
    valid_time = run_time + timedelta(hours=step)
    return {
        "run_time":   run_time,
        "step":       step,
        "valid_time": valid_time,
        "param":      param,
        "filename":   key.split("/")[-1],
        "s3_key":     key,
    }


def build_keys(param: str, run_time: datetime, step: int, valid_time: datetime):
    natural_key = (
        f"{PRODUCT_CODE}|{SOURCE}|{MODEL}|{ASSET_TYPE}|{STREAM}|{param}"
        f"|run={iso_z(run_time)}|step={step}"
    )
    valid_key = (
        f"{PRODUCT_CODE}|{SOURCE}|{MODEL}|{ASSET_TYPE}|{STREAM}|{param}"
        f"|valid={iso_z(valid_time)}"
    )
    return natural_key, valid_key


def build_doc(parsed: Dict[str, Any], size_bytes: int) -> Dict[str, Any]:
    run_time   = parsed["run_time"]
    step       = parsed["step"]
    valid_time = parsed["valid_time"]
    param      = parsed["param"]
    meta       = PARAMS[param]
    natural_key, valid_key = build_keys(param, run_time, step, valid_time)

    return {
        "source":       SOURCE,
        "dataset_code": PRODUCT_CODE,
        "variable":     param,
        "name_en":      meta["name_en"],
        "unit":         meta["unit"],
        "model":        MODEL,
        "type":         ASSET_TYPE,
        "stream":       STREAM,
        "resolution":   {"lon_deg": 0.25, "lat_deg": 0.25},
        "run_time_utc":   iso_z(run_time),
        "step_hours":     step,
        "valid_time_utc": iso_z(valid_time),
        "year":  int(valid_time.strftime("%Y")),
        "month": int(valid_time.strftime("%m")),
        "name":         parsed["filename"],
        "format":       "grib2",
        "content_type": "application/x-grib",
        "size_bytes":   size_bytes,
        "created_at":   utc_now(),
        "natural_key":  natural_key,
        "valid_key":    valid_key,
        "source_parameters": {
            "noaa": {
                "type":   TYPE_CODE,
                "stream": STREAM,
                "time":   f"{run_time:%H}",
                "step":   step,
                "param":  param,
                "resol":  RESOL,
                "access": "aws-s3-range-idx",
                "bucket": SRC_BUCKET,
                "domain": WAVE_DOMAIN,
            }
        },
        "inventory_directory": (
            f"{SOURCE}/{MODEL}/{run_time:%Y/%Y-%m/%Y-%m-%d/%H}Z/{PRODUCT_CODE}/{param}/"
        ),
        "inventory_name": (
            f"{PRODUCT_CODE}_{param}_{run_time:%Y%m%d_%H}Z_step{step:03}"
        ),
        "s3": {"bucket": S3_BUCKET, "region": AWS_REGION, "key": parsed["s3_key"]},
    }


def upsert_directories(dir_col, doc: Dict[str, Any]) -> None:
    inv_dir = _norm_dir(doc.get("inventory_directory", ""))
    if not inv_dir or not inv_dir.startswith(ROOT_DIR):
        return
    lm  = doc.get("created_at", utc_now())
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
            # ① 부모 생성 ($setOnInsert 단독)
            dir_col.update_one(
                {"_id": parent},
                {
                    "$setOnInsert": {"dir": parent, "parent": p_parent, "name": p_name, "children_dirs": [], "created_at": now},
                    "$max": {"last_modified": lm},
                    "$set": {"updated_at": now},
                },
                upsert=True,
            )
            # ② children 추가 (별도 호출 — code-40 회피)
            dir_col.update_one(
                {"_id": parent},
                {
                    "$addToSet": {"children_dirs": name + "/"},
                    "$max": {"last_modified": lm},
                    "$set": {"updated_at": now},
                },
            )
        cur = parent


def main():
    ap = argparse.ArgumentParser(description="S3 기반 assets_metadata/directories 보정")
    ap.add_argument("--dry_run",   action="store_true", help="DB 쓰기 없이 대상만 출력")
    ap.add_argument("--start_day", type=int, default=1,  help="시작 일 (기본 1)")
    ap.add_argument("--end_day",   type=int, default=24, help="끝 일 (기본 24)")
    args = ap.parse_args()

    if not MONGO_URI:
        raise SystemExit("MONGO_URI 환경변수를 설정하세요.")

    s3         = boto3.client("s3", region_name=AWS_REGION)
    client     = MongoClient(MONGO_URI)
    assets_col = client[MONGO_DB][ASSETS_COL]
    dir_col    = client[MONGO_DB][DIR_COL_NAME]

    try:
        assets_col.create_index([("natural_key", ASCENDING)], unique=True)
        assets_col.create_index([("inventory_directory", ASCENDING), ("name", ASCENDING)])
        dir_col.create_index([("parent", ASCENDING), ("name", ASCENDING)])
    except Exception:
        pass

    sep = "=" * 65
    print(sep)
    print(f"  S3 버킷 : s3://{S3_BUCKET}")
    print(f"  대상    : {TARGET_YEAR}-{TARGET_MONTH:02d} / day {args.start_day:02d}~{args.end_day:02d}")
    print(f"  dry_run : {args.dry_run}")
    print(sep)

    print("S3 오브젝트 목록 수집 중...")
    objects = list_s3_objects(s3, args.start_day, args.end_day)
    print(f"  -> {len(objects)}개 오브젝트 발견\n")

    print("기존 assets_metadata natural_key 캐싱 중...")
    prefix_re = f"^{PRODUCT_CODE}\\|{SOURCE}\\|{MODEL}\\|"
    existing_nk = set(
        doc["natural_key"]
        for doc in assets_col.find(
            {"natural_key": {"$regex": prefix_re}},
            {"natural_key": 1, "_id": 0},
        )
    )
    print(f"  -> 기존 {len(existing_nk)}개\n")

    cnt_skip = cnt_insert = cnt_fail = 0

    for obj in objects:
        parsed = parse_s3_key(obj["key"])
        if not parsed:
            cnt_fail += 1
            continue

        doc = build_doc(parsed, obj["size"])
        nk  = doc["natural_key"]

        if nk in existing_nk:
            cnt_skip += 1
            continue

        if args.dry_run:
            print(f"  [DRY] {nk}")
            cnt_insert += 1
            continue

        assets_col.update_one({"natural_key": nk}, {"$setOnInsert": doc}, upsert=True)
        upsert_directories(dir_col, doc)
        existing_nk.add(nk)
        cnt_insert += 1
        if cnt_insert % 1000 == 0:
            print(f"  처리 중... {cnt_insert}건 insert")

    client.close()

    print(f"\n{'[DRY-RUN] ' if args.dry_run else ''}완료")
    print(sep)
    print(f"  신규 insert : {cnt_insert}")
    print(f"  skip (기존) : {cnt_skip}")
    print(f"  파싱 실패   : {cnt_fail}")
    print(sep)


if __name__ == "__main__":
    main()