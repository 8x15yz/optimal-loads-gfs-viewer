"""
Delete all data for the 2026-05-18 18Z NOAA GFS runtime:
  - assets_metadata    : grib2 원본 메타 + S3 grib2 파일
  - s100assets_metadata: s111/s413 타일 메타 + S3 h5 파일
  - directories        : s111/s413 leaf 경로 항목
  - ingestion_runs     : 변수별 run 로그 (재수집 시 fresh start)
  - ingestion_control  : running=True stuck 상태 리셋

Environment variables required:
    MONGO_URI, MONGO_DB, MONGO_COLL
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION

Usage:
    python delete_run_18z.py          # dry-run
    python delete_run_18z.py --delete  # 실제 삭제
"""

import os
import sys
from datetime import datetime, timezone

import boto3
from pymongo import MongoClient

TARGET_RUN_TIME_STR = "2026-05-18T18:00:00Z"
S3_BUCKET = "optimal-loads"
CONTROL_DOC_ID = "noaa_gfs_ingestion"

# S3에서 list_objects로 긁을 h5 경로 prefix
S100_S3_PREFIXES = [
    "s111/2026/05/18/18Z/",
    "s413/2026/05/18/18Z/",
]

# directories 컬렉션에서 지울 leaf 경로
S100_DIR_LEAVES = [
    "s111/2026/05/18/18Z/",
    "s413/2026/05/18/18Z/",
]

DRY_RUN = "--delete" not in sys.argv


def get_db():
    return MongoClient(os.environ["MONGO_URI"])[os.environ["MONGO_DB"]]


def get_s3():
    return boto3.client(
        "s3",
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-2"),
    )


def list_s3_keys(s3, prefix: str) -> list[dict]:
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append({"Key": obj["Key"]})
    return keys


def batch_delete_s3(s3, objects: list[dict]) -> int:
    deleted = 0
    for i in range(0, len(objects), 1000):
        batch = objects[i : i + 1000]
        resp = s3.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": batch, "Quiet": False})
        deleted += len(resp.get("Deleted", []))
        for err in resp.get("Errors", []):
            print(f"  S3 error: {err}")
    return deleted


def main():
    mode = "DRY-RUN" if DRY_RUN else "DELETE"
    print(f"[{mode}] Target: NOAA GFS run_time_utc = {TARGET_RUN_TIME_STR}")
    print()

    db = get_db()
    assets_col  = db[os.environ["MONGO_COLL"]]
    s100_col    = db["s100assets_metadata"]
    dirs_col    = db["directories"]
    runs_col    = db["ingestion_runs"]
    control_col = db["ingestion_control"]

    run_query = {"source": "noaa", "run_time_utc": TARGET_RUN_TIME_STR}
    s100_query = {"run_time_utc": TARGET_RUN_TIME_STR}

    # ── 1. assets_metadata (grib2) ──────────────────────────────────
    asset_docs = list(assets_col.find(run_query, {"_id": 1, "s3": 1}))
    grib_s3 = [{"Key": d["s3"]["key"]} for d in asset_docs if d.get("s3", {}).get("key")]
    print(f"[assets_metadata]    docs={len(asset_docs)}  S3={len(grib_s3)}")

    # ── 2. s100assets_metadata (h5) ─────────────────────────────────
    s100_docs = list(s100_col.find(s100_query, {"_id": 1, "product": 1, "s3": 1}))
    h5_s3_from_mongo = [{"Key": d["s3"]["key"]} for d in s100_docs if d.get("s3", {}).get("key")]
    print(f"[s100assets_metadata] docs={len(s100_docs)}  S3={len(h5_s3_from_mongo)}")

    # ── 3. S3 h5 파일 (list로 재확인) ───────────────────────────────
    s3 = get_s3()
    h5_s3_listed = []
    for prefix in S100_S3_PREFIXES:
        found = list_s3_keys(s3, prefix)
        print(f"  S3 list [{prefix}] → {len(found)}개")
        h5_s3_listed.extend(found)

    # mongo + list 합집합 (Key 기준 dedup)
    h5_keys_all = {o["Key"]: o for o in h5_s3_from_mongo + h5_s3_listed}
    h5_s3_objects = list(h5_keys_all.values())
    print(f"  S3 h5 total (dedup): {len(h5_s3_objects)}")

    # ── 4. directories leaf ──────────────────────────────────────────
    dir_docs = list(dirs_col.find({"_id": {"$in": S100_DIR_LEAVES}}, {"_id": 1}))
    print()
    print(f"[directories]        leaf docs={len(dir_docs)}")
    for d in dir_docs:
        print(f"  {d['_id']}")

    # ── 5. ingestion_runs ────────────────────────────────────────────
    runs_docs = list(runs_col.find(run_query, {"_id": 1, "status": 1}))
    print()
    print(f"[ingestion_runs]     docs={len(runs_docs)}")
    for d in runs_docs:
        print(f"  {d['_id']}  status={d.get('status')}")

    # ── 6. ingestion_control ─────────────────────────────────────────
    ctl = control_col.find_one({"_id": CONTROL_DOC_ID}, {"running": 1, "status": 1})
    print()
    if ctl:
        print(f"[ingestion_control]  running={ctl.get('running')}  status={ctl.get('status')}")
        if ctl.get("running"):
            print("  ⚠️  running=True → --delete 시 False로 리셋")
    else:
        print(f"[ingestion_control]  doc not found")

    # ── dry-run 종료 ─────────────────────────────────────────────────
    if DRY_RUN:
        print()
        print("-- DRY RUN: no changes made. Pass --delete to actually delete. --")
        return

    # ── 실제 삭제 ────────────────────────────────────────────────────
    print()

    # S3 grib2
    n = batch_delete_s3(s3, grib_s3)
    print(f"S3 grib2 deleted     : {n}")

    # S3 h5
    n = batch_delete_s3(s3, h5_s3_objects)
    print(f"S3 h5 deleted        : {n}")

    # assets_metadata
    print(f"assets_metadata del  : {assets_col.delete_many(run_query).deleted_count}")

    # s100assets_metadata
    print(f"s100assets_meta del  : {s100_col.delete_many(s100_query).deleted_count}")

    # directories leaf 삭제 + 부모 children_dirs에서 제거
    for leaf in S100_DIR_LEAVES:
        dirs_col.delete_one({"_id": leaf})
        # leaf 이름만 추출 (e.g. "18Z/") 해서 부모에서 제거
        name = leaf.rstrip("/").split("/")[-1] + "/"
        parent = "/".join(leaf.rstrip("/").split("/")[:-1]) + "/"
        dirs_col.update_one({"_id": parent}, {"$pull": {"children_dirs": name}})
    print(f"directories leaf del : {len(S100_DIR_LEAVES)}")

    # ingestion_runs
    print(f"ingestion_runs del   : {runs_col.delete_many(run_query).deleted_count}")

    # ingestion_control
    if ctl and ctl.get("running"):
        control_col.update_one(
            {"_id": CONTROL_DOC_ID},
            {"$set": {
                "running": False,
                "status": "idle",
                "error": "manual reset by delete_run_18z.py",
                "updated_at": datetime.now(timezone.utc),
            }},
        )
        print("ingestion_control    : running → False (reset)")
    else:
        print("ingestion_control    : no reset needed")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
