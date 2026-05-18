#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
list_mongo_noaa_steps.py

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[ 이 스크립트가 파악하는 것 ]

MongoDB assets_metadata 컬렉션에서
NOAA GFS 수집 데이터 중 삭제 대상 문서를 파악합니다.

  대상 컬렉션 : assets_metadata
  조건        :
    - source       = "noaa"
    - model        = "gfs"
    - dataset_code = "original"
    - step_hours   = 3의 배수가 아닌 것 (0~144h)
    - step_hours   = 6의 배수가 아닌 것 (150h~)

  directories 컬렉션은 건드리지 않습니다.
  (inventory_directory가 step 단위가 아닌 변수 단위 폴더이기 때문)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

사용법:
  python list_mongo_noaa_steps.py                  # 현황만 출력
  python list_mongo_noaa_steps.py --save_csv       # CSV로 저장
"""

from __future__ import annotations

import argparse
import csv
import os
from collections import defaultdict

from pymongo import MongoClient

# ── 설정 ─────────────────────────────────────────────────────────────
MONGO_URI    = os.getenv("MONGO_URI", "")
MONGO_DB     = os.getenv("MONGO_DB",  "optimal_loads")
ASSETS_COL   = os.getenv("MONGO_COL", "assets_metadata")


def is_keep_step(step: int) -> bool:
    """
    보존 기준 (ECMWF IFS 방식):
    - 0~144h  : 3시간 간격 (3의 배수)
    - 150~384h : 6시간 간격 (6의 배수)
    """
    if step <= 144:
        return step % 3 == 0
    else:
        return step % 6 == 0


def main():
    ap = argparse.ArgumentParser(description="MongoDB NOAA GFS 잉여 메타데이터 파악")
    ap.add_argument("--save_csv", action="store_true", help="결과를 CSV로 저장")
    ap.add_argument("--csv_all", default="mongo_noaa_all.csv",    help="전체 목록 CSV")
    ap.add_argument("--csv_del", default="mongo_noaa_delete.csv", help="삭제 대상 CSV")
    args = ap.parse_args()

    if not MONGO_URI:
        raise SystemExit("❌ MONGO_URI 환경변수를 설정하세요.")

    client     = MongoClient(MONGO_URI)
    assets_col = client[MONGO_DB][ASSETS_COL]

    sep = "=" * 65
    print(sep)
    print(f"  MongoDB  : {MONGO_DB}.{ASSETS_COL}")
    print(sep)

    # 1) NOAA original 전체 조회
    print("\nNOAA GFS 메타데이터 조회 중...")
    query = {
        "source":       "noaa",
        "model":        "gfs",
        "dataset_code": "original",
    }

    # step_hours, natural_key, variable, run_time_utc만 가져오기 (메모리 절약)
    projection = {
        "_id":           1,
        "natural_key":   1,
        "variable":      1,
        "step_hours":    1,
        "run_time_utc":  1,
        "size_bytes":    1,
    }

    all_docs   = []
    keep_docs  = []
    delete_docs = []

    cursor = assets_col.find(query, projection).batch_size(5000)
    for i, doc in enumerate(cursor):
        step = doc.get("step_hours", -1)
        keep = is_keep_step(step)
        doc["keep"] = keep
        all_docs.append(doc)
        if keep:
            keep_docs.append(doc)
        else:
            delete_docs.append(doc)

        if (i + 1) % 100000 == 0:
            print(f"  {i+1:,}개 처리 중...")

    print(f"  조회 완료: {len(all_docs):,}개\n")

    # 2) 통계
    total_size  = sum(d.get("size_bytes", 0) for d in all_docs)
    keep_size   = sum(d.get("size_bytes", 0) for d in keep_docs)
    delete_size = sum(d.get("size_bytes", 0) for d in delete_docs)

    print(sep)
    print("  [ 전체 현황 ]")
    print(f"  전체 문서 수  : {len(all_docs):,}개")
    print(f"  전체 용량     : {total_size / 1024**3:.2f} GB")
    print(sep)
    print("  [ 보존 대상 ]")
    print(f"  문서 수       : {len(keep_docs):,}개")
    print(f"  용량          : {keep_size / 1024**3:.2f} GB")
    print(sep)
    print("  [ 삭제 대상 ]")
    print(f"  문서 수       : {len(delete_docs):,}개")
    print(f"  용량          : {delete_size / 1024**3:.2f} GB")
    if total_size > 0:
        print(f"  비율          : {len(delete_docs) / len(all_docs) * 100:.1f}%")
    print(sep)

    # 3) 변수별 분포
    print("\n  [ 변수별 삭제 대상 문서 수 ]")
    by_var = defaultdict(int)
    for d in delete_docs:
        by_var[d.get("variable", "?")] += 1
    for var, cnt in sorted(by_var.items()):
        print(f"    {var:<12} : {cnt:,}개")

    # 4) step 분포 (상위 20개)
    print("\n  [ 삭제 대상 step 분포 (상위 20개) ]")
    by_step = defaultdict(int)
    for d in delete_docs:
        by_step[d.get("step_hours", -1)] += 1
    for step, cnt in sorted(by_step.items())[:20]:
        print(f"    step {step:03d} : {cnt:,}개")
    if len(by_step) > 20:
        print(f"    ... 외 {len(by_step) - 20}개 step")

    # 5) 런 범위
    run_times = sorted(set(d.get("run_time_utc", "") for d in all_docs if d.get("run_time_utc")))
    if run_times:
        print(f"\n  총 런(run) 수  : {len(run_times):,}개")
        print(f"  가장 오래된 런 : {run_times[0]}")
        print(f"  가장 최신 런   : {run_times[-1]}")

    # 6) CSV 저장
    if args.save_csv:
        print(f"\n  CSV 저장 중...")

        with open(args.csv_all, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["_id", "natural_key", "variable", "step_hours", "run_time_utc", "size_bytes", "keep"])
            writer.writeheader()
            for d in sorted(all_docs, key=lambda x: (x.get("run_time_utc",""), x.get("variable",""), x.get("step_hours", 0))):
                writer.writerow({
                    "_id":          str(d["_id"]),
                    "natural_key":  d.get("natural_key", ""),
                    "variable":     d.get("variable", ""),
                    "step_hours":   d.get("step_hours", ""),
                    "run_time_utc": d.get("run_time_utc", ""),
                    "size_bytes":   d.get("size_bytes", ""),
                    "keep":         d.get("keep", ""),
                })
        print(f"  전체 목록 → {args.csv_all} ({len(all_docs):,}행)")

        with open(args.csv_del, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["_id", "natural_key", "variable", "step_hours", "run_time_utc", "size_bytes"])
            writer.writeheader()
            for d in sorted(delete_docs, key=lambda x: (x.get("run_time_utc",""), x.get("variable",""), x.get("step_hours", 0))):
                writer.writerow({
                    "_id":          str(d["_id"]),
                    "natural_key":  d.get("natural_key", ""),
                    "variable":     d.get("variable", ""),
                    "step_hours":   d.get("step_hours", ""),
                    "run_time_utc": d.get("run_time_utc", ""),
                    "size_bytes":   d.get("size_bytes", ""),
                })
        print(f"  삭제 대상 → {args.csv_del} ({len(delete_docs):,}행)")

    print(f"\n{sep}")
    client.close()


if __name__ == "__main__":
    main()