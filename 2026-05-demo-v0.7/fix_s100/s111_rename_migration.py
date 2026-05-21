#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# s111_rename_migration.py
# S3의 s111/ 경로 아래 _1.h5, _2.h5 ... 형식 파일을 _001.h5, _002.h5 ... 로 rename
# MongoDB assets_metadata 컬렉션의 natural_key, s3.key, tile.idx 도 함께 업데이트
#
# 환경변수:
#   MONGO_URI, MONGO_DB, MONGO_S100_COL
#   AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
#   S3_BUCKET (기본값: optimal-loads)
#
# 사용법:
#   python s111_rename_migration.py           # dry-run (기본값)
#   python s111_rename_migration.py --apply   # 실제 적용

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Optional

import boto3
from pymongo import MongoClient

# ── 환경변수 ───────────────────────────────────────────────────────────────────
MONGO_URI  = os.environ["MONGO_URI"]
MONGO_DB   = os.environ.get("MONGO_DB",   "optimal_loads")
MONGO_S100_COL = os.environ.get("MONGO_S100_COL", "s100assets_metadata")
S3_BUCKET  = os.environ.get("S3_BUCKET",  "optimal-loads")
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-2")

S111_PREFIX = "s111/"

# _숫자.h5 로 끝나는 파일 (zero-pad 안 된 것만)
# 예: 11120260520_18Z_1.h5  → match
#     11120260520_18Z_001.h5 → no match (이미 3자리)
NEEDS_RENAME = re.compile(r"^(.+_)(\d{1,2})\.h5$")


def new_key(old_key: str) -> Optional[str]:
    """old_key 에서 suffix를 zero-pad 3자리로 바꾼 새 key 반환. 변경 불필요하면 None."""
    basename = old_key.split("/")[-1]
    m = NEEDS_RENAME.match(basename)
    if not m:
        return None
    prefix_part = "/".join(old_key.split("/")[:-1])
    new_basename = f"{m.group(1)}{int(m.group(2)):03d}.h5"
    return f"{prefix_part}/{new_basename}"


def list_s111_objects(s3) -> list[dict]:
    """s111/ 아래 모든 오브젝트 목록 반환."""
    paginator = s3.get_paginator("list_objects_v2")
    objects = []
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=S111_PREFIX):
        for obj in page.get("Contents", []):
            objects.append(obj)
    return objects


def main() -> None:
    ap = argparse.ArgumentParser(
        description="S-111 S3+MongoDB 파일명 zero-pad 정규화 마이그레이션"
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="실제 변경 적용 (없으면 dry-run)",
    )
    args = ap.parse_args()
    dry = not args.apply

    mode_label = "🔍 DRY-RUN" if dry else "✅ APPLY"
    print(f"[migration] 모드: {mode_label}")
    print(f"[migration] S3 버킷: {S3_BUCKET}  prefix: {S111_PREFIX}")
    print(f"[migration] MongoDB: {MONGO_DB}.{MONGO_S100_COL}")
    print()

    # ── 클라이언트 초기화 ──────────────────────────────────────────────────────
    s3 = boto3.client("s3", region_name=AWS_REGION)
    mongo = MongoClient(MONGO_URI)
    col = mongo[MONGO_DB][MONGO_S100_COL]

    # ── S3 오브젝트 목록 ───────────────────────────────────────────────────────
    print("[migration] S3 오브젝트 목록 조회 중...")
    all_objects = list_s111_objects(s3)
    print(f"[migration] 전체 s111/ 오브젝트 수: {len(all_objects)}")

    targets = []
    for obj in all_objects:
        old = obj["Key"]
        nk = new_key(old)
        if nk:
            targets.append((old, nk))

    print(f"[migration] rename 대상: {len(targets)}개\n")

    if not targets:
        print("[migration] rename 대상 없음 — 종료")
        return

    # ── 대상 목록 출력 ─────────────────────────────────────────────────────────
    for old, nk in targets:
        print(f"  {old}")
        print(f"    → {nk}")

    print()

    if dry:
        print("[migration] ⚠️  dry-run 모드 — 실제 변경 없음")
        print("[migration] 적용하려면:  python s111_rename_migration.py --apply")
        return

    # ── 실제 적용 ──────────────────────────────────────────────────────────────
    s3_ok = s3_fail = 0
    mongo_ok = mongo_fail = mongo_skip = 0

    for old_key_str, new_key_str in targets:
        old_basename = old_key_str.split("/")[-1]
        new_basename = new_key_str.split("/")[-1]

        # 1) S3 copy
        try:
            s3.copy_object(
                Bucket=S3_BUCKET,
                CopySource={"Bucket": S3_BUCKET, "Key": old_key_str},
                Key=new_key_str,
                ContentType="application/x-hdf5",
                MetadataDirective="REPLACE",
            )
        except Exception as e:
            print(f"[s3] ❌ copy 실패: {old_key_str} → {e}")
            s3_fail += 1
            continue

        # 2) S3 delete 원본
        try:
            s3.delete_object(Bucket=S3_BUCKET, Key=old_key_str)
        except Exception as e:
            print(f"[s3] ⚠️  delete 실패 (copy는 성공): {old_key_str} → {e}")
            # copy는 됐으니 MongoDB는 계속 진행

        s3_ok += 1
        print(f"[s3] ✅ {old_basename} → {new_basename}")

        # 3) MongoDB 업데이트
        # natural_key: "s111|forecast|run=...|tile=001" 형식
        # s3.key: old_key_str → new_key_str
        # tile.idx: "1" → "001"  (또는 이미 int인 경우도 대응)
        old_tile_num_m = re.search(r"_(\d{1,2})\.h5$", old_basename)
        if not old_tile_num_m:
            mongo_skip += 1
            continue
        tile_int = int(old_tile_num_m.group(1))
        tile_str_old = str(tile_int)           # "1", "2" ...
        tile_str_new = f"{tile_int:03d}"       # "001", "002" ...

        # natural_key 패턴: s111|forecast|run=...|tile=1  (또는 tile=001 이미 된 것)
        filter_q = {"s3.key": old_key_str}

        update_q = {
            "$set": {
                "s3.key": new_key_str,
                "tile.idx": tile_str_new,
            }
        }

        # natural_key 내부 tile= 부분도 교체
        doc = col.find_one(filter_q)
        if doc:
            old_nk = doc.get("natural_key", "")
            # tile= 뒤 숫자를 zero-pad로 교체
            new_nk = re.sub(
                r"(\|tile=)(\d+)$",
                lambda m2: f"{m2.group(1)}{int(m2.group(2)):03d}",
                old_nk,
            )
            update_q["$set"]["natural_key"] = new_nk

            try:
                result = col.update_one(filter_q, update_q)
                if result.modified_count:
                    mongo_ok += 1
                    print(f"[mongo] ✅ tile={tile_str_new} key updated")
                else:
                    mongo_skip += 1
                    print(f"[mongo] ⚠️  변경 없음 (이미 최신?): {old_key_str}")
            except Exception as e:
                print(f"[mongo] ❌ 업데이트 실패: {old_key_str} → {e}")
                mongo_fail += 1
        else:
            print(f"[mongo] ⚠️  문서 없음 (S3만 rename됨): {old_key_str}")
            mongo_skip += 1

    print()
    print(f"[migration] S3    — 성공: {s3_ok}, 실패: {s3_fail}")
    print(f"[migration] Mongo — 성공: {mongo_ok}, 실패: {mongo_fail}, 스킵: {mongo_skip}")
    print("[migration] 완료")


if __name__ == "__main__":
    main()