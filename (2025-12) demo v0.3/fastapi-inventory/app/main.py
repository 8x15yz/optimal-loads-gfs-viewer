from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from app.db import get_collection
from app.api import router as api_router
import os
from fastapi.staticfiles import StaticFiles
from datetime import datetime
from typing import Optional, Any, Dict, List

load_dotenv()

APP_TITLE = os.getenv("APP_TITLE", "Inventory")
templates = Jinja2Templates(directory="app/templates")

app = FastAPI(
    title=APP_TITLE,
    version="0.1.0",
    description="""
It reads ocean gridded data (e.g., CMEMS wave data) from S3 and provides it as JSON.

- Indexing: row-major, bottom-up (south → north)
""",
    contact={"name": "BlueMap", "email": "hjk@bluemap.dev"},
    license_info={"name": "MIT"},
    openapi_tags=[
        {"name": "grid", "description": "grid data API"},
    ],
)

app.mount("/guide", StaticFiles(directory="app/templates/static/guide"), name="guide")

@app.get("/ko", response_class=HTMLResponse, include_in_schema=False)
async def root():
    return FileResponse("app/templates/static/guide/index_ko.html")

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root_en():
    return FileResponse("app/templates/static/guide/index_en.html")

app.include_router(api_router)

# ---- CORS ----
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# =========================================================
# NOAA Index 스타일 helper
# =========================================================

def _norm_path(p: str) -> str:
    p = (p or "/").strip()
    if not p.startswith("/"):
        p = "/" + p
    if p != "/" and p.endswith("/"):
        p = p[:-1]
    return p

def _parent_path(p: str) -> Optional[str]:
    p = _norm_path(p)
    if p == "/":
        return None
    parts = p.strip("/").split("/")
    if len(parts) <= 1:
        return "/"
    return "/" + "/".join(parts[:-1])

def _human_size(n: Optional[int]) -> Optional[str]:
    if n is None:
        return None
    size = float(n)
    for unit in ["B", "K", "M", "G", "T"]:
        if size < 1024 or unit == "T":
            if unit == "B":
                return f"{int(size)}"
            return f"{size:.1f}{unit}"
        size /= 1024.0
    return str(n)

# =========================================================
# ✅ NEW: /inventory (Index of …)
# =========================================================

@app.get("/inventory", response_class=HTMLResponse, include_in_schema=False)
async def inventory_index(
    request: Request,
    path: str = Query("/", description="directory-like path. e.g. /pub/data/nccf/com/gfs/prod/gfs.20251209"),
):
    """
    NOAA 'Index of ...' 스타일 디렉토리 브라우저.
    Mongo에 저장된 s3.key를 기준으로 path 하위의 '바로 한 단계 자식'만 폴더/파일로 묶어 보여준다.
    """
    coll = await get_collection()

    current_path = _norm_path(path)
    parent_path = _parent_path(current_path)

    # ✅ 네 스키마가 s3: { key: "..."} 구조니까 "s3.key" 사용
    KEY_FIELD = "s3.key"

    # path -> S3 key prefix (앞 "/" 제거 + trailing "/" 보장)
    prefix = current_path.strip("/")
    prefix = f"{prefix}/" if prefix else ""

    pipeline = [
        {"$match": {KEY_FIELD: {"$regex": f"^{prefix}"}}},
        {"$project": {
            "key": f"${KEY_FIELD}",
            "size_bytes": "$size_bytes",
            "created_at": "$created_at",
        }},
        {"$addFields": {
            "remainder": {
                "$substrBytes": [
                    "$key",
                    len(prefix),
                    {"$subtract": [{"$strLenBytes": "$key"}, len(prefix)]}
                ]
            }
        }},
        {"$addFields": {
            "parts": {"$split": ["$remainder", "/"]},
            "first": {"$arrayElemAt": [{"$split": ["$remainder", "/"]}, 0]},
        }},
        {"$addFields": {
            "has_more": {"$gt": [{"$size": "$parts"}, 1]}
        }},
        {"$group": {
            "_id": "$first",
            "is_dir": {"$max": {"$cond": ["$has_more", 1, 0]}},
            "last_modified": {"$max": "$created_at"},
            "size_bytes": {
                "$sum": {
                    "$cond": ["$has_more", 0, {"$ifNull": ["$size_bytes", 0]}]
                }
            }
        }},
        {"$sort": {"_id": 1}}
    ]

    groups = await coll.aggregate(pipeline).to_list(length=5000)

    entries: List[Dict[str, Any]] = []
    for g in groups:
        name = g.get("_id")
        if not name:
            continue

        is_dir = bool(g.get("is_dir", 0))
        lm = g.get("last_modified")
        last_modified = lm.strftime("%d-%b-%Y %H:%M") if isinstance(lm, datetime) else None

        # UI에서 쓰는 child path (앞에는 "/" 유지)
        child_path = (current_path.rstrip("/") + "/" + name).replace("//", "/")

        if is_dir:
            entries.append({
                "name": name,
                "is_dir": True,
                "path": child_path,
                "key": None,
                "last_modified": last_modified,
                "size_human": None,
            })
        else:
            full_key = prefix + name  # S3 key (앞 "/" 없음)
            entries.append({
                "name": name,
                "is_dir": False,
                "path": None,
                "key": full_key,
                "last_modified": last_modified,
                "size_human": _human_size(int(g.get("size_bytes", 0))),
            })

    return templates.TemplateResponse("inventory_index.html", {
        "request": request,
        "title": APP_TITLE,
        "current_path": current_path,
        "parent_path": parent_path,
        "entries": entries,
    })

# =========================================================
# (선택) 파일 클릭 핸들러 - 일단 placeholder
# =========================================================

@app.get("/inventory/file", include_in_schema=False)
async def inventory_file(key: str = Query(..., description="S3 key")):
    # TODO:
    # 1) presigned URL로 리다이렉트
    # 2) 또는 파일 메타 + griddata 예시를 보여주는 페이지로 렌더링
    raise HTTPException(status_code=501, detail=f"Not implemented yet. key={key}")

