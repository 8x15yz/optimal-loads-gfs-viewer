# app/ingestion.py - 탭 분리 버전
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, Query, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_ingestion_control_collection, get_ingestion_runs_collection

# ✅ 두 모델의 control doc ID
CONTROL_DOC_ID_ECMWF = os.getenv("CONTROL_DOC_ID_ECMWF", "ecmwf_ifs_ingestion")
CONTROL_DOC_ID_NOAA = os.getenv("CONTROL_DOC_ID_NOAA", "noaa_gfs_ingestion")

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(tags=["ingestion"])


def _fmt_dt(v: Any) -> str:
    """UI 표시용 datetime 포맷"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(v)


def _safe_counters(c: Any) -> Dict[str, int]:
    """counters 안전 추출"""
    if not isinstance(c, dict):
        return {"downloaded": 0, "existed": 0, "uploaded": 0, "mongo_written": 0, "failed": 0}
    return {
        "downloaded": int(c.get("downloaded", 0) or 0),
        "existed": int(c.get("existed", 0) or 0),
        "uploaded": int(c.get("uploaded", 0) or 0),
        "mongo_written": int(c.get("mongo_written", 0) or 0),
        "failed": int(c.get("failed", 0) or 0),
    }


async def _get_control_data(control_id: str) -> Dict[str, Any]:
    """특정 모델의 control 문서 가져오기"""
    control_col = await get_ingestion_control_collection()
    doc = await control_col.find_one({"_id": control_id}) or {}
    
    return {
        **doc,
        "last_started_at": _fmt_dt(doc.get("last_started_at")),
        "last_finished_at": _fmt_dt(doc.get("last_finished_at")),
        "last_heartbeat_at": _fmt_dt(doc.get("last_heartbeat_at")),
    }


async def _get_runs_data(
    source: str,
    status: Optional[str] = None,
    variable: Optional[str] = None,
    stream: Optional[str] = None,
    limit: int = 50,
) -> tuple[List[Dict[str, Any]], List[str], List[str], List[str]]:
    """
    특정 소스(ecmwf/noaa)의 runs 데이터 가져오기
    
    Returns:
        (runs, vars, streams, statuses)
    """
    runs_col = await get_ingestion_runs_collection()

    q: Dict[str, Any] = {"source": source}
    if status:
        q["status"] = status
    if variable:
        q["variable"] = variable
    if stream:
        q["stream"] = stream

    projection: Dict[str, Any] = {
        "_id": 1,
        "source": 1,
        "run_time_utc": 1,
        "stream": 1,
        "variable": 1,
        "status": 1,
        "updated_at": 1,
        "running": 1,

        # new schema
        "attempts_cnt": 1,
        "started_at_last": 1,
        "finished_at_last": 1,
        "duration_sec_last": 1,
        "trigger_last": 1,
        "counters_last": 1,
        "counters_total": 1,
        "notes_last": 1,
        "attempts": {"$slice": -5},

        # legacy compatibility
        "started_at": 1,
        "finished_at": 1,
        "counters": 1,
        "trigger": 1,
        "errors": 1,
    }

    raw_runs = (
        await runs_col.find(q, projection)
        .sort([("run_time_utc", -1), ("variable", 1)])
        .limit(limit)
        .to_list(length=limit)
    )

    # dropdown 후보
    vars_ = sorted({r.get("variable") for r in raw_runs if r.get("variable")})
    streams_ = sorted({r.get("stream") for r in raw_runs if r.get("stream")})
    statuses_ = ["running", "success", "partial", "failed"]

    # runs 정규화
    runs: List[Dict[str, Any]] = []
    for r in raw_runs:
        attempts = r.get("attempts") or []
        attempts_cnt_val = r.get("attempts_cnt")
        if attempts_cnt_val is None:
            attempts_cnt_val = len(attempts)

        started_last = r.get("started_at_last") or r.get("started_at")
        finished_last = r.get("finished_at_last") or r.get("finished_at")
        counters_last = r.get("counters_last") or r.get("counters")
        counters_last = _safe_counters(counters_last)

        last_attempt_errors = []
        if attempts:
            last_attempt_errors = (attempts[-1].get("errors") or [])

        # attempts 정규화
        norm_attempts = []
        for a in attempts:
            ac = _safe_counters(a.get("counters"))
            norm_attempts.append({
                **a,
                "started_at": _fmt_dt(a.get("started_at")),
                "finished_at": _fmt_dt(a.get("finished_at")),
                "counters": ac,
                "duration_sec": a.get("duration_sec") if a.get("duration_sec") is not None else "",
                "errors": a.get("errors") or [],
            })

        runs.append({
            **r,
            "attempts": norm_attempts,
            "attempts_cnt": int(attempts_cnt_val),
            "started_at_last": _fmt_dt(r.get("started_at_last")),
            "finished_at_last": _fmt_dt(r.get("finished_at_last")),
            "counters_last": counters_last,
            "started_at": _fmt_dt(started_last),
            "finished_at": _fmt_dt(finished_last),
            "counters": counters_last,
            "errors": last_attempt_errors,
            "errors_cnt": len(last_attempt_errors),
        })

    return runs, vars_, streams_, statuses_


# ==================== 통합 페이지 (탭 버전) ====================

@router.get("/ingestion", response_class=HTMLResponse, include_in_schema=False)
async def ingestion_page(
    request: Request,
    # ECMWF 필터
    status_ecmwf: Optional[str] = Query(None),
    variable_ecmwf: Optional[str] = Query(None),
    stream_ecmwf: Optional[str] = Query(None),
    limit_ecmwf: int = Query(50, ge=1, le=500),
    
    # NOAA 필터
    status_noaa: Optional[str] = Query(None),
    variable_noaa: Optional[str] = Query(None),
    stream_noaa: Optional[str] = Query(None),
    limit_noaa: int = Query(50, ge=1, le=500),
):
    """
    ✅ ECMWF + NOAA 탭 통합 페이지
    """
    
    # ECMWF 데이터
    control_ecmwf = await _get_control_data(CONTROL_DOC_ID_ECMWF)
    runs_ecmwf, vars_ecmwf, streams_ecmwf, statuses_ecmwf = await _get_runs_data(
        source="ecmwf",
        status=status_ecmwf,
        variable=variable_ecmwf,
        stream=stream_ecmwf,
        limit=limit_ecmwf,
    )
    
    # NOAA 데이터
    control_noaa = await _get_control_data(CONTROL_DOC_ID_NOAA)
    runs_noaa, vars_noaa, streams_noaa, statuses_noaa = await _get_runs_data(
        source="noaa",
        status=status_noaa,
        variable=variable_noaa,
        stream=stream_noaa,
        limit=limit_noaa,
    )

    return templates.TemplateResponse(
        "ingestion.html",
        {
            "request": request,
            
            # ECMWF
            "control_ecmwf": control_ecmwf,
            "runs_ecmwf": runs_ecmwf,
            "filters_ecmwf": {
                "status": status_ecmwf or "",
                "variable": variable_ecmwf or "",
                "stream": stream_ecmwf or "",
                "limit": limit_ecmwf,
            },
            "vars_ecmwf": vars_ecmwf,
            "streams_ecmwf": streams_ecmwf,
            "statuses_ecmwf": statuses_ecmwf,
            
            # NOAA
            "control_noaa": control_noaa,
            "runs_noaa": runs_noaa,
            "filters_noaa": {
                "status": status_noaa or "",
                "variable": variable_noaa or "",
                "stream": stream_noaa or "",
                "limit": limit_noaa,
            },
            "vars_noaa": vars_noaa,
            "streams_noaa": streams_noaa,
            "statuses_noaa": statuses_noaa,
        },
    )


# ==================== ECMWF Control ====================

@router.post("/ingestion/ecmwf/pause", include_in_schema=False)
async def ecmwf_pause():
    control_col = await get_ingestion_control_collection()
    await control_col.update_one(
        {"_id": CONTROL_DOC_ID_ECMWF},
        {"$set": {"enabled": False, "status": "paused", "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return RedirectResponse(url="/ingestion", status_code=303)


@router.post("/ingestion/ecmwf/resume", include_in_schema=False)
async def ecmwf_resume():
    control_col = await get_ingestion_control_collection()
    await control_col.update_one(
        {"_id": CONTROL_DOC_ID_ECMWF},
        {"$set": {"enabled": True, "status": "idle", "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return RedirectResponse(url="/ingestion", status_code=303)


# ==================== NOAA Control ====================

@router.post("/ingestion/noaa/pause", include_in_schema=False)
async def noaa_pause():
    control_col = await get_ingestion_control_collection()
    await control_col.update_one(
        {"_id": CONTROL_DOC_ID_NOAA},
        {"$set": {"enabled": False, "status": "paused", "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return RedirectResponse(url="/ingestion", status_code=303)


@router.post("/ingestion/noaa/resume", include_in_schema=False)
async def noaa_resume():
    control_col = await get_ingestion_control_collection()
    await control_col.update_one(
        {"_id": CONTROL_DOC_ID_NOAA},
        {"$set": {"enabled": True, "status": "idle", "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return RedirectResponse(url="/ingestion", status_code=303)


# ==================== API Endpoints ====================

@router.get("/api/ingestion/control/ecmwf", include_in_schema=False)
async def api_ecmwf_control():
    """ECMWF control 상태 API"""
    control_col = await get_ingestion_control_collection()
    return await control_col.find_one({"_id": CONTROL_DOC_ID_ECMWF}, {"_id": 0}) or {}


@router.get("/api/ingestion/control/noaa", include_in_schema=False)
async def api_noaa_control():
    """NOAA control 상태 API"""
    control_col = await get_ingestion_control_collection()
    return await control_col.find_one({"_id": CONTROL_DOC_ID_NOAA}, {"_id": 0}) or {}


@router.get("/api/ingestion/runs", include_in_schema=False)
async def api_ingestion_runs(
    source: Optional[str] = Query(None, description="ecmwf or noaa"),
    status: Optional[str] = None,
    variable: Optional[str] = None,
    stream: Optional[str] = None,
    limit: int = 50,
):
    """
    ✅ 통합 runs API - source 파라미터로 구분
    """
    runs_col = await get_ingestion_runs_collection()
    q: Dict[str, Any] = {}
    
    if source:
        q["source"] = source
    if status:
        q["status"] = status
    if variable:
        q["variable"] = variable
    if stream:
        q["stream"] = stream

    proj = {
        "_id": 0,
        "source": 1,
        "run_time_utc": 1,
        "stream": 1,
        "variable": 1,
        "status": 1,
        "attempts_cnt": 1,
        "started_at_last": 1,
        "finished_at_last": 1,
        "duration_sec_last": 1,
        "trigger_last": 1,
        "counters_last": 1,
        "counters_total": 1,
        "attempts": {"$slice": -5},
        "updated_at": 1,
    }
    
    return (
        await runs_col.find(q, proj)
        .sort([("run_time_utc", -1)])
        .limit(limit)
        .to_list(length=limit)
    )


# ==================== 기존 호환성 유지 (deprecated) ====================

@router.get("/api/ingestion/control", include_in_schema=False)
async def api_ingestion_control_legacy():
    """
    ⚠️ Deprecated: /api/ingestion/control/ecmwf 사용 권장
    기존 호환성 유지를 위해 ECMWF 반환
    """
    return await api_ecmwf_control()


@router.post("/ingestion/pause", include_in_schema=False)
async def ingestion_pause_legacy():
    """
    ⚠️ Deprecated: /ingestion/ecmwf/pause 사용 권장
    기존 호환성 유지를 위해 ECMWF pause
    """
    return await ecmwf_pause()


@router.post("/ingestion/resume", include_in_schema=False)
async def ingestion_resume_legacy():
    """
    ⚠️ Deprecated: /ingestion/ecmwf/resume 사용 권장
    기존 호환성 유지를 위해 ECMWF resume
    """
    return await ecmwf_resume()