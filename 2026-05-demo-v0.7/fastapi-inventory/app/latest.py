# app/latest.py
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone, timedelta, date

import boto3
from fastapi import APIRouter, HTTPException

DAILY_LIMIT = 50
_rate: tuple[date, int] = (date.min, 0)

def _check_rate_limit():
    global _rate
    today = datetime.now(timezone.utc).date()
    last_date, count = _rate
    count = count + 1 if last_date == today else 1
    _rate = (today, count)
    if count > DAILY_LIMIT:
        raise HTTPException(status_code=429, detail=f"Daily limit ({DAILY_LIMIT}) exceeded.")

from app.db import get_assets_collection, get_ingestion_runs_collection, get_s100_assets_collection

AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-2")
S3_BUCKET  = os.getenv("S3_BUCKET", "optimal-loads")
_s3 = boto3.client("s3", region_name=AWS_REGION)

EXPIRES_IN = 3600  # presigned URL and manifest validity period (seconds)

latest_router = APIRouter(prefix="/api", tags=["latest"])

# ---- Variable metadata (static) ----
_VARIABLES_META = {
    "WDIR":  {
        "name_en": "Wind Direction",
        "unit": "degree",
        "direction": "from",
        "source": "noaa/gfs",
        "description": (
            "Direction the wind blows from, in degrees clockwise from north "
            "(0=N, 90=E, 180=S, 270=W). "
            "Ex) 225 means wind coming from the southwest (a southwesterly wind)."
        ),
    },
    "WIND":  {
        "name_en": "Wind Speed",
        "unit": "m/s",
        "direction": None,
        "source": "noaa/gfs",
        "description": "Wind strength only; magnitude with no direction information.",
    },
    "HTSGW": {
        "name_en": "Significant Wave Height",
        "unit": "m",
        "direction": None,
        "source": "noaa/gfs",
        "description": (
            "Average height of the highest one-third of waves. "
            "Wave height is measured from the trough (lowest point) to the crest (highest point)."
        ),
    },
    "DIRPW": {
        "name_en": "Peak Wave Direction",
        "unit": "degree",
        "direction": "from",
        "source": "noaa/gfs",
        "description": (
            "When many waves overlap, the direction from which the wave carrying the most "
            "energy comes - i.e. the direction of the dominant swell at that moment. "
            "Same angle convention as wind direction."
        ),
    },
    "PERPW": {
        "name_en": "Peak Wave Period",
        "unit": "s",
        "direction": None,
        "source": "noaa/gfs",
        "description": (
            "Period of the wave carrying the most energy. A long period (roughly 12-20 s) "
            "indicates a swell that traveled from far away; a short period (roughly 4-6 s) "
            "indicates a wind-driven wave formed nearby. "
            "Shorter period = narrower spacing between waves."
        ),
    },
    "UGRD":  {
        "name_en": "Eastward Current",
        "unit": "m/s",
        "direction": "from",
        "source": "noaa/gfs",
        "description": (
            "East-west component of the current. Positive = eastward component, "
            "negative = westward. Combine with VGRD to get the actual current direction and speed."
        ),
    },
    "VGRD":  {
        "name_en": "Northward Current",
        "unit": "m/s",
        "direction": "from",
        "source": "noaa/gfs",
        "description": (
            "North-south component of the current. "
            "Positive = northward component, negative = southward."
        ),
    },
    "tidal_elevation": {
        "name_en": "Astronomical Tide Height",
        "unit": "m",
        "direction": None,
        "source": "eot20/tide",
        "description": (
            "Sea-surface height driven only by the gravitational pull of celestial bodies "
            "such as the Moon and Sun, which makes the water rise and fall on a regular cycle. "
            "Weather effects such as wind, pressure, and storms are not included. "
            "Excludes: storm surge, weather-driven sea-level changes, wave height, wind setup, "
            "river discharge, observed sea level, and real-time data assimilation. "
            "Spatial grid: 0.25 x 0.25 deg lat/lon. "
            "Time structure: tides have no forecast run concept, so run_time_utc = valid_time_utc "
            "and step_hours = 0; generated at 3-hour intervals (00Z/03Z/.../21Z)."
        ),
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
        "Returns all presigned download URLs for the latest weather data as of the request time. "
        "Called without parameters, it returns the full file list for NOAA GFS Wave "
        "(7 variables x 89 steps) and EOT20 astronomical tide (8 steps for the day) in a single response. "
        "The manifest is a one-time snapshot; after expires_at, all included URLs expire."
    ),
)
async def get_latest():
    _check_rate_limit()
    now = _now_utc()
    generated_at = _to_z(now)
    expires_at   = _to_z(now + timedelta(seconds=EXPIRES_IN))

    coll       = await get_assets_collection()
    runs_coll  = await get_ingestion_runs_collection()
    s100_coll  = await get_s100_assets_collection()

    # -- 1. Look up the latest completed GFS run_time_utc ----------
    # Two most recent run_time candidates (skip runs still processing)
    run_candidates = await coll.aggregate([
        {"$match": {"source": "noaa", "model": "gfs", "dataset_code": "original"}},
        {"$group": {"_id": "$run_time_utc"}},
        {"$sort": {"_id": -1}},
        {"$limit": 2},
    ]).to_list(2)

    if not run_candidates:
        raise HTTPException(status_code=503, detail="No NOAA GFS data available.")

    async def _is_run_ready(run_time) -> bool:
        # If a GFS download or conversion job is still running, the run is incomplete
        running = await runs_coll.count_documents({
            "source": "noaa",
            "run_time_utc": run_time,
            "status": "running",
        })
        if running > 0:
            return False
        # If s111 / s413 tiles are missing, conversion is incomplete
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

    # If all candidates are incomplete, fall back to the most recent one
    if latest_run is None:
        raw = run_candidates[0]["_id"]
        latest_run = _to_z(raw) if isinstance(raw, datetime) else raw

    # -- 2. Query all GFS documents for this run -------------------
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

    # -- 3. Astronomical tide - 0~384h range relative to the GFS run
    # Convert latest_run to datetime
    if isinstance(latest_run, datetime):
        latest_run_dt = latest_run if latest_run.tzinfo else latest_run.replace(tzinfo=timezone.utc)
    else:
        latest_run_dt = datetime.fromisoformat(latest_run.replace("Z", "+00:00"))

    tide_cutoff_3h_dt  = latest_run_dt + timedelta(hours=144)  # after 144h -> 6h interval
    tide_cutoff_end_dt = latest_run_dt + timedelta(hours=384)  # end of forecast

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

    # 0~144h: all 3-hour steps / 144~384h: 6-hour steps only (00, 06, 12, 18Z)
    tide_docs = [
        doc for doc in tide_docs_raw
        if _doc_run_dt(doc) <= tide_cutoff_3h_dt or _doc_run_dt(doc).hour % 6 == 0
    ]

    # -- 4. Generate presigned URLs in parallel --------------------
    all_docs = gfs_docs + tide_docs
    s3_keys  = [
        doc["s3"]["key"]
        for doc in all_docs
        if doc.get("s3", {}).get("key")
    ]
    urls = await asyncio.gather(*[_make_presigned(k) for k in s3_keys])
    key_to_url: dict[str, str] = dict(zip(s3_keys, urls))

    # -- 5. Build assets -------------------------------------------
    assets: dict = {}

    # GFS - group by variable
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

    # EOT20 astronomical tide
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

    # -- 6. Return the final manifest ------------------------------
    total_files        = sum(a["file_count"] for a in assets.values())
    variables_included = list(assets.keys())

    return {
        "schema_version": "1.0",
        "issued": {
            "generated_at":     generated_at,
            "expires_at":       expires_at,
            "expires_in_seconds": EXPIRES_IN,
            "note": (
                "This manifest is a one-time snapshot as of its issue time. "
                "After expires_at, all included presigned URLs expire. "
                "If you need the latest data, call /api/latest again."
            ),
        },
        "summary": {
            "total_files":        total_files,
            "variables_included": variables_included,
        },
        "variables": {k: _VARIABLES_META[k] for k in variables_included if k in _VARIABLES_META},
        "assets": assets,
    }
