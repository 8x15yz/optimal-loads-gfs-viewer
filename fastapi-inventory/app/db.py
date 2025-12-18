import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from typing import Optional

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME   = os.getenv("MONGO_DB", "yourdb")
COLL_NAME = os.getenv("MONGO_COLL", "inventory")

_client: Optional[AsyncIOMotorClient] = None

async def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    return _client

async def get_collection():
    client = await get_client()
    return client[DB_NAME][COLL_NAME]
