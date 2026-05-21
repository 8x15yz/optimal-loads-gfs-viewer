#!/usr/bin/env python3
"""
S3에서 GRIB2 내려받아 S-111 / S-413 변환 후 S3 + MongoDB 업로드

Usage:
  python rerun_s100_conversion.py 2026-05-20T06:00:00Z          # S-111, S-413 둘 다
  python rerun_s100_conversion.py 2026-05-20T06:00:00Z --mode 2  # S-111만
  python rerun_s100_conversion.py 2026-05-20T06:00:00Z --mode 3  # S-413만
  python rerun_s100_conversion.py 2026-05-20T06:00:00Z --no-cleanup  # 로컬 파일 유지
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from pymongo import MongoClient

# depth_conversion_wrapper 임포트
THIS_DIR  = Path(__file__).resolve().parent
NOAA_DIR  = THIS_DIR.parent / "get_variables" / "noaa"
sys.path.insert(0, str(NOAA_DIR))

from depth_conversion_wrapper import rollback_s3, run_conversion  # noqa: E402

UTC = timezone.utc

# ── 환경변수 ─────────────────────────────────────────────────────────────────
BUCKET         = os.getenv("S3_BUCKET",      "optimal-loads")
REGION         = os.getenv("AWS_REGION",     "ap-northeast-2")
MONGO_URI      = os.getenv("MONGO_URI",      "")
MONGO_DB       = os.getenv("MONGO_DB",       "optimal_loads")
MONGO_S100_COL = os.getenv("MONGO_S100_COL", "s100assets_metadata")
MONGO_DIR_COL  = os.getenv("MONGO_DIR_COL",  "directories")

S3_GRIB2_ROOT = "noaa/gfs/fc"  # noaa/gfs/fc/YYYY/MM/DD/HHZ/original/PARAM/
WAVE_PARAMS   = ["UGRD", "VGRD", "HTSGW", "DIRPW", "WDIR", "WIND"]
STAGING_ROOT  = THIS_DIR / "staging"


# ── 유틸 ─────────────────────────────────────────────────────────────────────

def parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s.rstrip("Z"))
    return dt.replace(tzinfo=UTC)


def s3_download_grib2(s3, run_dt: datetime, run_set_dir: Path) -> int:
    yyyy = run_dt.strftime("%Y")
    mm   = run_dt.strftime("%m")
    dd   = run_dt.strftime("%d")
    hhZ  = f"{run_dt:%H}Z"

    total = 0
    for param in WAVE_PARAMS:
        prefix = f"{S3_GRIB2_ROOT}/{yyyy}/{mm}/{dd}/{hhZ}/original/{param}/"
        paginator = s3.get_paginator("list_objects_v2")
        keys = [
            obj["Key"]
            for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix)
            for obj in page.get("Contents", [])
        ]

        if not keys:
            print(f"  [S3] {param}: 오브젝트 없음 (prefix={prefix})")
            continue

        local_dir = run_set_dir / "wave" / param
        local_dir.mkdir(parents=True, exist_ok=True)

        for key in keys:
            local_path = local_dir / Path(key).name
            if local_path.exists():
                total += 1
                continue
            print(f"  ↓ {key}")
            s3.download_file(BUCKET, key, str(local_path))
            total += 1

    return total


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="S3 GRIB2 → S-111/S-413 재변환")
    parser.add_argument("run_utc", help="런타임 (예: 2026-05-20T06:00:00Z)")
    parser.add_argument("--mode", type=int, choices=[2, 3], default=None,
                        help="2=S-111만, 3=S-413만 (기본: 둘 다)")
    parser.add_argument("--no-cleanup", action="store_true",
                        help="변환 후 로컬 스테이징 파일 삭제 안 함")
    args = parser.parse_args()

    run_dt = parse_utc(args.run_utc)
    modes  = [args.mode] if args.mode else [2, 3]

    yyyy = run_dt.strftime("%Y")
    mm   = run_dt.strftime("%m")
    dd   = run_dt.strftime("%d")
    hhZ  = f"{run_dt:%H}Z"
    run_set_dir = STAGING_ROOT / yyyy / mm / dd / hhZ

    print("=" * 56)
    print("  S-111 / S-413 재변환")
    print(f"  런타임    : {run_dt.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print(f"  모드      : {['S-111' if m == 2 else 'S-413' for m in modes]}")
    print(f"  스테이징  : {run_set_dir}")
    print(f"  S3 버킷   : {BUCKET}")
    print("=" * 56)

    if not MONGO_URI:
        print("[ERROR] MONGO_URI 환경변수가 없습니다.", file=sys.stderr)
        sys.exit(1)

    s3 = boto3.client("s3", region_name=REGION)
    mongo_client = MongoClient(MONGO_URI)
    db       = mongo_client[MONGO_DB]
    s100_col = db[MONGO_S100_COL]
    dir_col  = db[MONGO_DIR_COL]

    # ── 1. GRIB2 다운로드 ─────────────────────────────────────────────────────
    print("\n── GRIB2 다운로드 ──────────────────────────────")
    n = s3_download_grib2(s3, run_dt, run_set_dir)
    print(f"  총 {n}개 파일 준비 완료")

    # ── 2. 변환 ───────────────────────────────────────────────────────────────
    failed = False
    for mode in modes:
        product = "s111" if mode == 2 else "s413"
        print(f"\n── {product.upper()} 변환 (mode={mode}) ──────────────────────")
        ok, keys = run_conversion(
            mode=mode,
            run_time_utc=run_dt,
            run_set_dir=run_set_dir,
            s3_client=s3,
            s100_col=s100_col,
            dir_col=dir_col,
        )
        if not ok:
            print(f"❌ {product.upper()} 변환 실패 → 롤백")
            rollback_s3(s3, keys)
            failed = True
            break

    # ── 3. 로컬 정리 ──────────────────────────────────────────────────────────
    if not args.no_cleanup and run_set_dir.exists():
        print(f"\n── 로컬 스테이징 정리: {run_set_dir}")
        shutil.rmtree(run_set_dir)

    mongo_client.close()

    if failed:
        print("\n❌ 변환 실패로 종료.")
        sys.exit(1)

    print("\n✅ 완료.")


if __name__ == "__main__":
    main()
