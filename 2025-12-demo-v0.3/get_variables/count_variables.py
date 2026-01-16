import os
from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB  = os.getenv("MONGO_DB", "optimal_loads")
DIR_COL   = os.getenv("MONGO_DIR_COL", "directories")

DRY_RUN = False  # ✅ 먼저 True로 돌리고, 확인 후 False로

if not MONGO_URI:
    raise RuntimeError("MONGO_URI env not set")

client = MongoClient(MONGO_URI)
coll = client[MONGO_DB][DIR_COL]

# === 삭제할 "서브트리 루트" (스샷 기준) ===
SUBTREE = "ecmwf/ifs/2025/2025-04/"     # 이 아래 전부 삭제
PARENT  = "ecmwf/ifs/2025/"            # 여기서 children_dirs의 "2025-04/"를 제거
CHILD_NAME = "2025-04/"                # 부모 children_dirs에 들어있는 값

# 1) 서브트리 문서 찾기: _id 또는 dir가 SUBTREE로 시작하는 것들
query_subtree = {
    "$or": [
        {"_id": {"$regex": f"^{SUBTREE}"}},
        {"dir": {"$regex": f"^{SUBTREE}"}},
    ]
}

count_subtree = coll.count_documents(query_subtree)
print(f"[directories] subtree docs under {SUBTREE} = {count_subtree}")
print(f"DRY_RUN={DRY_RUN}")

print("\n--- sample subtree docs (up to 10) ---")
for doc in coll.find(query_subtree, {"_id": 1, "dir": 1, "children_dirs": 1}).limit(10):
    print({
        "_id": doc.get("_id"),
        "dir": doc.get("dir"),
        "children_dirs_len": len(doc.get("children_dirs", []))
    })

# 2) 부모 노드에 CHILD_NAME이 실제로 들어있는지 확인
parent_doc = coll.find_one({"_id": PARENT}, {"_id": 1, "children_dirs": 1})
if not parent_doc:
    print(f"\n⚠️ parent doc not found: {PARENT}")
else:
    hits = [x for x in parent_doc.get("children_dirs", []) if x == CHILD_NAME]
    print(f"\n[parent check] {PARENT} has CHILD_NAME hits = {hits}")

if DRY_RUN:
    print("\n(DRY_RUN=True) No changes applied.")
    raise SystemExit(0)

# 3) 부모 children_dirs에서 "2025-04/" 제거
res_pull = coll.update_one(
    {"_id": PARENT},
    {"$pull": {"children_dirs": CHILD_NAME}}
)
print(f"\n✅ parent pull modified = {res_pull.modified_count}")

# 4) 서브트리 문서들 삭제
res_del = coll.delete_many(query_subtree)
print(f"✅ subtree deleted = {res_del.deleted_count}")

# 5) 사후 검증
after_subtree = coll.count_documents(query_subtree)
print(f"\n[directories] remaining subtree docs = {after_subtree}")

parent_doc2 = coll.find_one({"_id": PARENT}, {"_id": 1, "children_dirs": 1})
if parent_doc2:
    still = [x for x in parent_doc2.get("children_dirs", []) if x == CHILD_NAME]
    print(f"[parent check] remaining CHILD_NAME hits = {still}")
