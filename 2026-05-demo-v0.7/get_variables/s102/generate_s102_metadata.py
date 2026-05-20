#!/usr/bin/env python3
"""
S-102 (GEBCO 2025) 타일 메타데이터 생성 스크립트

23행 × 45열 = 1,035 타일
S3 경로: gebco/s102/2025/Split/102KRTDGEBCO_2025_{n}.h5
MongoDB 컬렉션: s100assets_metadata

Usage:
  python generate_s102_metadata.py               # dry-run
  python generate_s102_metadata.py --execute     # 실제 upsert
  python generate_s102_metadata.py --execute --skip-s3-check   # S3 조회 생략
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from pymongo import MongoClient, ASCENDING

# ── 환경변수 ───────────────────────────────────────────────────
MONGO_URI  = os.environ.get("MONGO_URI")
MONGO_DB   = os.environ.get("MONGO_DB",       "optimal_loads")
MONGO_S100 = os.environ.get("MONGO_S100_COL", "s100assets_metadata")
S3_BUCKET  = os.environ.get("S3_BUCKET",      "optimal-loads")
AWS_REGION = os.environ.get("AWS_REGION",     "ap-northeast-2")

# ── S-102 타일 그리드 (api.py와 동일) ──────────────────────────
_ROW_SOUTH = [
     82.0021,  74.0021,  66.0021,  58.0021,  50.0021,  42.0021,  34.0021,
     26.0021,  18.0021,  10.0021,   2.0021,  -5.9979, -13.9979, -21.9979,
    -29.9979, -37.9979, -45.9979, -53.9979, -61.9979, -69.9979, -77.9979,
    -85.9979, -89.9979,
]
_ROW_NORTH = [89.9979] + _ROW_SOUTH[:-1]
_COL_WEST  = [-179.9979 + i * 8.0 for i in range(45)]
_COL_EAST  = [-179.9979 + (i + 1) * 8.0 for i in range(45)]

S3_PREFIX  = "gebco/s102/2025/Split"
YEAR       = 2025
UTC        = timezone.utc


# ── 타일 목록 생성 ──────────────────────────────────────────────
def iter_tiles():
    """(tile_number, s3_key, tile_bbox) 순서로 1035개 yield"""
    for r in range(23):
        for c in range(45):
            n = r * 45 + c + 1
            s3_key = f"{S3_PREFIX}/102KRTDGEBCO_2025_{n}.h5"
            tile_bbox = {
                "idx":   f"{n:04d}",
                "north": round(_ROW_NORTH[r], 4),
                "south": round(_ROW_SOUTH[r], 4),
                "west":  round(_COL_WEST[c],  4),
                "east":  round(_COL_EAST[c],  4),
            }
            yield n, s3_key, tile_bbox


# ── S3 파일 크기 조회 ────────────────────────────────────────────
def get_s3_size(s3_client, key: str) -> int | None:
    try:
        resp = s3_client.head_object(Bucket=S3_BUCKET, Key=key)
        return resp["ContentLength"]
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("404", "NoSuchKey"):
            return None
        raise


# ── 메타 문서 빌더 ───────────────────────────────────────────────
def build_s102_doc(tile_bbox: dict, s3_key: str, size_bytes: int | None) -> dict:
    n = int(tile_bbox["idx"])
    natural_key = f"s102|bathymetry|year={YEAR}|tile={tile_bbox['idx']}"
    return {
        "natural_key":  natural_key,
        "product":      "s102",
        "source":       "gebco",
        "type":         "bathymetry",
        "year":         YEAR,
        "tile":         tile_bbox,
        "variables": {
            "stored": ["depth"],
        },
        "s3": {
            "bucket": S3_BUCKET,
            "region": AWS_REGION,
            "key":    s3_key,
        },
        "size_bytes":   size_bytes,
        "format":       "hdf5",
        "content_type": "application/x-hdf5",
        "created_at":   datetime.now(UTC),
    }


# ── 인덱스 보장 ──────────────────────────────────────────────────
def ensure_indexes(col):
    col.create_index("natural_key", unique=True)
    col.create_index([("product", ASCENDING), ("tile.idx", ASCENDING)])


# ── main ─────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="S-102 메타데이터 생성")
    parser.add_argument("--execute",       action="store_true", help="실제 upsert 실행")
    parser.add_argument("--skip-s3-check", action="store_true", help="S3 파일 크기 조회 생략")
    args = parser.parse_args()

    dry_run = not args.execute

    print("=" * 54)
    print("  S-102 (GEBCO 2025) 메타데이터 생성")
    print(f"  모드   : {'DRY-RUN' if dry_run else '★ EXECUTE ★'}")
    print(f"  S3     : s3://{S3_BUCKET}/{S3_PREFIX}/")
    print(f"  MongoDB: {MONGO_DB}.{MONGO_S100}")
    print(f"  S3 조회: {'생략' if args.skip_s3_check else '실행'}")
    print("=" * 54)

    # S3 클라이언트
    s3_client = boto3.client("s3", region_name=AWS_REGION)

    # MongoDB
    if not dry_run:
        if not MONGO_URI:
            print("[ERROR] MONGO_URI 환경변수가 없습니다.", file=sys.stderr)
            sys.exit(1)
        client = MongoClient(MONGO_URI)
        col = client[MONGO_DB][MONGO_S100]
        ensure_indexes(col)

    ok = skip = fail = 0

    for n, s3_key, tile_bbox in iter_tiles():
        # S3 크기 조회
        if args.skip_s3_check:
            size_bytes = None
        else:
            size_bytes = get_s3_size(s3_client, s3_key)
            if size_bytes is None:
                print(f"  [SKIP] tile={n:04d} — S3 파일 없음: {s3_key}")
                skip += 1
                continue

        doc = build_s102_doc(tile_bbox, s3_key, size_bytes)

        if dry_run:
            size_str = f"{size_bytes:,}" if size_bytes else "?"
            print(f"  [DRY] tile={n:04d}  bbox=({tile_bbox['west']:.2f},{tile_bbox['south']:.4f},{tile_bbox['east']:.2f},{tile_bbox['north']:.4f})  size={size_str}B")
            ok += 1
        else:
            try:
                col.update_one(
                    {"natural_key": doc["natural_key"]},
                    {"$set": doc},
                    upsert=True,
                )
                print(f"  [OK] tile={n:04d}  {s3_key}")
                ok += 1
            except Exception as e:
                print(f"  [ERROR] tile={n:04d}: {e}", file=sys.stderr)
                fail += 1

    print()
    print(f"  완료 — OK: {ok}  SKIP: {skip}  FAIL: {fail}  합계: {ok+skip+fail}")

    if not dry_run:
        client.close()


if __name__ == "__main__":
    main()
