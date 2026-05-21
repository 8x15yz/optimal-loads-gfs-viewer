#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# inspect_mongo_s111.py
# MongoDB assets_metadata 컬렉션에서 s111 문서 구조 확인용

import json
import os
from pymongo import MongoClient

MONGO_URI  = os.environ.get("MONGO_URI",  "mongodb+srv://8x15yz_db_user:3WprrHmmFJiWcVEr@cluster0.oirpleh.mongodb.net/?appName=Cluster0")
MONGO_DB   = os.environ.get("MONGO_DB",   "optimal_loads")
MONGO_S100_COL = os.environ.get("MONGO_S100_COL", "s100assets_metadata")

client = MongoClient(MONGO_URI)
col = client[MONGO_DB][MONGO_S100_COL]

print(f"=== DB: {MONGO_DB}  COL: {MONGO_S100_COL} ===\n")

# 1) 전체 문서 수
total = col.count_documents({})
print(f"[1] 전체 문서 수: {total}\n")

# 2) product 종류별 카운트
print("[2] product 별 카운트:")
for item in col.aggregate([{"$group": {"_id": "$product", "count": {"$sum": 1}}}]):
    print(f"    {item['_id']}: {item['count']}")
print()

# 3) s111 문서 샘플 1개 (전체 필드 출력)
print("[3] s111 문서 샘플 1개:")
doc = col.find_one({"product": "s111"})
if doc:
    print(json.dumps(doc, indent=2, default=str, ensure_ascii=False))
else:
    print("  ⚠️  product=s111 문서 없음")

    # s111이 없으면 아무 문서나 1개 출력
    print("\n  → 아무 문서나 1개 샘플:")
    doc2 = col.find_one()
    if doc2:
        print(json.dumps(doc2, indent=2, default=str, ensure_ascii=False))
    else:
        print("  컬렉션이 비어있습니다.")

print()

# 4) s111 문서의 최상위 필드명 목록 확인 (5개 샘플)
print("[4] s111 문서 필드명 목록 (최대 5개 샘플):")
keys_set = set()
for d in col.find({"product": "s111"}).limit(5):
    keys_set.update(d.keys())
if keys_set:
    for k in sorted(keys_set):
        print(f"    {k}")
else:
    print("  s111 문서 없음")

print()

# 5) s3 관련 필드 확인
print("[5] s111 문서의 s3 필드 구조:")
doc_s3 = col.find_one({"product": "s111"}, {"s3": 1, "s3_key": 1, "key": 1})
if doc_s3:
    print(json.dumps(doc_s3, indent=2, default=str, ensure_ascii=False))
else:
    print("  s111 문서 없음")