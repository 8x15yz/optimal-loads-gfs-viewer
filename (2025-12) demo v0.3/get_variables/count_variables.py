import os
from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB  = os.getenv("MONGO_DB", "optimal_loads")
DIR_COL   = os.getenv("MONGO_DIR_COL", "directories")

DRY_RUN = False

if not MONGO_URI:
    raise RuntimeError("MONGO_URI env not set")

client = MongoClient(MONGO_URI)
coll = client[MONGO_DB][DIR_COL]

# children_dirs 배열에 들어있는 값이 정확히 'pp1d/' 인 케이스
query = {"children_dirs": "pp1d/"}

before = coll.count_documents(query)
print(f"[directories] docs having 'pp1d/' in children_dirs = {before}")
print(f"DRY_RUN={DRY_RUN}")

# 샘플 확인
print("\n--- sample before ---")
for doc in coll.find(query, {"_id": 0, "dir": 1, "children_dirs": 1}).limit(5):
    hits = [x for x in doc.get("children_dirs", []) if x == "pp1d/"]
    print("DIR:", doc.get("dir"))
    print("HITS:", hits)

if before == 0:
    print("Nothing to update.")
    raise SystemExit(0)

if DRY_RUN:
    print("\n(DRY_RUN=True, not updating)")
else:
    res = coll.update_many(
        query,
        {"$pull": {"children_dirs": "pp1d/"}}
    )
    print(f"\n✅ modified = {res.modified_count}")

after = coll.count_documents(query)
print(f"[directories] remaining docs with 'pp1d/' = {after}")
