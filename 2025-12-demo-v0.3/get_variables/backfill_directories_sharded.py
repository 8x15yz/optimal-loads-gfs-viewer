# backfill_directories_sharded.py
from __future__ import annotations

from datetime import datetime, timezone
from pymongo import MongoClient
from pymongo.errors import AutoReconnect, NetworkTimeout, CursorNotFound
from bson import ObjectId
import argparse
import os
import sys
import time

UTC = timezone.utc

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB  = os.getenv("MONGO_DB", "optimal_loads")
ASSETS_COL = os.getenv("MONGO_COL", "assets_metadata")
DIR_COL    = os.getenv("MONGO_DIR_COL", "directories")

if not MONGO_URI:
    raise RuntimeError("MONGO_URI env not set")

mongo = MongoClient(MONGO_URI)
assets = mongo[MONGO_DB][ASSETS_COL]
dirs   = mongo[MONGO_DB][DIR_COL]


def _norm_dir(d: str) -> str:
    d = (d or "").strip().lstrip("/")
    if d and not d.endswith("/"):
        d += "/"
    return d


def _split_dir(d: str):
    d = _norm_dir(d)
    parts = [p for p in d.strip("/").split("/") if p]
    if not parts:
        return None, ""
    name = parts[-1]
    parent = "/".join(parts[:-1]) + "/" if len(parts) > 1 else None
    return parent, name


def _iso_to_dt(v):
    if isinstance(v, datetime):
        return v.astimezone(UTC)
    if isinstance(v, str):
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(UTC)
    return datetime.now(UTC)


def _oid_mod_shard(oid: ObjectId, num_shards: int) -> int:
    # 끝 8자리(32bit)로 mod 샤딩
    hx = str(oid)
    return int(hx[-8:], 16) % num_shards


def upsert_directories_from_doc(doc):
    inv_dir = _norm_dir(doc.get("inventory_directory"))
    if not inv_dir:
        return

    lm = _iso_to_dt(doc.get("created_at"))
    now = datetime.now(UTC)

    cur = inv_dir
    while cur:
        parent, name = _split_dir(cur)

        # 현재 디렉토리 upsert
        dirs.update_one(
            {"_id": cur},
            {
                "$setOnInsert": {
                    "dir": cur,
                    "parent": parent,
                    "name": name,
                    "created_at": now,
                },
                "$max": {"last_modified": lm},
                "$set": {"updated_at": now},
            },
            upsert=True,
        )

        # 부모 디렉토리: children_dirs 연결
        if parent:
            p_parent, p_name = _split_dir(parent)
            dirs.update_one(
                {"_id": parent},
                {
                    "$setOnInsert": {
                        "dir": parent,
                        "parent": p_parent,
                        "name": p_name,
                        "created_at": now,
                    },
                    "$addToSet": {"children_dirs": name + "/"},
                    "$max": {"last_modified": lm},
                    "$set": {"updated_at": now},
                },
                upsert=True,
            )

        cur = parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--num_shards", type=int, default=1)
    ap.add_argument("--page_size", type=int, default=1000, help="page size for _id pagination")
    ap.add_argument("--log_every", type=int, default=1000)
    ap.add_argument("--retry", type=int, default=5)
    ap.add_argument("--start_after", type=str, default="", help="resume after this ObjectId hex (exclusive)")
    args = ap.parse_args()

    if args.num_shards < 1:
        raise ValueError("--num_shards must be >= 1")
    if not (0 <= args.shard < args.num_shards):
        raise ValueError("--shard must be in [0, num_shards-1]")

    start_after_oid = None
    if args.start_after:
        start_after_oid = ObjectId(args.start_after)

    print("▶ Backfilling directories from assets_metadata (Atlas-safe pagination)")
    print(f"  shard={args.shard} / num_shards={args.num_shards}")
    print(f"  page_size={args.page_size}, log_every={args.log_every}")
    if start_after_oid:
        print(f"  start_after={start_after_oid}")

    base_query = {"inventory_directory": {"$exists": True}}
    projection = {"inventory_directory": 1, "created_at": 1}

    processed = 0
    last_seen = start_after_oid

    attempt = 0
    while True:
        try:
            attempt = 0  # 배치 성공하면 attempt 초기화

            q = dict(base_query)
            if last_seen is not None:
                q["_id"] = {"$gt": last_seen}

            batch = list(
                assets.find(q, projection)
                      .sort("_id", 1)
                      .limit(args.page_size)
            )
            if not batch:
                break

            for doc in batch:
                oid = doc["_id"]
                last_seen = oid  # 다음 페이지 기준점

                if args.num_shards > 1:
                    if _oid_mod_shard(oid, args.num_shards) != args.shard:
                        continue

                upsert_directories_from_doc(doc)
                processed += 1

                if processed % args.log_every == 0:
                    print(
                        f"[shard {args.shard}] processed={processed:,} "
                        f"last_id={oid} last_dir={doc.get('inventory_directory')}"
                    )

        except (AutoReconnect, NetworkTimeout, CursorNotFound) as e:
            attempt += 1
            if attempt > args.retry:
                print(f"\n❌ ERROR: {type(e).__name__}: {e}", file=sys.stderr)
                print(f"   Resume hint: --start_after {last_seen}", file=sys.stderr)
                raise
            wait_s = min(5 * attempt, 30)
            print(
                f"\n⚠️ {type(e).__name__}: {e} -> retry {attempt}/{args.retry} after {wait_s}s\n"
                f"   Resume hint: --start_after {last_seen}",
                file=sys.stderr
            )
            time.sleep(wait_s)

    print(f"✅ DONE. shard {args.shard} processed {processed:,} docs.")
    if last_seen:
        print(f"   Resume hint: --start_after {last_seen}")


if __name__ == "__main__":
    main()
