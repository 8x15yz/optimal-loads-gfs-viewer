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
# ✅ NEW: /inventory (Index of …)  - inventory_directory 기반
# =========================================================

@app.get("/inventory", response_class=HTMLResponse, include_in_schema=False)
async def inventory_index(
    request: Request,
    path: str = Query("/", description="directory-like path. e.g. /ecmwf/ifs/2025/2025-07/2025-07-16/06Z/"),
):
    coll = await get_collection()

    current_path = _norm_path(path)

    # ✅ directory는 trailing "/"를 강제 (inventory_directory도 이 형태로 저장하니까)
    if current_path != "/" and not current_path.endswith("/"):
        current_path = current_path + "/"

    parent_path = _parent_path(current_path.rstrip("/"))  # parent는 / 없이 계산
    if parent_path and not parent_path.endswith("/"):
        parent_path += "/"

    DIR_FIELD  = "inventory_directory"
    NAME_FIELD = "inventory_name"   # 파일 베이스 이름
    FILE_FIELD = "name"             # 있으면 이걸 우선 노출(확장자 포함)

    # path -> prefix (앞 "/" 제거)
    prefix_dir = current_path.lstrip("/")  # e.g. ecmwf/ifs/2025/.../10v/
    if prefix_dir == "/":
        prefix_dir = ""

    # -----------------------------
    # 1) ✅ 하위 폴더(1-depth) 목록
    # -----------------------------
    # current_dir보다 더 깊은 inventory_directory를 가진 문서들에서 "바로 다음 segment"만 추출
    pipeline_dirs = [
        {"$match": {DIR_FIELD: {"$regex": f"^{prefix_dir}"}}},
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
        # remainder가 ""면 현재 디렉토리의 “파일들”이므로 폴더 후보에서 제외
        {"$match": {"remainder": {"$ne": ""}}},
        {"$addFields": {
            "parts": {"$split": ["$remainder", "/"]},
            "first": {"$arrayElemAt": [{"$split": ["$remainder", "/"]}, 0]},
        }},
        # first가 빈 문자열이면 제외(이상치 방어)
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
    # inventory_directory == current_dir 인 문서들을 파일로 나열
    pipeline_files = [
        {"$match": {DIR_FIELD: prefix_dir}},
        {"$project": {
            "inventory_name": f"${NAME_FIELD}",
            "name": f"${FILE_FIELD}",
            "natural_key": "$natural_key",
            "created_at": "$created_at",
            "size_bytes": "$size_bytes",
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
            "path": child_path,     # /inventory?path=...
            "file_id": None,
            "last_modified": last_modified,
            "size_human": None,
        })

    # ---- 파일 엔트리
    for d in file_docs:
        lm = d.get("created_at")
        last_modified = lm.strftime("%d-%b-%Y %H:%M") if isinstance(lm, datetime) else None

        # 화면에 보여줄 파일명: name(확장자 포함)이 있으면 그걸 우선, 없으면 inventory_name
        display_name = d.get("name") or d.get("inventory_name") or "(unnamed)"

        entries.append({
            "name": display_name,
            "is_dir": False,
            "path": None,
            # ✅ derived도 열람 가능하도록 natural_key를 file id로 사용 (가장 안전/유니크)
            "file_id": d.get("natural_key"),
            "last_modified": last_modified,
            "size_human": _human_size(d.get("size_bytes")),
        })

    # (선택) 폴더가 위에, 파일이 아래로 보이게 정렬
    entries.sort(key=lambda x: (0 if x["is_dir"] else 1, x["name"]))

    # ✅ describe_path도 이제 path에서 oper/wave가 안 나오니, dataset_code/variable 기반으로 바꾸는 게 좋음
    # 일단은 폴더 path만 보고 간단히:
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
