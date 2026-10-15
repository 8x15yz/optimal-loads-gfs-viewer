"""Read-only operator dashboard. Aggregations use MongoDB 4.4-compatible operators."""
from __future__ import annotations

import asyncio
import math
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app.db import get_request_logs_collection

security = HTTPBasic(auto_error=False)
templates = Jinja2Templates(directory="app/templates")


def require_operator(credentials: Optional[HTTPBasicCredentials] = Depends(security)):
    username = os.getenv("MONITORING_USERNAME", "")
    password = os.getenv("MONITORING_PASSWORD", "")
    if not username or not password:
        raise HTTPException(503, "Monitoring is not configured. Set MONITORING_USERNAME and MONITORING_PASSWORD.")
    supplied_user = credentials.username if credentials else ""
    supplied_password = credentials.password if credentials else ""
    user_ok = secrets.compare_digest(supplied_user.encode(), username.encode())
    password_ok = secrets.compare_digest(supplied_password.encode(), password.encode())
    if not (credentials and user_ok and password_ok):
        raise HTTPException(401, "Operator login required", headers={"WWW-Authenticate": 'Basic realm="Weather monitoring"'})


router = APIRouter(dependencies=[Depends(require_operator)], include_in_schema=False)


@router.get("/monitoring", response_class=HTMLResponse)
async def monitoring_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="monitoring.html",
        context={"request": request, "active_nav": "monitoring"},
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"},
    )


def make_match(now, hours, category, status, endpoint, client_ip, include_monitoring=False,
               request_id="", account=""):
    match = {"started_at": {"$gte": now - timedelta(hours=hours), "$lt": now}}
    if category != "all":
        match["category"] = category
    elif not include_monitoring:
        match["category"] = {"$ne": "monitoring"}
    if status != "all":
        low = int(status[0]) * 100
        match["status_code"] = {"$gte": low, "$lt": low + 100}
    if endpoint:
        match["path"] = {"$regex": re.escape(endpoint)}
    if client_ip:
        match["client_ip"] = client_ip
    if request_id:
        match["request_id"] = request_id
    if account:
        match["relay_account"] = account
    return match


def make_pipeline(match, bucket_minutes, page, page_size):
    return [{"$match": match}, {"$facet": {
        "overview": [{"$group": {
            "_id": None, "total": {"$sum": 1},
            "client_errors": {"$sum": {"$cond": [{"$and": [
                {"$gte": ["$status_code", 400]}, {"$lt": ["$status_code", 500]},
            ]}, 1, 0]}},
            "server_errors": {"$sum": {"$cond": [{"$gte": ["$status_code", 500]}, 1, 0]}},
            "incomplete": {"$sum": {"$cond": ["$response_complete", 0, 1]}},
            "avg_ms": {"$avg": "$duration_ms"}, "max_ms": {"$max": "$duration_ms"},
        }}],
        "endpoints": [
            {"$group": {"_id": "$endpoint", "count": {"$sum": 1},
                "avg_ms": {"$avg": "$duration_ms"},
                "errors": {"$sum": {"$cond": [{"$gte": ["$status_code", 400]}, 1, 0]}}}},
            {"$sort": {"count": -1, "_id": 1}}, {"$limit": 15},
        ],
        "clients": [
            {"$group": {"_id": "$client_ip", "count": {"$sum": 1}}},
            {"$sort": {"count": -1, "_id": 1}}, {"$limit": 10},
        ],
        # Blue-X 등 중계 서버가 X-BlueX-Account로 전달한 계정별 요청 수
        "accounts": [
            {"$match": {"relay_account": {"$type": "string"}}},
            {"$group": {"_id": "$relay_account", "count": {"$sum": 1},
                "errors": {"$sum": {"$cond": [{"$gte": ["$status_code", 400]}, 1, 0]}}}},
            {"$sort": {"count": -1, "_id": 1}}, {"$limit": 10},
        ],
        "timeline": [
            {"$group": {
                "_id": {"$floor": {"$divide": [{"$subtract": [
                    "$started_at", datetime(1970, 1, 1, tzinfo=timezone.utc),
                ]}, bucket_minutes * 60000]}},
                "count": {"$sum": 1},
                "errors": {"$sum": {"$cond": [{"$gte": ["$status_code", 400]}, 1, 0]}},
            }}, {"$sort": {"_id": 1}},
        ],
        "requests": [
            {"$sort": {"started_at": -1, "_id": -1}},
            {"$skip": (page - 1) * page_size}, {"$limit": page_size},
            {"$project": {"expires_at": 0}},
        ],
    }}]


@router.get("/api/monitoring/data")
async def monitoring_data(
    request: Request, response: Response,
    hours: int = Query(24, ge=1, le=168),
    category: Literal["all", "data_api", "admin_api", "page", "static", "monitoring", "unknown"] = "all",
    include_monitoring: bool = Query(False),
    status: Literal["all", "2xx", "3xx", "4xx", "5xx"] = "all",
    endpoint: str = Query("", max_length=256),
    client_ip: str = Query("", max_length=128),
    request_id: str = Query("", max_length=128),
    account: str = Query("", max_length=128),
    page: int = Query(1, ge=1, le=1000),
):
    response.headers["Cache-Control"] = "no-store"
    now = datetime.now(timezone.utc)
    page_size = 50
    bucket_minutes = 5 if hours <= 6 else (60 if hours <= 48 else 360)
    match = make_match(now, hours, category, status, endpoint.strip(), client_ip.strip(), include_monitoring,
                       request_id.strip(), account.strip())
    coll = await get_request_logs_collection()

    async def query():
        rows = await coll.aggregate(
            make_pipeline(match, bucket_minutes, page, page_size),
            maxTimeMS=5000, allowDiskUse=True,
        ).to_list(length=1)
        data = rows[0]
        overview = data["overview"][0] if data["overview"] else {
            "total": 0, "client_errors": 0, "server_errors": 0, "incomplete": 0,
            "avg_ms": 0, "max_ms": 0,
        }
        overview.pop("_id", None)
        overview["p95_ms"] = None
        if overview["total"]:
            # Exact nearest-rank percentile, without MongoDB 7-only $percentile.
            p95 = await coll.aggregate([
                {"$match": match}, {"$sort": {"duration_ms": 1}},
                {"$skip": math.ceil(overview["total"] * 0.95) - 1}, {"$limit": 1},
                {"$project": {"_id": 0, "duration_ms": 1}},
            ], maxTimeMS=5000, allowDiskUse=True).to_list(length=1)
            overview["p95_ms"] = p95[0]["duration_ms"] if p95 else None
        data["overview"] = overview
        return data

    try:
        data = await asyncio.wait_for(query(), timeout=12)
    except Exception:
        raise HTTPException(503, "Request logs are temporarily unavailable. Try a shorter time range.")
    # Fill empty time buckets so gaps are not drawn as continuous activity.
    buckets = {int(row["_id"]): row for row in data["timeline"]}
    for row in data["requests"]:
        if row["started_at"].tzinfo is None:
            row["started_at"] = row["started_at"].replace(tzinfo=timezone.utc)
    step_seconds = bucket_minutes * 60
    start = int((now - timedelta(hours=hours)).timestamp() // step_seconds)
    end = int(now.timestamp() // step_seconds)
    data["timeline"] = [
        {"time": datetime.fromtimestamp(i * step_seconds, timezone.utc),
         "count": buckets.get(i, {}).get("count", 0), "errors": buckets.get(i, {}).get("errors", 0)}
        for i in range(start, end + 1)
    ]
    writer = getattr(request.app.state, "request_log_writer", None)
    data.update({"generated_at": now, "bucket_minutes": bucket_minutes, "page": page,
                 "page_size": page_size, "writer": writer.health() if writer else None})
    return data
