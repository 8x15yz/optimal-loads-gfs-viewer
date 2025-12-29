from datetime import datetime, timezone
from pymongo import MongoClient
import os

UTC = timezone.utc

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB  = os.getenv("MONGO_DB", "optimal_loads")
ASSETS_COL = os.getenv("MONGO_COL", "assets_metadata")
DIR_COL    = os.getenv("MONGO_DIR_COL", "directories")

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
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    return datetime.now(UTC)


def upsert_directories_from_doc(doc):
    inv_dir = _norm_dir(doc.get("inventory_directory"))
    if not inv_dir:
        return

    lm = _iso_to_dt(doc.get("created_at"))
    now = datetime.now(UTC)

    cur = inv_dir
    while cur:
        parent, name = _split_dir(cur)

        # 1️⃣ 현재 디렉토리
        result = dirs.update_one(
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

        if result.upserted_id:
            print(f"  📁 created dir: {cur}")

        # 2️⃣ 부모 디렉토리
        if parent:
            p_parent, p_name = _split_dir(parent)

            result_p = dirs.update_one(
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

            if result_p.upserted_id:
                print(f"  📂 created parent dir: {parent}")

        cur = parent




if __name__ == "__main__":
    print("▶ Backfilling directories from assets_metadata")

    cursor = assets.find(
        {"inventory_directory": {"$exists": True}},
        {"inventory_directory": 1, "created_at": 1}
    )

    count = 0
    for doc in cursor:
        upsert_directories_from_doc(doc)
        count += 1
        print(f"  processed {count} docs...", end="\r")
        if count % 1000 == 0:
            print(f"  processed {count} docs...")

    print(f"✅ DONE. processed {count} asset documents.")
