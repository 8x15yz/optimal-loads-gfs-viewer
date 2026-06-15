# app/latest.py
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone, timedelta

import boto3
from fastapi import APIRouter, HTTPException

from app.db import get_assets_collection

AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-2")
S3_BUCKET  = os.getenv("S3_BUCKET", "optimal-loads")
_s3 = boto3.client("s3", region_name=AWS_REGION)

EXPIRES_IN = 3600  # presigned URL 및 매니페스트 유효 기간 (초)

latest_router = APIRouter(prefix="/api", tags=["latest"])

# ---- 변수 메타데이터 (정적) ----
_VARIABLES_META = {
    "WDIR":  {
        "name_en": "Wind Direction",
        "unit": "degree",
        "direction": "from",
        "source": "noaa/gfs",
        "description": "Ex) 225 → 남서풍 (바람이 불어오는 방향)",
    },
    "WIND":  {
        "name_en": "Wind Speed",
        "unit": "m/s",
        "direction": None,
        "source": "noaa/gfs",
        "description": "바람 세기",
    },
    "HTSGW": {
        "name_en": "Significant Wave Height",
        "unit": "m",
        "direction": None,
        "source": "noaa/gfs",
        "description": "유의파고 (상위 30% 평균)",
    },
    "DIRPW": {
        "name_en": "Peak Wave Direction",
        "unit": "degree",
        "direction": "to",
        "source": "noaa/gfs",
        "description": "Ex) 225 → 너울이 225도 방향으로 흘러감",
    },
    "PERPW": {
        "name_en": "Peak Wave Period",
        "unit": "s",
        "direction": None,
        "source": "noaa/gfs",
        "description": "파주기 (짧을수록 너울 간격 좁음)",
    },
    "UGRD":  {
        "name_en": "Eastward Current",
        "unit": "m/s",
        "direction": "from",
        "source": "noaa/gfs",
        "description": "동쪽 방향 해류 성분",
    },
    "VGRD":  {
        "name_en": "Northward Current",
        "unit": "m/s",
        "direction": "from",
        "source": "noaa/gfs",
        "description": "북쪽 방향 해류 성분",
    },
    "tidal_elevation": {
        "name_en": "Astronomical Tide Height",
        "unit": "m",
        "direction": None,
        "source": "eot20/tide",
        "description": "천문 조위 (3시간 간격, 당일 전체)",
    },
}

_GFS_VARS = ["WDIR", "WIND", "HTSGW", "DIRPW", "PERPW", "UGRD", "VGRD"]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_valid_time(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return _to_z(v)
    return str(v)


async def _make_presigned(s3_key: str) -> str:
    return await asyncio.to_thread(
        _s3.generate_presigned_url,
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": s3_key},
        ExpiresIn=EXPIRES_IN,
    )


@latest_router.get(
    "/latest",
    summary="Latest marine weather data manifest",
    description=(
        "요청 시점 기준 최신 기상 데이터의 presigned 다운로드 URL을 모두 반환합니다. "
        "파라미터 없이 호출하면 NOAA GFS Wave(7변수 × 89 step)와 "
        "EOT20 천문조위(당일 8 step)의 전체 파일 목록을 한 번에 받을 수 있습니다. "
        "매니페스트 자체가 1회성 스냅샷이며, expires_at 이후에는 포함된 모든 URL이 만료됩니다."
    ),
)
async def get_latest():
    now = _now_utc()
    generated_at = _to_z(now)
    expires_at   = _to_z(now + timedelta(seconds=EXPIRES_IN))

    coll = await get_assets_collection()

    # ── 1. 최신 GFS run_time_utc 조회 ──────────────────────────────
    latest_gfs_doc = await coll.find_one(
        {"source": "noaa", "model": "gfs", "dataset_code": "original"},
        sort=[("run_time_utc", -1)],
    )
    if not latest_gfs_doc:
        raise HTTPException(status_code=503, detail="No NOAA GFS data available.")

    latest_run = latest_gfs_doc["run_time_utc"]  # "2026-06-14T18:00:00Z"
    if isinstance(latest_run, datetime):
        latest_run = _to_z(latest_run)

    # ── 2. 해당 run의 GFS 전체 문서 조회 ───────────────────────────
    gfs_docs = await coll.find(
        {
            "source": "noaa",
            "model": "gfs",
            "dataset_code": "original",
            "variable": {"$in": _GFS_VARS},
            "run_time_utc": latest_run,
        },
        {"variable": 1, "step_hours": 1, "valid_time_utc": 1,
         "size_bytes": 1, "format": 1, "s3": 1, "_id": 0},
    ).sort([("variable", 1), ("step_hours", 1)]).to_list(length=10000)

    # ── 3. 오늘 날짜 천문조위 문서 조회 ────────────────────────────
    today_run = now.strftime("%Y-%m-%dT00:00:00Z")  # "2026-06-15T00:00:00Z"
    tide_docs = await coll.find(
        {
            "source": "eot20",
            "model": "tide",
            "dataset_code": "computed",
            "variable": "tidal_elevation",
            "run_time_utc": today_run,
        },
        {"variable": 1, "step_hours": 1, "valid_time_utc": 1,
         "size_bytes": 1, "format": 1, "s3": 1, "_id": 0},
    ).sort([("step_hours", 1)]).to_list(length=100)

    # ── 4. presigned URL 병렬 생성 ─────────────────────────────────
    all_docs = gfs_docs + tide_docs
    s3_keys  = [
        doc["s3"]["key"]
        for doc in all_docs
        if doc.get("s3", {}).get("key")
    ]
    urls = await asyncio.gather(*[_make_presigned(k) for k in s3_keys])
    key_to_url: dict[str, str] = dict(zip(s3_keys, urls))

    # ── 5. assets 구성 ─────────────────────────────────────────────
    assets: dict = {}

    # GFS — 변수별 그룹핑
    gfs_by_var: dict[str, list] = {v: [] for v in _GFS_VARS}
    for doc in gfs_docs:
        var = doc.get("variable", "")
        if var in gfs_by_var:
            gfs_by_var[var].append(doc)

    for var in _GFS_VARS:
        docs = gfs_by_var[var]
        if not docs:
            continue
        steps = [
            {
                "step_hours":    doc.get("step_hours"),
                "valid_time_utc": _safe_valid_time(doc.get("valid_time_utc")),
                "href":          key_to_url.get((doc.get("s3") or {}).get("key", ""), ""),
                "size_bytes":    doc.get("size_bytes"),
                "s3_key":        (doc.get("s3") or {}).get("key", ""),
            }
            for doc in docs
        ]
        assets[var] = {
            "source":       "noaa",
            "model":        "gfs",
            "dataset_code": "original",
            "run_time_utc": latest_run,
            "format":       docs[0].get("format"),
            "file_count":   len(steps),
            "steps":        steps,
        }

    # EOT20 천문조위
    if tide_docs:
        tide_steps = [
            {
                "step_hours":    doc.get("step_hours"),
                "valid_time_utc": _safe_valid_time(doc.get("valid_time_utc")),
                "href":          key_to_url.get((doc.get("s3") or {}).get("key", ""), ""),
                "size_bytes":    doc.get("size_bytes"),
                "s3_key":        (doc.get("s3") or {}).get("key", ""),
            }
            for doc in tide_docs
        ]
        assets["tidal_elevation"] = {
            "source":       "eot20",
            "model":        "tide",
            "dataset_code": "computed",
            "date":         now.strftime("%Y-%m-%d"),  # run_time_utc 대신 date 사용
            "format":       tide_docs[0].get("format"),
            "file_count":   len(tide_steps),
            "steps":        tide_steps,
        }

    # ── 6. 최종 매니페스트 반환 ────────────────────────────────────
    total_files        = sum(a["file_count"] for a in assets.values())
    variables_included = list(assets.keys())

    return {
        "schema_version": "1.0",
        "issued": {
            "generated_at":     generated_at,
            "expires_at":       expires_at,
            "expires_in_seconds": EXPIRES_IN,
            "note": (
                "이 매니페스트는 발급 시점 기준 1회성 스냅샷입니다. "
                "expires_at 이후에는 포함된 모든 presigned URL이 만료됩니다. "
                "최신 데이터가 필요하면 /api/latest를 재호출하세요."
            ),
        },
        "summary": {
            "total_files":        total_files,
            "variables_included": variables_included,
        },
        "variables": {k: _VARIABLES_META[k] for k in variables_included if k in _VARIABLES_META},
        "assets": assets,
    }
