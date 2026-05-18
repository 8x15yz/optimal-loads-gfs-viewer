#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
delete_mongo_noaa_steps.py

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[ 이 스크립트가 삭제하는 것 ]

MongoDB assets_metadata 컬렉션에서
NOAA GFS 잉여 step 문서를 삭제합니다.

  삭제 조건:
    - source       = "noaa"
    - model        = "gfs"
    - dataset_code = "original"
    - step_hours가 3의 배수가 아닌 것 (0~144h)
    - step_hours가 6의 배수가 아닌 것 (150h~)

  건드리지 않는 것:
    - directories 컬렉션 (step 단위 폴더가 아니므로 불필요)
    - ecmwf 관련 문서
    - 보존 기준에 해당하는 step 문서

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

실행 순서 (권장):
  1. S3 삭제 먼저 완료
  2. 그 다음 이 스크립트 실행

사용법:
  # 1) dry-run으로 확인 (실제 삭제 안 함)
  python delete_mongo_noaa_steps.py --dry_run

  # 2) 실제 삭제
  python delete_mongo_noaa_steps.py
"""

from __future__ import annotations

import argparse
import os
import time
from collections import defaultdict

from pymongo import MongoClient

# ── 설정 ─────────────────────────────────────────────────────────────
MONGO_URI  = os.getenv("MONGO_URI", "")
MONGO_DB   = os.getenv("MONGO_DB",  "optimal_loads")
ASSETS_COL = os.getenv("MONGO_COL", "assets_metadata")

BATCH_SIZE = 1000  # 한 번에 삭제할 문서 수


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
    ap = argparse.ArgumentParser(description="MongoDB NOAA GFS 잉여 메타데이터 삭제")
    ap.add_argument("--dry_run", action="store_true", help="실제 삭제 없이 대상만 확인")
    args = ap.parse_args()

    if not MONGO_URI:
        raise SystemExit("❌ MONGO_URI 환경변수를 설정하세요.")

    client     = MongoClient(MONGO_URI)
    assets_col = client[MONGO_DB][ASSETS_COL]

    sep = "=" * 65
    print(sep)
    print(f"  MongoDB  : {MONGO_DB}.{ASSETS_COL}")
    print(f"  dry_run  : {args.dry_run}")
    print(sep)
    print()
    print("[ 삭제 기준 ]")
    print("  - source='noaa', model='gfs', dataset_code='original'")
    print("  - 보존: step 0~144h 중 3의 배수 / step 150~384h 중 6의 배수")
    print("  - 삭제: 그 외 모든 step (1h 간격 잉여분)")
    print("  - directories 컬렉션은 건드리지 않음")
    print()

    # 1) 삭제 대상 _id 수집
    print("삭제 대상 문서 조회 중...")
    query = {
        "source":       "noaa",
        "model":        "gfs",
        "dataset_code": "original",
    }
    projection = {"_id": 1, "step_hours": 1, "variable": 1}

    delete_ids = []
    by_var     = defaultdict(int)
    scanned    = 0

    cursor = assets_col.find(query, projection).batch_size(5000)
    for doc in cursor:
        scanned += 1
        step = doc.get("step_hours", -1)
        if not is_keep_step(step):
            delete_ids.append(doc["_id"])
            by_var[doc.get("variable", "?")] += 1

        if scanned % 100000 == 0:
            print(f"  {scanned:,}개 스캔, 삭제 대상 {len(delete_ids):,}개 발견 중...")

    print(f"  스캔 완료: 총 {scanned:,}개 중 삭제 대상 {len(delete_ids):,}개\n")

    if not delete_ids:
        print("삭제 대상 문서가 없습니다.")
        client.close()
        return

    # 2) 통계 출력
    print(sep)
    print(f"  삭제 대상 문서 수 : {len(delete_ids):,}개")
    print()
    print("  [ 변수별 ]")
    for var, cnt in sorted(by_var.items()):
        print(f"    {var:<12} : {cnt:,}개")
    print(sep)

    # 3) 실제 삭제 전 확인
    if not args.dry_run:
        print()
        print("  ⚠️  위 문서들을 실제로 삭제합니다. 되돌릴 수 없습니다.")
        confirm = input("  계속하려면 'delete' 를 입력하세요: ").strip()
        if confirm != "delete":
            print("  취소되었습니다.")
            client.close()
            return
        print()

    # 4) 배치 삭제
    print(f"  {'[DRY-RUN] ' if args.dry_run else ''}삭제 시작...")
    total   = len(delete_ids)
    deleted = 0
    failed  = 0

    for i in range(0, total, BATCH_SIZE):
        batch = delete_ids[i:i + BATCH_SIZE]

        if args.dry_run:
            deleted += len(batch)
        else:
            try:
                result = assets_col.delete_many({"_id": {"$in": batch}})
                deleted += result.deleted_count
                failed  += len(batch) - result.deleted_count
            except Exception as e:
                print(f"  ❌ 배치 오류: {e}")
                failed += len(batch)

        # 진행률
        progress = min(i + BATCH_SIZE, total)
        if progress % 50000 == 0 or progress == total:
            pct = progress / total * 100
            print(f"  진행: {progress:,}/{total:,} ({pct:.1f}%) | 삭제: {deleted:,} | 실패: {failed}")

        time.sleep(0.01)

    # 5) 완료
    print()
    print(sep)
    print(f"  {'[DRY-RUN] ' if args.dry_run else ''}완료")
    print(f"  삭제 성공 : {deleted:,}개")
    print(f"  삭제 실패 : {failed:,}개")
    print(sep)

    client.close()


if __name__ == "__main__":
    main()