#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_tile_bbox.py
──────────────────
s100assets_metadata 컬렉션의 s111/s413 도큐먼트 중
잘못 저장된 tile.north/south/west/east 를 올바른 값으로 일괄 수정한다.

버그 원인:
  기존 compute_tile_bbox()가 포인트 인덱스 기반(ls-n+1, js+n-1)으로 경계를 계산해
  타일 크기가 실제 20°가 아닌 19.75°로 저장됨.
  → 각 타일의 south/east 가 0.25° 작게 기록됨.

수정 공식:
  north = 90.0  - row * 20
  south = max(90.0 - (row+1) * 20, -90)
  west  = -180.0 + col * 20
  east  = west + 20

[사용법]
  # dry-run (실제 수정 없이 변경 예정 목록만 출력)
  python patch_tile_bbox.py --dry-run

  # 실제 패치 실행
  python patch_tile_bbox.py

[환경변수]
  MONGO_URI      (기본: mongodb://localhost:27017)
  MONGO_DB       (기본: weather)
  MONGO_S100_COL (기본: s100assets_metadata)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from pymongo import MongoClient, UpdateOne

# ── 설정 ──────────────────────────────────────────────────────────────────────
MONGO_URI  = os.getenv("MONGO_URI",      "mongodb://localhost:27017")
MONGO_DB   = os.getenv("MONGO_DB",       "weather")
MONGO_COL  = os.getenv("MONGO_S100_COL", "s100assets_metadata")

TILE_DEG       = 20.0
TILES_PER_ROW  = 18   # 360 / 20
TOTAL_ROWS     =  9   # 180 / 20
TOTAL_TILES    = TILES_PER_ROW * TOTAL_ROWS   # 162

PRODUCTS = ("s111", "s413")
BATCH    = 500   # bulk_write 배치 크기

UTC = timezone.utc


# ── 올바른 bbox 계산 ──────────────────────────────────────────────────────────

def correct_bbox(tile_idx_str: str) -> dict | None:
    """
    tile.idx (zero-padded 문자열 '001'~'162') → 올바른 bbox dict.
    범위를 벗어나면 None 반환.
    """
    try:
        idx = int(tile_idx_str)
    except (ValueError, TypeError):
        return None

    if not (1 <= idx <= TOTAL_TILES):
        return None

    row = (idx - 1) // TILES_PER_ROW
    col = (idx - 1) %  TILES_PER_ROW

    north = round(90.0  - row * TILE_DEG, 4)
    south = round(max(90.0 - (row + 1) * TILE_DEG, -90.0), 4)
    west  = round(-180.0 + col * TILE_DEG, 4)
    east  = round(west  + TILE_DEG, 4)

    return {"north": north, "south": south, "west": west, "east": east}


# ── 패치 실행 ─────────────────────────────────────────────────────────────────

def run_patch(dry_run: bool = False) -> None:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        client.admin.command("ping")
    except Exception as e:
        print(f"[patch] ❌ MongoDB 연결 실패: {e}")
        sys.exit(1)

    col = client[MONGO_DB][MONGO_COL]
    now = datetime.now(tz=UTC)

    mode_label = "[DRY-RUN]" if dry_run else "[PATCH]"
    print(f"{mode_label} DB={MONGO_DB}  COL={MONGO_COL}  URI={MONGO_URI}")
    print(f"{mode_label} 대상 product: {PRODUCTS}")

    total_scanned = total_changed = total_skipped = total_error = 0
    ops: list[UpdateOne] = []

    cursor = col.find(
        {"product": {"$in": list(PRODUCTS)}},
        {"_id": 1, "product": 1, "tile": 1, "natural_key": 1},
    ).batch_size(BATCH)

    for doc in cursor:
        total_scanned += 1
        tile = doc.get("tile") or {}
        idx_str = tile.get("idx")

        if idx_str is None:
            total_skipped += 1
            continue

        new_bbox = correct_bbox(idx_str)
        if new_bbox is None:
            print(f"  ⚠️  tile.idx={idx_str!r} 범위 초과 → 스킵  (nk={doc.get('natural_key')})")
            total_error += 1
            continue

        # 현재 저장값과 비교 — 이미 올바르면 스킵
        already_ok = (
            tile.get("north") == new_bbox["north"] and
            tile.get("south") == new_bbox["south"] and
            tile.get("west")  == new_bbox["west"]  and
            tile.get("east")  == new_bbox["east"]
        )
        if already_ok:
            total_skipped += 1
            continue

        # 변경 필요
        total_changed += 1
        if dry_run:
            print(
                f"  → tile={idx_str}"
                f"  현재 N={tile.get('north')} S={tile.get('south')}"
                f" W={tile.get('west')} E={tile.get('east')}"
                f"  수정 N={new_bbox['north']} S={new_bbox['south']}"
                f" W={new_bbox['west']} E={new_bbox['east']}"
                f"  ({doc.get('product')})"
            )
        else:
            ops.append(UpdateOne(
                {"_id": doc["_id"]},
                {
                    "$set": {
                        "tile.north": new_bbox["north"],
                        "tile.south": new_bbox["south"],
                        "tile.west":  new_bbox["west"],
                        "tile.east":  new_bbox["east"],
                        "patched_at": now,
                    }
                },
            ))
            # 배치 flush
            if len(ops) >= BATCH:
                result = col.bulk_write(ops, ordered=False)
                print(f"  bulk_write {result.modified_count}건 수정")
                ops.clear()

    # 잔여 배치
    if ops and not dry_run:
        result = col.bulk_write(ops, ordered=False)
        print(f"  bulk_write {result.modified_count}건 수정 (잔여)")

    print()
    print(f"{mode_label} 완료 ─────────────────────────────")
    print(f"  전체 스캔  : {total_scanned:>6,}")
    print(f"  수정 대상  : {total_changed:>6,}")
    print(f"  이미 정상  : {total_skipped:>6,}")
    print(f"  오류/스킵  : {total_error:>6,}")
    if dry_run:
        print()
        print("  ※ dry-run 모드 — 실제 DB 변경 없음.")
        print("  ※ 실제 패치: python patch_tile_bbox.py")

    client.close()


# ── 진입점 ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="S-111/S-413 tile bbox 패치")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="실제 수정 없이 변경 대상 목록만 출력"
    )
    args = parser.parse_args()
    run_patch(dry_run=args.dry_run)