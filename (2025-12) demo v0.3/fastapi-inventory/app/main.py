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

def describe_path(path: str) -> str | None:
    parts = path.strip("/").split("/")

    # .../oper/10v
    if len(parts) >= 2 and parts[-2] == "oper" and parts[-1] == "10v":
        return (
            "ECMWF IFS operational forecast · "
            "10 metre V wind component. "
            "Each file represents a forecast step for this model run."
        )

    if len(parts) >= 2 and parts[-2] == "oper" and parts[-1] == "10u":
        return (
            "ECMWF IFS operational forecast · "
            "10 metre U wind component. "
            "Each file represents a forecast step for this model run."
        )

    if len(parts) >= 2 and parts[-2] == "wave":
        return (
            "ECMWF IFS wave model output. "
            "Each subdirectory corresponds to a wave-related variable."
        )

    return None


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
    description = describe_path(current_path)

    return templates.TemplateResponse(
        "inventory.html",
        {
            "request": request,
            "current_path": current_path,
            "parent_path": parent_path,
            "entries": entries,
            "description": description,
        },
    )

# =========================================================
# (선택) 파일 클릭 핸들러 - 일단 placeholder
# =========================================================

from fastapi import HTTPException, Query
from fastapi.responses import HTMLResponse
import json
from datetime import datetime

@app.get("/inventory/file", response_class=HTMLResponse, include_in_schema=False)
async def inventory_file(request: Request, key: str = Query(..., description="S3 key")):
    coll = await get_collection()

    # ✅ key로 단건 조회
    doc = await coll.find_one({"s3.key": key}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail=f"Not found: {key}")

    # ✅ 표로 보여주기 좋은 주요 필드(원하는대로 추가/삭제 가능)
    preferred_order = [
        ("s3.key", None),
        ("source", None),
        ("dataset_code", None),
        ("model", None),
        ("type", None),
        ("stream", None),
        ("variable", None),
        ("name", None),
        ("name_en", None),
        ("unit", None),
        ("format", None),
        ("content_type", None),
        ("size_bytes", None),
        ("resolution", None),
        ("run_time_utc", None),
        ("step_hours", None),
        ("valid_time_utc", None),
        ("year", None),
        ("month", None),
        ("natural_key", None),
        ("valid_key", None),
        ("created_at", None),
        ("source_parameters", None),
        ("s3", None),
    ]

    def get_by_path(d, path: str):
        cur = d
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    def fmt(v):
        if v is None:
            return ""
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False, indent=2)
        return str(v)

    rows = []
    used = set()

    for field, _ in preferred_order:
        val = get_by_path(doc, field) if "." in field else doc.get(field)
        if val is not None:
            rows.append({"k": field, "v": fmt(val), "is_json": isinstance(val, (dict, list))})
            used.add(field.split(".")[0])

    # ✅ 나머지 필드도 아래쪽에 “Other fields”로 보여주고 싶으면
    # (원치 않으면 이 블록 삭제해도 됨)
    other = []
    for k, v in doc.items():
        if k in used:
            continue
        other.append({"k": k, "v": fmt(v), "is_json": isinstance(v, (dict, list))})

    return templates.TemplateResponse(
        "inventory_file.html",
        {
            "request": request,
            "key": key,
            "rows": rows,
            "other": other,
        },
    )
