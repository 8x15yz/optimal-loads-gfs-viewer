#!/usr/bin/env python3
"""
S-111 / S-413 타일 전체 삭제 스크립트
  - MongoDB s100assets_metadata 에서 product in ["s111","s413"] 문서 삭제
  - S3 에서 s111/, s413/ 프리픽스 오브젝트 전체 삭제

기본 동작: DRY-RUN (삭제 대상만 출력)
실제 삭제: --execute 플래그 필요

Usage:
  python cleanup_s100_tiles.py               # dry-run
  python cleanup_s100_tiles.py --execute     # 실제 삭제
"""
from __future__ import annotations

import argparse
import os
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from pymongo import MongoClient

# ── 환경 변수 ──────────────────────────────────────────────
MONGO_URI    = os.environ.get("MONGO_URI")
MONGO_DB     = os.environ.get("MONGO_DB",      "optimal_loads")
MONGO_S100   = os.environ.get("MONGO_S100_COL","s100assets_metadata")
S3_BUCKET    = os.environ.get("S3_BUCKET",     "optimal-loads")
AWS_REGION   = os.environ.get("AWS_REGION",    "ap-northeast-2")

PRODUCTS     = ["s111", "s413"]
S3_PREFIXES  = ["s111/", "s413/"]

# ── S3 삭제 ───────────────────────────────────────────────
def s3_list_all(s3, prefix: str) -> list[str]:
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def s3_delete_batch(s3, keys: list[str], dry_run: bool) -> int:
    if not keys:
        return 0
    if dry_run:
        for k in keys:
            print(f"  [S3 DRY] {k}")
        return len(keys)

    deleted = 0
    for i in range(0, len(keys), 1000):
        batch = keys[i:i+1000]
        resp = s3.delete_objects(
            Bucket=S3_BUCKET,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
        errors = resp.get("Errors", [])
        if errors:
            for e in errors:
                print(f"  [S3 ERROR] {e['Key']}: {e['Message']}", file=sys.stderr)
        deleted += len(batch) - len(errors)
    return deleted


def run_s3(dry_run: bool):
    print("\n── S3 ──────────────────────────────────────────")
    s3 = boto3.client("s3", region_name=AWS_REGION)
    total = 0
    for prefix in S3_PREFIXES:
        print(f"\n  prefix: {prefix}")
        keys = s3_list_all(s3, prefix)
        print(f"  대상 오브젝트: {len(keys)}개")
        n = s3_delete_batch(s3, keys, dry_run)
        total += n
        if not dry_run:
            print(f"  삭제 완료: {n}개")
    print(f"\n  S3 합계: {total}개 {'(dry-run)' if dry_run else '삭제 완료'}")


# ── MongoDB 삭제 ──────────────────────────────────────────
def run_mongo(dry_run: bool):
    print("\n── MongoDB ─────────────────────────────────────")
    if not MONGO_URI:
        print("  [ERROR] MONGO_URI 환경변수가 없습니다.", file=sys.stderr)
        sys.exit(1)

    client = MongoClient(MONGO_URI)
    col = client[MONGO_DB][MONGO_S100]

    query = {"product": {"$in": PRODUCTS}}
    count = col.count_documents(query)
    print(f"  컬렉션: {MONGO_DB}.{MONGO_S100}")
    print(f"  대상 문서: {count}개  (product in {PRODUCTS})")

    if dry_run:
        print("  [MONGO DRY] 삭제 스킵")
    else:
        result = col.delete_many(query)
        print(f"  삭제 완료: {result.deleted_count}개")

    client.close()


# ── main ──────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="S-111/S-413 타일 전체 삭제")
    parser.add_argument("--execute", action="store_true", help="실제 삭제 실행 (없으면 dry-run)")
    args = parser.parse_args()

    dry_run = not args.execute

    print("=" * 52)
    print("  S-111 / S-413 타일 삭제 스크립트")
    print(f"  모드: {'DRY-RUN (아무것도 삭제 안 함)' if dry_run else '★ EXECUTE — 실제 삭제 ★'}")
    print(f"  S3 버킷: {S3_BUCKET}")
    print(f"  MongoDB DB: {MONGO_DB}  컬렉션: {MONGO_S100}")
    print("=" * 52)

    if not dry_run:
        confirm = input("\n정말 삭제하시겠습니까? (yes 입력): ").strip()
        if confirm != "yes":
            print("취소됨.")
            sys.exit(0)

    try:
        run_s3(dry_run)
    except (BotoCoreError, ClientError) as e:
        print(f"\n[S3 ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    run_mongo(dry_run)

    print("\n완료.")


if __name__ == "__main__":
    main()
