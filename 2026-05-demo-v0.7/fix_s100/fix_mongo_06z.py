#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# fix_mongo_06z.py
# S3는 이미 rename 완료됐으나 MongoDB s3.key가 old 형식인 06Z 문서들만 업데이트

import os
import re
from pymongo import MongoClient

MONGO_URI  = os.environ.get("MONGO_URI",  "")
MONGO_DB   = os.environ.get("MONGO_DB",   "optimal_loads")
MONGO_S100_COL = os.environ.get("MONGO_S100_COL", "s100assets_metadata")

client = MongoClient(MONGO_URI)
col = client[MONGO_DB][MONGO_S100_COL]

print(f"=== DB: {MONGO_DB}  COL: {MONGO_S100_COL} ===\n")

# s111/2026/05/20/06Z/ 아래 s3.key가 _숫자(1~2자리).h5 로 끝나는 문서 찾기
OLD_KEY_PATTERN = re.compile(r"^s111/2026/05/20/06Z/.+_(\d{1,2})\.h5$")

docs = list(col.find({
    "product": "s111",
    "run_time_utc": "2026-05-20T06:00:00Z",
}))

print(f"06Z s111 문서 수: {len(docs)}")

to_fix = []
for doc in docs:
    old_key = doc.get("s3", {}).get("key", "")
    if OLD_KEY_PATTERN.match(old_key):
        to_fix.append(doc)

print(f"수정 필요한 문서 수: {len(to_fix)}\n")

if not to_fix:
    print("✅ 수정할 문서 없음 — 이미 모두 최신 상태입니다.")
    exit(0)

for doc in to_fix:
    old_key = doc["s3"]["key"]
    # _숫자.h5 → _NNN.h5
    new_key = re.sub(
        r"_(\d{1,2})\.h5$",
        lambda m: f"_{int(m.group(1)):03d}.h5",
        old_key,
    )
    old_nk = doc.get("natural_key", "")
    new_nk = re.sub(
        r"(\|tile=)(\d+)$",
        lambda m: f"{m.group(1)}{int(m.group(2)):03d}",
        old_nk,
    )

    print(f"  s3.key: {old_key}")
    print(f"       → {new_key}")

    result = col.update_one(
        {"_id": doc["_id"]},
        {"$set": {
            "s3.key": new_key,
            "natural_key": new_nk,
        }},
    )
    if result.modified_count:
        print(f"  [mongo] ✅ updated\n")
    else:
        print(f"  [mongo] ⚠️  변경 없음\n")

print("완료!")