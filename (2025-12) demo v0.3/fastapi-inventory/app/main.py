from __future__ import annotations

import os
import re
import json
from datetime import datetime
from typing import Optional, Any, Dict, List

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware

from app.db import get_collection
from app.api import router as api_router

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

# ---- Static Guide ----
app.mount("/guide", StaticFiles(directory="app/templates/static/guide"), name="guide")

@app.get("/ko", response_class=HTMLResponse, include_in_schema=False)
async def root_ko():
    return FileResponse("app/templates/static/guide/index_ko.html")

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root_en():
    return FileResponse("app/templates/static/guide/index_en.html")

# ---- API Router ----
app.include_router(api_router)

# ---- CORS ----
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# =========================================================
# Helpers
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

RUN_LEVEL_RE = re.compile(r"^/.+/\d{4}/\d{2}/\d{2}/\d{2}Z$")

def is_run_level(path: str) -> bool:
    # 예: /ecmwf/original/ifs/fc/2025/12/17/18Z
    return bool(RUN_LEVEL_RE.match(_norm_path(path)))

def stream_for_var(var: str) -> str:
    """
    UI에서 oper/wave를 숨겼을 때, var 클릭 시 실제 stream으로 라우팅.
    - 10u/10v -> oper
    - 그 외(파도 등) -> wave
    필요하면 여기 규칙만 늘리면 됨.
    """
    return "oper" if var in ("10u", "10v") else "wave"

def describe_path(path: str) -> Optional[str]:
    path = _norm_path(path)
    parts = path.strip("/").split("/")

    # run-level 안내 (oper/wave 숨김)
    if is_run_level(path):
        return (
            "Model run directory. Stream folders (oper/wave) are hidden in this view; "
            "variables are shown directly."
        )

    # .../oper/10v
    if len(parts) >= 2 and parts[-2] == "oper" and parts[-1] == "10v":
        return (
            "ECMWF IFS operational forecast · "
            "10 metre V wind component. "
            "Each file represents a forecast step for this model run."
        )

    # .../oper/10u
    if len(parts) >= 2 and parts[-2] == "oper" and parts[-1] == "10u":
        return (
            "ECMWF IFS operational forecast · "
            "10 metre U wind component. "
            "Each file represents a forecast step for this model run."
        )

    # .../wave/*
    if len(parts) >= 2 and parts[-2] == "wave":
        return (
            "ECMWF IFS wave model output. "
            "Each subdirectory corresponds to a wave-related variable."
        )

    return None

# =========================================================
# /inventory  (NOAA Index style)
# =========================================================

@app.get("/inventory", response_class=HTMLResponse, include_in_schema=False)
async def inventory_index(
    request: Request,
    path: str = Query("/", description="directory-like path. e.g. /ecmwf/original/ifs/fc/2025/12/17/18Z"),
):
    coll = await get_collection()

    current_path = _norm_path(path)
    parent_path = _parent_path(current_path)

    KEY_FIELD = "s3.key"  # Mongo: s3: { key: ... }

    # path -> s3 prefix (leading "/" 제거, trailing "/" 추가)
    prefix = current_path.strip("/")
    prefix = f"{prefix}/" if prefix else ""

    run_level = is_run_level(current_path)

    if run_level:
        # ✅ flatten: .../18Z/oper/10v/...  → 10v/ 를 바로 보여줌 (UI)
        pipeline = [
            {"$match": {KEY_FIELD: {"$regex": f"^{prefix}"}}},
            {"$project": {"key": f"${KEY_FIELD}", "created_at": 1}},
            {"$addFields": {
                "rest": {
                    "$substrBytes": [
                        "$key",
                        len(prefix),
                        {"$subtract": [{"$strLenBytes": "$key"}, len(prefix)]}
                    ]
                }
            }},
            {"$addFields": {
                "parts": {"$split": ["$rest", "/"]},
                "stream": {"$arrayElemAt": ["$parts", 0]},
                "var": {"$arrayElemAt": ["$parts", 1]},
            }},
            {"$match": {"stream": {"$in": ["oper", "wave"]}, "var": {"$ne": None}}},
            {"$group": {
                "_id": "$var",
                "last_modified": {"$max": "$created_at"},
            }},
            {"$sort": {"_id": 1}},
        ]
    else:
        # ✅ 일반: 바로 아래 1단계(oper/, wave/, 2025/, 12/ 등) 보여줌
        pipeline = [
            {"$match": {KEY_FIELD: {"$regex": f"^{prefix}"}}},
            {"$project": {"key": f"${KEY_FIELD}", "size_bytes": 1, "created_at": 1}},
            {"$addFields": {
                "rest": {
                    "$substrBytes": [
                        "$key",
                        len(prefix),
                        {"$subtract": [{"$strLenBytes": "$key"}, len(prefix)]}
                    ]
                }
            }},
            {"$addFields": {
                "parts": {"$split": ["$rest", "/"]},
                "name": {"$arrayElemAt": [{"$split": ["$rest", "/"]}, 0]},
                "is_dir": {"$gt": [{"$size": {"$split": ["$rest", "/"]}}, 1]},
            }},
            {"$group": {
                "_id": "$name",
                "is_dir": {"$max": {"$cond": ["$is_dir", 1, 0]}},
                "last_modified": {"$max": "$created_at"},
                "size_bytes": {
                    "$sum": {"$cond": ["$is_dir", 0, {"$ifNull": ["$size_bytes", 0]}]}
                }
            }},
            {"$sort": {"is_dir": -1, "_id": 1}},
        ]

    groups = await coll.aggregate(pipeline).to_list(length=5000)

    entries: List[Dict[str, Any]] = []

    for g in groups:
        name = g.get("_id")
        if not name:
            continue

        lm = g.get("last_modified")
        last_modified = lm.strftime("%d-%b-%Y %H:%M") if isinstance(lm, datetime) else None

        if run_level:
            # ✅ UI에서는 var만 보이지만, 실제로는 /18Z/{oper|wave}/{var} 로 들어가야 함
            stream = stream_for_var(name)
            child_path = f"{current_path.rstrip('/')}/{stream}/{name}".replace("//", "/")

            entries.append({
                "name": name,
                "is_dir": True,
                "path": child_path,
                "key": None,
                "last_modified": last_modified,
                "size_human": None,
            })
            continue

        # 일반 모드
        is_dir = bool(g.get("is_dir", 0))
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
            full_key = prefix + name
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
# /inventory/file  (metadata table)
# =========================================================

@app.get("/inventory/file", response_class=HTMLResponse, include_in_schema=False)
async def inventory_file(
    request: Request,
    key: str = Query(..., description="S3 key"),
):
    coll = await get_collection()

    # 단건 조회
    doc = await coll.find_one({"s3.key": key}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail=f"Not found: {key}")

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

    def get_by_path(d: dict, path: str):
        cur = d
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur

    def fmt(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False, indent=2)
        return str(v)

    rows: List[Dict[str, Any]] = []
    used_top_keys = set()

    for field, _ in preferred_order:
        val = get_by_path(doc, field) if "." in field else doc.get(field)
        if val is not None:
            rows.append({
                "k": field,
                "v": fmt(val),
                "is_json": isinstance(val, (dict, list)),
            })
            used_top_keys.add(field.split(".")[0])

    other: List[Dict[str, Any]] = []
    for k, v in doc.items():
        if k in used_top_keys:
            continue
        other.append({
            "k": k,
            "v": fmt(v),
            "is_json": isinstance(v, (dict, list)),
        })

    return templates.TemplateResponse(
        "inventory_file.html",
        {
            "request": request,
            "key": key,
            "rows": rows,
            "other": other,
        },
    )
