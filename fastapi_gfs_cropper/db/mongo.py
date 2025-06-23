from pymongo import MongoClient
import os

MONGO_URI = os.getenv("MONGO_URI", "mongodb://bluemap.kr:21808")
client = MongoClient(MONGO_URI)
db = client["gfs_data"]
collection = db["wind"]
