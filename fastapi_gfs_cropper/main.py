from fastapi import FastAPI
from routes.gfs import router as gfs_router
from fastapi.middleware.cors import CORSMiddleware



app = FastAPI()

app.include_router(gfs_router, prefix="/api/gfs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 또는 ["http://192.168.2.84:3000"] 로 제한 가능
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)