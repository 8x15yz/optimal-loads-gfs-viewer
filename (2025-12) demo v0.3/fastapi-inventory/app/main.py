from __future__ import annotations

import os
import re
import json
from datetime import datetime, timezone
from typing import Optional, Any, Dict, List, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.cors import CORSMiddleware
from urllib.parse import urlencode

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
    openapi_tags=[{"name": "grid", "description": "grid data API"}],
)

app.mount("/guide", StaticFiles(directory="app/templates/static/guide"), name="guide")


@app.get("/ko", response_class=HTMLResponse, include_in_schema=False)
async def root_ko():
    return FileResponse("app/templates/static/guide/index_ko.html")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root_en():
    return FileResponse("app/templates/static/guide/index_en.html")


app.include_router(api_router)

# ---- CORS ----
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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


def _to_iso_z(v: Any) -> str:
    """
    datetime -> "YYYY-MM-DDTHH:MM:SSZ"
    str이면 그대로 반환(이미 Z 포맷이라고 가정)
    """
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, datetime):
        if v.tzinfo is None:
            # naive datetime을 UTC로 간주 (저장 단계에서 UTC aware로 넣는 게 가장 좋음)
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(v)

from urllib.parse import quote
def _build_api_example(doc: Dict[str, Any], bbox: List[str]) -> str:
    """
    /api/griddata?variable=...&forecast_datetime=YYYY-MM-DDTHH:MM:SSZ&source=...&bbox=... (콜론 유지)
    """
    variable = doc.get("variable") or ""
    source   = doc.get("source") or ""

    fdt = doc.get("forecast_datetime") or doc.get("valid_time_utc") or doc.get("run_time_utc")
    forecast_datetime = _to_iso_z(fdt)

    # ✅ 핵심: forecast_datetime에서 ":"는 인코딩하지 않도록 safe=":"
    # (T, Z, -, 숫자는 원래 인코딩 안 됨)
    forecast_datetime = quote(forecast_datetime, safe=":")

    # ✅ bbox는 숫자만 오니까 인코딩 필요 없음
    bbox_q = "&".join([f"bbox={v}" for v in bbox])

    # ✅ 너가 원하는 형태: "GET " 없이 /api/... 로만 (원하면 GET 붙여도 됨)
    return (
        f"/api/griddata"
        f"?variable={variable}"
        f"&forecast_datetime={forecast_datetime}"
        f"&source={source}"
        f"&{bbox_q}"
    )


# =========================================================
# ✅ /inventory (Index of …)  - inventory_directory 기반
# =========================================================

@app.get("/inventory", response_class=HTMLResponse, include_in_schema=False)
async def inventory_index(
    request: Request,
    path: str = Query("/", description="directory-like path. e.g. /ecmwf/ifs/2025/2025-07/2025-07-16/06Z/"),
    bbox: List[str] = Query(default=["128", "34", "130", "36"], description="bbox repeated 4 times: minLon,minLat,maxLon,maxLat"),
):
    coll = await get_collection()

    current_path = _norm_path(path)

    # ✅ directory는 trailing "/"를 강제 (inventory_directory도 이 형태로 저장하니까)
    if current_path != "/" and not current_path.endswith("/"):
        current_path = current_path + "/"

    parent_path = _parent_path(current_path.rstrip("/"))
    if parent_path and not parent_path.endswith("/"):
        parent_path += "/"

    DIR_FIELD = "inventory_directory"
    NAME_FIELD = "inventory_name"  # 파일 베이스 이름
    FILE_FIELD = "name"            # 있으면 이걸 우선 노출(확장자 포함)

    # path -> prefix (앞 "/" 제거)
    prefix_dir = current_path.lstrip("/")  # e.g. ecmwf/ifs/2025/.../10v/
    # 루트면 prefix_dir == "" 가 됨

    # ✅ regex 안전 처리
    escaped_prefix = re.escape(prefix_dir)

    # -----------------------------
    # 1) ✅ 하위 폴더(1-depth) 목록
    # -----------------------------
    pipeline_dirs = [
        {"$match": {DIR_FIELD: {"$regex": f"^{escaped_prefix}"}}},
        {"$project": {"dir": f"${DIR_FIELD}", "created_at": "$created_at"}},
        {"$addFields": {
            "remainder": {
                "$substrBytes": [
                    "$dir",
                    len(prefix_dir),
                    {"$subtract": [{"$strLenBytes": "$dir"}, len(prefix_dir)]}
                ]
            }
        }},
        {"$match": {"remainder": {"$ne": ""}}},
        {"$addFields": {
            "first": {"$arrayElemAt": [{"$split": ["$remainder", "/"]}, 0]},
        }},
        {"$match": {"first": {"$ne": ""}}},
        {"$group": {
            "_id": "$first",
            "last_modified": {"$max": "$created_at"},
        }},
        {"$sort": {"_id": 1}},
    ]
    dir_groups = await coll.aggregate(pipeline_dirs).to_list(length=5000)

    # -----------------------------
    # 2) ✅ 현재 디렉토리의 “파일” 목록
    # -----------------------------
    pipeline_files = [
        {"$match": {DIR_FIELD: prefix_dir}},
        {"$project": {
            "inventory_name": f"${NAME_FIELD}",
            "name": f"${FILE_FIELD}",
            "natural_key": "$natural_key",
            "created_at": "$created_at",
            "size_bytes": "$size_bytes",

            # ✅ 예시 URL 조립에 필요한 필드들
            "source": "$source",
            "variable": "$variable",
            "forecast_datetime": "$forecast_datetime",
            "valid_time_utc": "$valid_time_utc",
            "run_time_utc": "$run_time_utc",

            # (참고) 기타 유지
            "format": "$format",
            "content_type": "$content_type",
            "type": "$type",
            "dataset_code": "$dataset_code",
        }},
        {"$sort": {"name": 1, "inventory_name": 1}},
    ]
    file_docs = await coll.aggregate(pipeline_files).to_list(length=5000)

    entries: List[Dict[str, Any]] = []

    # ---- 폴더 엔트리
    for g in dir_groups:
        name = g.get("_id")
        if not name:
            continue

        lm = g.get("last_modified")
        last_modified = lm.strftime("%d-%b-%Y %H:%M") if isinstance(lm, datetime) else None

        child_path = (current_path + name + "/").replace("//", "/")
        entries.append({
            "name": name,
            "is_dir": True,
            "path": child_path,
            "file_id": None,
            "last_modified": last_modified,
            "size_human": None,
        })

    # ---- 파일 엔트리
    for d in file_docs:
        lm = d.get("created_at")
        last_modified = lm.strftime("%d-%b-%Y %H:%M") if isinstance(lm, datetime) else None

        display_name = d.get("name") or d.get("inventory_name") or "(unnamed)"

        api_example = _build_api_example(d, bbox=bbox)

        entries.append({
            "name": display_name,
            "is_dir": False,
            "path": None,
            "api_url": api_example,              # ✅ Copy 버튼이 이걸 복사
            "file_id": d.get("natural_key"),     # 내부 식별용
            "last_modified": last_modified,
            "size_human": _human_size(d.get("size_bytes")),
        })

    # 폴더 먼저, 파일 나중
    entries.sort(key=lambda x: (0 if x["is_dir"] else 1, x["name"]))

    description = None

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
# (선택) 파일 상세 보기: s3.key 기반
# =========================================================

@app.get("/inventory/file", response_class=HTMLResponse, include_in_schema=False)
async def inventory_file(request: Request, key: str = Query(..., description="S3 key")):
    coll = await get_collection()

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
        ("natural_key", None),
        ("valid_key", None),
        ("created_at", None),
        ("s3", None),
    ]

    def get_by_path(d: Dict[str, Any], path: str):
        cur: Any = d
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

    rows = []
    used = set()

    for field, _ in preferred_order:
        val = get_by_path(doc, field) if "." in field else doc.get(field)
        if val is not None:
            rows.append({"k": field, "v": fmt(val), "is_json": isinstance(val, (dict, list))})
            used.add(field.split(".")[0])

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
