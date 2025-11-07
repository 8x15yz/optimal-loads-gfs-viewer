## 온프레미스 버전
# from pymongo import MongoClient
# import os

# MONGO_URI = os.getenv("MONGO_URI", "mongodb://bluemap.kr:21808")
# client = MongoClient(MONGO_URI)
# db = client["gfs_data"]
# collection = db["wind"]



## 아틀라스 버전
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi

uri = "mongodb+srv://8x15yz_db_user:3WprrHmmFJiWcVEr@cluster0.oirpleh.mongodb.net/?appName=Cluster0"

# Create a new client and connect to the server
client = MongoClient(uri, server_api=ServerApi('1'))

# Send a ping to confirm a successful connection
try:
    client.admin.command('ping')
    print("Pinged your deployment. You successfully connected to MongoDB!")
except Exception as e:
    print(e)

db = client["gfs_data"]
collection = db["wind"]
