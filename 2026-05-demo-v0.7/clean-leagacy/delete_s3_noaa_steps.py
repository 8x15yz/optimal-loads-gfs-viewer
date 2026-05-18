#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
delete_s3_noaa_steps.py

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[ 이 스크립트가 삭제하는 것 ]

NOAA GFS는 원래 0~119h 구간을 1시간 간격으로 수집하고 있었는데,
ECMWF IFS 방식으로 통일하면서 아래 기준으로 정리합니다.

  보존 기준 (ECMWF IFS 방식과 동일):
    - step   0 ~ 144h : 3시간 간격 (step % 3 == 0)
    - step 150 ~ 384h : 6시간 간격 (step % 6 == 0)

  삭제 대상:
    - step   1,  2,  4,  5,  7,  8 ... (3의 배수가 아닌 1h 간격 잉여분)
    - step 150 ~ 384h 중 6의 배수가 아닌 것 (123, 126 → 129, 132... 등)

  삭제하지 않는 것:
    - ecmwf/ 하위 데이터 (ECMWF는 건드리지 않음)
    - noaa/ 이외의 모든 데이터
    - 보존 기준에 해당하는 step 파일

  예시:
    삭제: noaa/gfs/fc/2025/07/01/00Z/original/HTSGW/original_HTSGW_20250701_00Z_step001.grib2
    삭제: noaa/gfs/fc/2025/07/01/00Z/original/HTSGW/original_HTSGW_20250701_00Z_step002.grib2
    보존: noaa/gfs/fc/2025/07/01/00Z/original/HTSGW/original_HTSGW_20250701_00Z_step003.grib2
    보존: noaa/gfs/fc/2025/07/01/00Z/original/HTSGW/original_HTSGW_20250701_00Z_step006.grib2

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

사용법:
  # 1) 먼저 dry-run으로 확인 (실제 삭제 안 함)
  python delete_s3_noaa_steps.py --dry_run

  # 2) 특정 기간만 먼저 테스트
  python delete_s3_noaa_steps.py --dry_run --prefix noaa/gfs/fc/2025/07/

  # 3) 실제 삭제 (확인 후 실행)
  python delete_s3_noaa_steps.py

  # 4) CSV로 삭제 목록 저장하면서 삭제
  python delete_s3_noaa_steps.py --save_csv
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import time
from collections import defaultdict

import boto3

# ── 설정 ─────────────────────────────────────────────────────────────
S3_BUCKET   = os.getenv("S3_BUCKET",  "optimal-loads")
AWS_REGION  = os.getenv("AWS_REGION", "ap-northeast-2")
NOAA_PREFIX = "noaa/gfs/fc/"

# S3 배치 삭제는 한 번에 최대 1000개
BATCH_SIZE = 1000

# S3 key 패턴
KEY_RE = re.compile(
    r"^noaa/gfs/fc/"
    r"(?P<yyyy>\d{4})/(?P<mm>\d{2})/(?P<dd>\d{2})/"
    r"(?P<hh>\d{2})Z/original/(?P<param>[^/]+)/"
    r"original_[^_]+_\d{8}_\d{2}Z_step(?P<step>\d{3})\.grib2$"
)


def is_keep_step(step: int) -> bool:
    """
    보존 기준 (ECMWF IFS 방식):
    - 0~144h : 3시간 간격
    - 150~384h : 6시간 간격
    """
    if step <= 144:
        return step % 3 == 0
    else:
        return step % 6 == 0


def list_delete_targets(s3, prefix: str) -> list[dict]:
    """삭제 대상 파일 목록 수집"""
    targets = []
    paginator = s3.get_paginator("list_objects_v2")
    total_scanned = 0
    print(f"  S3 스캔 중... (prefix: {prefix})")

    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key  = obj["Key"]
            size = obj["Size"]
            total_scanned += 1

            m = KEY_RE.match(key)
            if not m:
                continue

            step = int(m.group("step"))
            if not is_keep_step(step):
                targets.append({"key": key, "size": size, "step": step})

        if total_scanned % 100000 == 0:
            print(f"    {total_scanned:,}개 스캔, 삭제 대상 {len(targets):,}개 발견 중...")

    print(f"  스캔 완료: 총 {total_scanned:,}개 중 삭제 대상 {len(targets):,}개\n")
    return targets


def delete_in_batches(s3, targets: list[dict], dry_run: bool) -> tuple[int, int]:
    """배치 단위로 삭제 (S3 bulk delete, 최대 1000개/요청)"""
    total    = len(targets)
    deleted  = 0
    failed   = 0

    for i in range(0, total, BATCH_SIZE):
        batch = targets[i:i + BATCH_SIZE]
        objects = [{"Key": t["key"]} for t in batch]

        if dry_run:
            deleted += len(batch)
            if i == 0:
                print(f"  [DRY-RUN] 첫 번째 배치 샘플 (5개):")
                for t in batch[:5]:
                    print(f"    삭제 예정: {t['key']}")
        else:
            try:
                resp = s3.delete_objects(
                    Bucket=S3_BUCKET,
                    Delete={"Objects": objects, "Quiet": True},
                )
                errors = resp.get("Errors", [])
                deleted += len(batch) - len(errors)
                failed  += len(errors)
                if errors:
                    for e in errors[:3]:
                        print(f"  ⚠️  삭제 실패: {e['Key']} - {e['Message']}")
            except Exception as e:
                print(f"  ❌ 배치 오류: {e}")
                failed += len(batch)

        # 진행률 출력
        progress = min(i + BATCH_SIZE, total)
        if progress % 50000 == 0 or progress == total:
            pct = progress / total * 100
            print(f"  진행: {progress:,}/{total:,} ({pct:.1f}%) | 삭제: {deleted:,} | 실패: {failed}")

        # NOAA 서버 부하 방지용 대기 (삭제는 S3 자체라 짧게)
        time.sleep(0.05)

    return deleted, failed


def main():
    ap = argparse.ArgumentParser(description="NOAA GFS S3 잉여 데이터 삭제")
    ap.add_argument("--dry_run",  action="store_true", help="실제 삭제 없이 대상만 확인")
    ap.add_argument("--prefix",   default=NOAA_PREFIX, help="S3 prefix")
    ap.add_argument("--save_csv", action="store_true",  help="삭제 목록 CSV 저장")
    ap.add_argument("--csv_file", default="deleted_noaa_steps.csv", help="CSV 파일명")
    args = ap.parse_args()

    s3  = boto3.client("s3", region_name=AWS_REGION)
    sep = "=" * 65

    print(sep)
    print(f"  버킷     : s3://{S3_BUCKET}")
    print(f"  prefix   : {args.prefix}")
    print(f"  dry_run  : {args.dry_run}")
    print(sep)
    print()
    print("[ 삭제 기준 ]")
    print("  - NOAA GFS step 중 보존 기준에 맞지 않는 파일 삭제")
    print("  - 보존: step 0~144h 중 3의 배수 / step 150~384h 중 6의 배수")
    print("  - 삭제: 그 외 모든 step (1h 간격 잉여분)")
    print()

    # 삭제 대상 수집
    targets = list_delete_targets(s3, args.prefix)

    if not targets:
        print("삭제 대상 파일이 없습니다.")
        return

    # 통계
    total_size = sum(t["size"] for t in targets)
    by_param   = defaultdict(int)
    for t in targets:
        m = KEY_RE.match(t["key"])
        if m:
            by_param[m.group("param")] += 1

    print(sep)
    print(f"  삭제 대상 파일 수 : {len(targets):,}개")
    print(f"  삭제 대상 용량    : {total_size / 1024**3:.2f} GB")
    print()
    print("  [ 변수별 ]")
    for param, cnt in sorted(by_param.items()):
        print(f"    {param:<10} : {cnt:,}개")
    print(sep)

    # CSV 저장
    if args.save_csv:
        with open(args.csv_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["key", "step", "size"])
            writer.writeheader()
            for t in targets:
                writer.writerow({"key": t["key"], "step": t["step"], "size": t["size"]})
        print(f"  삭제 목록 CSV 저장: {args.csv_file}")

    # 실제 삭제 전 확인
    if not args.dry_run:
        print()
        print("  ⚠️  위 파일들을 실제로 삭제합니다. 되돌릴 수 없습니다.")
        confirm = input("  계속하려면 'delete' 를 입력하세요: ").strip()
        if confirm != "delete":
            print("  취소되었습니다.")
            return
        print()

    # 삭제 실행
    print(f"  {'[DRY-RUN] ' if args.dry_run else ''}삭제 시작...")
    deleted, failed = delete_in_batches(s3, targets, dry_run=args.dry_run)

    print()
    print(sep)
    print(f"  {'[DRY-RUN] ' if args.dry_run else ''}완료")
    print(f"  삭제 성공 : {deleted:,}개")
    print(f"  삭제 실패 : {failed:,}개")
    print(f"  절감 용량 : {total_size / 1024**3:.2f} GB")
    print(sep)


if __name__ == "__main__":
    main()