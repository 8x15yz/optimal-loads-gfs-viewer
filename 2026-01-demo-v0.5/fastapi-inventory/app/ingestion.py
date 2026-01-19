# app/ingestion.py
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_ingestion_control_collection, get_ingestion_runs_collection

CONTROL_DOC_ID = os.getenv("CONTROL_DOC_ID", "ecmwf_ifs_ingestion")

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(tags=["ingestion"])

def _fmt_dt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(v)

@router.get("/ingestion", response_class=HTMLResponse, include_in_schema=False)
async def ingestion_page(
    request: Request,
    status: Optional[str] = Query(None),
    variable: Optional[str] = Query(None),
    stream: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    control_col = await get_ingestion_control_collection()
    runs_col = await get_ingestion_runs_collection()

    control = await control_col.find_one({"_id": CONTROL_DOC_ID}) or {}

    q: Dict[str, Any] = {}
    if status:
        q["status"] = status
    if variable:
        q["variable"] = variable
    if stream:
        q["stream"] = stream

    runs = await runs_col.find(q).sort([("run_time_utc", -1), ("variable", 1)]).limit(limit).to_list(length=limit)

    # dropdown 후보(최근 데이터 기반으로 뽑기)
    vars_ = sorted({r.get("variable") for r in runs if r.get("variable")})
    streams_ = sorted({r.get("stream") for r in runs if r.get("stream")})
    statuses_ = ["running", "success", "partial", "failed"]

    return templates.TemplateResponse(
        "ingestion.html",
        {
            "request": request,
            "control": {
                **control,
                "last_started_at": _fmt_dt(control.get("last_started_at")),
                "last_finished_at": _fmt_dt(control.get("last_finished_at")),
                "last_heartbeat_at": _fmt_dt(control.get("last_heartbeat_at")),
            },
            "runs": [
                {
                    **r,
                    "started_at": _fmt_dt(r.get("started_at")),
                    "finished_at": _fmt_dt(r.get("finished_at")),
                }
                for r in runs
            ],
            "filters": {"status": status or "", "variable": variable or "", "stream": stream or "", "limit": limit},
            "vars": vars_,
            "streams": streams_,
            "statuses": statuses_,
        },
    )

@router.post("/ingestion/pause", include_in_schema=False)
async def ingestion_pause():
    control_col = await get_ingestion_control_collection()
    await control_col.update_one(
        {"_id": CONTROL_DOC_ID},
        {"$set": {"enabled": False, "status": "paused", "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return RedirectResponse(url="/ingestion", status_code=303)

@router.post("/ingestion/resume", include_in_schema=False)
async def ingestion_resume():
    control_col = await get_ingestion_control_collection()
    await control_col.update_one(
        {"_id": CONTROL_DOC_ID},
        {"$set": {"enabled": True, "status": "idle", "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return RedirectResponse(url="/ingestion", status_code=303)

# (선택) JSON API: UI 말고도 외부에서 조회용
@router.get("/api/ingestion/control")
async def api_ingestion_control():
    control_col = await get_ingestion_control_collection()
    return await control_col.find_one({"_id": CONTROL_DOC_ID}, {"_id": 0}) or {}

@router.get("/api/ingestion/runs")
async def api_ingestion_runs(
    status: Optional[str] = None,
    variable: Optional[str] = None,
    stream: Optional[str] = None,
    limit: int = 50,
):
    runs_col = await get_ingestion_runs_collection()
    q: Dict[str, Any] = {}
    if status: q["status"] = status
    if variable: q["variable"] = variable
    if stream: q["stream"] = stream
    return await runs_col.find(q, {"_id": 0}).sort([("run_time_utc", -1)]).limit(limit).to_list(length=limit)
