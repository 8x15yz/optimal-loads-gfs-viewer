# app/db.py
import os
from motor.motor_asyncio import AsyncIOMotorClient

_MONGO_URI = os.getenv("MONGO_URI")
_MONGO_DB  = os.getenv("MONGO_DB", "optimal_loads")
_ASSET_COL = os.getenv("MONGO_COL", "assets_metadata")
_DIR_COL   = os.getenv("MONGO_DIR_COL", "directories")

# ✅ 추가
_CONTROL_COL = os.getenv("MONGO_CONTROL_COL", "ingestion_control")
_RUNS_COL    = os.getenv("MONGO_RUNS_COL", "ingestion_runs")

_client = AsyncIOMotorClient(_MONGO_URI)
_db = _client[_MONGO_DB]

async def get_assets_collection():
    return _db[_ASSET_COL]

async def get_directories_collection():
    return _db[_DIR_COL]

# ✅ 추가
async def get_ingestion_control_collection():
    return _db[_CONTROL_COL]

async def get_ingestion_runs_collection():
    return _db[_RUNS_COL]
