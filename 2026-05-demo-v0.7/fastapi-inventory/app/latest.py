# app/latest.py
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone, timedelta

import boto3
from fastapi import APIRouter, HTTPException

from app.db import get_assets_collection, get_ingestion_runs_collection, get_s100_assets_collection

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

    coll       = await get_assets_collection()
    runs_coll  = await get_ingestion_runs_collection()
    s100_coll  = await get_s100_assets_collection()

    # ── 1. 완료된 최신 GFS run_time_utc 조회 ───────────────────────
    # 최근 run_time 후보 2개 (아직 처리 중인 런은 건너뜀)
    run_candidates = await coll.aggregate([
        {"$match": {"source": "noaa", "model": "gfs", "dataset_code": "original"}},
        {"$group": {"_id": "$run_time_utc"}},
        {"$sort": {"_id": -1}},
        {"$limit": 2},
    ]).to_list(2)

    if not run_candidates:
        raise HTTPException(status_code=503, detail="No NOAA GFS data available.")

    async def _is_run_ready(run_time) -> bool:
        # GFS 다운로드 또는 변환 중인 작업이 있으면 미완료
        running = await runs_coll.count_documents({
            "source": "noaa",
            "run_time_utc": run_time,
            "status": "running",
        })
        if running > 0:
            return False
        # s111 / s413 타일이 없으면 변환 미완료
        s111 = await s100_coll.count_documents({"product": "s111", "run_time_utc": run_time})
        s413 = await s100_coll.count_documents({"product": "s413", "run_time_utc": run_time})
        return s111 > 0 and s413 > 0

    latest_run = None
    for candidate in run_candidates:
        run_time = candidate["_id"]
        if isinstance(run_time, datetime):
            run_time = _to_z(run_time)
        if await _is_run_ready(run_time):
            latest_run = run_time
            break

    # 후보 모두 미완료면 가장 최근 것으로 fallback
    if latest_run is None:
        raw = run_candidates[0]["_id"]
        latest_run = _to_z(raw) if isinstance(raw, datetime) else raw

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

    # ── 3. 천문조위 - GFS run 기준 0~384h 범위 ─────────────────────
    # latest_run → datetime 변환
    if isinstance(latest_run, datetime):
        latest_run_dt = latest_run if latest_run.tzinfo else latest_run.replace(tzinfo=timezone.utc)
    else:
        latest_run_dt = datetime.fromisoformat(latest_run.replace("Z", "+00:00"))

    tide_cutoff_3h_dt  = latest_run_dt + timedelta(hours=144)  # 144h 이후 → 6h 간격
    tide_cutoff_end_dt = latest_run_dt + timedelta(hours=384)  # 예측 끝

    tide_range_start = _to_z(latest_run_dt)
    tide_range_end   = _to_z(tide_cutoff_end_dt)

    tide_docs_raw = await coll.find(
        {
            "source": "eot20",
            "model": "tide",
            "dataset_code": "computed",
            "variable": "tidal_elevation",
            "run_time_utc": {"$gte": tide_range_start, "$lte": tide_range_end},
        },
        {"variable": 1, "valid_time_utc": 1,
         "size_bytes": 1, "format": 1, "s3": 1, "run_time_utc": 1, "_id": 0},
    ).sort([("run_time_utc", 1)]).to_list(length=500)

    def _doc_run_dt(doc) -> datetime:
        v = doc.get("run_time_utc")
        if isinstance(v, datetime):
            return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))

    # 0~144h: 3시간 간격 전부 / 144~384h: 6시간 간격(00,06,12,18Z)만
    tide_docs = [
        doc for doc in tide_docs_raw
        if _doc_run_dt(doc) <= tide_cutoff_3h_dt or _doc_run_dt(doc).hour % 6 == 0
    ]

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
        tide_steps = []
        for doc in tide_docs:
            dt = _doc_run_dt(doc)
            step_h = int((dt - latest_run_dt).total_seconds() / 3600)
            tide_steps.append({
                "step_hours":     step_h,
                "valid_time_utc": _safe_valid_time(dt),
                "href":           key_to_url.get((doc.get("s3") or {}).get("key", ""), ""),
                "size_bytes":     doc.get("size_bytes"),
                "s3_key":         (doc.get("s3") or {}).get("key", ""),
            })
        assets["tidal_elevation"] = {
            "source":           "eot20",
            "model":            "tide",
            "dataset_code":     "computed",
            "gfs_run_time_utc": latest_run if isinstance(latest_run, str) else _to_z(latest_run_dt),
            "tide_range":       {"from": tide_range_start, "to": tide_range_end},
            "format":           tide_docs[0].get("format"),
            "file_count":       len(tide_steps),
            "steps":            tide_steps,
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
