#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
list_s3_noaa_steps.py

S3에 저장된 NOAA GFS 데이터 현황 파악 스크립트.
- 전체 파일 목록 및 step 분포 출력
- 삭제 대상 (3시간 간격 아닌 파일) 미리 확인
- CSV로 저장 가능

사용법:
  python list_s3_noaa_steps.py                        # 전체 현황만 출력
  python list_s3_noaa_steps.py --save_csv             # CSV로 저장
  python list_s3_noaa_steps.py --save_csv --prefix noaa/gfs/fc/2025/07   # 특정 월만
"""

from __future__ import annotations

import argparse
import csv
import os
import re
from collections import defaultdict
from datetime import datetime, timezone

import boto3

# ── 설정 ─────────────────────────────────────────────────────────────
S3_BUCKET  = os.getenv("S3_BUCKET",  "optimal-loads")
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-2")
NOAA_PREFIX = "noaa/gfs/fc/"

# S3 key 패턴
KEY_RE = re.compile(
    r"^noaa/gfs/fc/"
    r"(?P<yyyy>\d{4})/(?P<mm>\d{2})/(?P<dd>\d{2})/"
    r"(?P<hh>\d{2})Z/original/(?P<param>[^/]+)/"
    r"original_[^_]+_\d{8}_\d{2}Z_step(?P<step>\d{3})\.grib2$"
)

UTC = timezone.utc


def list_all_objects(s3, prefix: str) -> list[dict]:
    """S3에서 전체 오브젝트 목록 가져오기"""
    objects = []
    paginator = s3.get_paginator("list_objects_v2")
    print(f"  S3 목록 수집 중... (prefix: {prefix})")
    page_count = 0
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            objects.append({"key": obj["Key"], "size": obj["Size"]})
        page_count += 1
        if page_count % 10 == 0:
            print(f"    {len(objects):,}개 수집 중...")
    return objects


def parse_key(key: str) -> dict | None:
    m = KEY_RE.match(key)
    if not m:
        return None
    step = int(m.group("step"))
    run_hour = int(m.group("hh"))
    return {
        "key":      key,
        "yyyy":     m.group("yyyy"),
        "mm":       m.group("mm"),
        "dd":       m.group("dd"),
        "hh":       run_hour,
        "param":    m.group("param"),
        "step":     step,
        "run_id":   f"{m.group('yyyy')}-{m.group('mm')}-{m.group('dd')}T{run_hour:02d}Z",
    }


def is_keep_step(step: int) -> bool:
    """ECMWF 방식 기준: 0~144h 3시간 간격, 150~384h 6시간 간격"""
    if step <= 144:
        return step % 3 == 0
    else:
        return step % 6 == 0


def main():
    ap = argparse.ArgumentParser(description="S3 NOAA GFS 데이터 현황 파악")
    ap.add_argument("--prefix",   default=NOAA_PREFIX, help="S3 prefix (기본: noaa/gfs/fc/)")
    ap.add_argument("--save_csv", action="store_true",  help="결과를 CSV로 저장")
    ap.add_argument("--csv_all",  default="s3_noaa_all.csv",    help="전체 목록 CSV 파일명")
    ap.add_argument("--csv_del",  default="s3_noaa_delete.csv", help="삭제 대상 CSV 파일명")
    args = ap.parse_args()

    s3 = boto3.client("s3", region_name=AWS_REGION)

    sep = "=" * 65
    print(sep)
    print(f"  버킷   : s3://{S3_BUCKET}")
    print(f"  prefix : {args.prefix}")
    print(sep)

    # 1) 전체 목록 수집
    objects = list_all_objects(s3, args.prefix)
    print(f"\n총 {len(objects):,}개 오브젝트 발견\n")

    # 2) 파싱
    parsed_list = []
    skip_count  = 0
    for obj in objects:
        p = parse_key(obj["key"])
        if p:
            p["size"] = obj["size"]
            p["keep"] = is_keep_step(p["step"])
            parsed_list.append(p)
        else:
            skip_count += 1

    keep_list   = [p for p in parsed_list if p["keep"]]
    delete_list = [p for p in parsed_list if not p["keep"]]

    total_size      = sum(p["size"] for p in parsed_list)
    keep_size       = sum(p["size"] for p in keep_list)
    delete_size     = sum(p["size"] for p in delete_list)

    # 3) 현황 출력
    print(sep)
    print("  [ 전체 현황 ]")
    print(f"  전체 파일 수  : {len(parsed_list):,}개")
    print(f"  전체 용량     : {total_size / 1024**3:.2f} GB")
    print(sep)
    print("  [ 보존 대상 (ECMWF 방식) ]")
    print(f"  파일 수       : {len(keep_list):,}개")
    print(f"  용량          : {keep_size / 1024**3:.2f} GB")
    print(sep)
    print("  [ 삭제 대상 (1h 간격 잉여분) ]")
    print(f"  파일 수       : {len(delete_list):,}개")
    print(f"  용량          : {delete_size / 1024**3:.2f} GB")
    print(f"  절감률        : {delete_size / total_size * 100:.1f}%")
    print(sep)

    # 4) 변수별 분포
    print("\n  [ 변수별 삭제 대상 파일 수 ]")
    by_param = defaultdict(int)
    for p in delete_list:
        by_param[p["param"]] += 1
    for param, cnt in sorted(by_param.items()):
        print(f"    {param:<10} : {cnt:,}개")

    # 5) step 분포 (삭제 대상)
    print("\n  [ 삭제 대상 step 분포 (상위 20개) ]")
    by_step = defaultdict(int)
    for p in delete_list:
        by_step[p["step"]] += 1
    for step, cnt in sorted(by_step.items())[:20]:
        print(f"    step {step:03d} : {cnt:,}개")
    if len(by_step) > 20:
        print(f"    ... 외 {len(by_step) - 20}개 step")

    # 6) 런 수
    run_ids = set(p["run_id"] for p in parsed_list)
    print(f"\n  총 런(run) 수 : {len(run_ids):,}개")
    if run_ids:
        sorted_runs = sorted(run_ids)
        print(f"  가장 오래된 런 : {sorted_runs[0]}")
        print(f"  가장 최신 런   : {sorted_runs[-1]}")

    # 7) CSV 저장
    if args.save_csv:
        print(f"\n  CSV 저장 중...")

        # 전체 목록
        with open(args.csv_all, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["key", "run_id", "param", "step", "size", "keep"])
            writer.writeheader()
            for p in sorted(parsed_list, key=lambda x: (x["run_id"], x["param"], x["step"])):
                writer.writerow({
                    "key":    p["key"],
                    "run_id": p["run_id"],
                    "param":  p["param"],
                    "step":   p["step"],
                    "size":   p["size"],
                    "keep":   p["keep"],
                })
        print(f"  전체 목록 → {args.csv_all} ({len(parsed_list):,}행)")

        # 삭제 대상만
        with open(args.csv_del, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["key", "run_id", "param", "step", "size"])
            writer.writeheader()
            for p in sorted(delete_list, key=lambda x: (x["run_id"], x["param"], x["step"])):
                writer.writerow({
                    "key":    p["key"],
                    "run_id": p["run_id"],
                    "param":  p["param"],
                    "step":   p["step"],
                    "size":   p["size"],
                })
        print(f"  삭제 대상     → {args.csv_del} ({len(delete_list):,}행)")

    print(f"\n파싱 실패(패턴 불일치): {skip_count}개")
    print(sep)


if __name__ == "__main__":
    main()