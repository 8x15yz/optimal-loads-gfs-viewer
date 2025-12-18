from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.cors import CORSMiddleware
from typing import Optional
from typing_extensions import Annotated
from pydantic import BeforeValidator
from dotenv import load_dotenv
from app.db import get_collection
from app.api import router as api_router
import os
from fastapi.staticfiles import StaticFiles


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


# ---- 기본 페이지 (inventory) ----
def empty_to_none(v):
    # "", "   " → None (Query '' → int 변환 에러 방지)
    if isinstance(v, str) and v.strip() == "":
        return None
    return v

YearParam  = Annotated[Optional[int], BeforeValidator(empty_to_none), Query()]
MonthParam = Annotated[Optional[int], BeforeValidator(empty_to_none), Query()]
PageParam  = Annotated[int,  Query(ge=1)]
SizeParam  = Annotated[int,  Query(ge=1, le=500)]


@app.get("/inventory", response_class=HTMLResponse, include_in_schema=False)
async def inventory(
    request: Request,
    source: str | None = None,
    dataset_code: str | None = None,
    variable: str | None = None,
    year: YearParam = None,
    month: MonthParam = None,
    day: int | None = None,     
    run_time_utc: str | None = None,  
    step_hours: int | None = None,          
    q: str | None = None,
    page: PageParam = 1,
    page_size: SizeParam = 50,
):
    coll = await get_collection()

    cond: dict = {}
    if source:        cond["source"] = source
    if dataset_code:  cond["dataset_code"] = dataset_code
    if variable:      cond["variable"] = variable
    if year is not None:  cond["year"] = year
    if month is not None: cond["month"] = month
    if day is not None:
        dd = f"{day:02d}"
        cond["valid_time_utc"] = {"$regex": fr"-{dd}T"}
    if run_time_utc:  cond["run_time_utc"] = run_time_utc
    if step_hours is not None: cond["step_hours"] = step_hours


    if q and q.strip():
        cond["$or"] = [
            {"name":        {"$regex": q, "$options": "i"}},
            {"s3.key":      {"$regex": q, "$options": "i"}},
            {"natural_key": {"$regex": q, "$options": "i"}},
            {"valid_key":   {"$regex": q, "$options": "i"}},
        ]

    total = await coll.count_documents(cond)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    skip = (page - 1) * page_size

    projection = {
        "_id": 0,

        # 기존
        "source": 1, "dataset_code": 1, "variable": 1,
        "year": 1, "month": 1, "valid_time_utc": 1,
        "name": 1, "size_bytes": 1,

        # ✅ 추가: 화면 + 상세에서 필요
        "name_en": 1,
        "unit": 1,
        "resolution": 1,

        "model": 1,
        "type": 1,
        "stream": 1,

        "run_time_utc": 1,
        "step_hours": 1,

        "natural_key": 1,
        "valid_key": 1,

        "s3": 1,
        "format": 1,
        "content_type": 1,
        "created_at": 1,
        "source_parameters": 1,
    }

    cursor = (
        coll.find(cond, projection)
        .sort([("valid_time_utc", -1)])
        .skip(skip).limit(page_size)
    )
    records = [doc async for doc in cursor]

    distinct_source   = await coll.distinct("source")
    distinct_dataset  = await coll.distinct("dataset_code")
    distinct_variable = await coll.distinct("variable")
    distinct_year     = sorted([int(y) for y in await coll.distinct("year") if isinstance(y, int)])
    distinct_month    = sorted([int(m) for m in await coll.distinct("month") if isinstance(m, int)])
    day_pipeline = [
        {"$project": {"dd": {"$substrBytes": ["$valid_time_utc", 8, 2]}}},
        {"$group": {"_id": "$dd"}},
        {"$sort": {"_id": 1}},
    ]
    day_docs = await coll.aggregate(day_pipeline).to_list(length=1000)
    distinct_day = [int(d["_id"]) for d in day_docs if d.get("_id") and d["_id"].isdigit()]
    distinct_run_time = await coll.distinct("run_time_utc")
    distinct_step = await coll.distinct("step_hours")


    return templates.TemplateResponse("inventory.html", {
        "request": request,
        "title": APP_TITLE,
        "records": records,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "filters": {
            "source": source, "dataset_code": dataset_code, "variable": variable,
            "year": year, "month": month,
            "day": day,                          # ✅
            "run_time_utc": run_time_utc,        # ✅
            "step_hours": step_hours,            # ✅
            "q": q
        },
        "choices": {
            "source": sorted(filter(None, distinct_source)),
            "dataset_code": sorted(filter(None, distinct_dataset)),
            "variable": sorted(filter(None, distinct_variable)),
            "year": distinct_year,
            "month": distinct_month,

            "day": distinct_day,                                     # ✅
            "run_time_utc": sorted(filter(None, distinct_run_time)), # ✅
            "step_hours": sorted([s for s in distinct_step if isinstance(s, int)]), # ✅
        }
    })
