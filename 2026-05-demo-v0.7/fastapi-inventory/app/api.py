# api.py
from __future__ import annotations

from fastapi import APIRouter, Query, HTTPException
from typing import Optional, List
from datetime import datetime, timezone, timedelta

from app.models import GridDataResponse
from app.db import get_assets_collection, get_directories_collection, get_s100_assets_collection


import numpy as np
import xarray as xr
import tempfile, os, contextlib, boto3
import asyncio

# ---- env / S3 ----
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-2")
S3_BUCKET  = os.getenv("S3_BUCKET", "optimal-loads")
s3 = boto3.client("s3", region_name=AWS_REGION)

# ---- dataset code 상수 ----
DATASET_CODE_ALL_STEPS = "original-all-steps"

# =============================================================================
# S-100 Router
# =============================================================================
s100_router = APIRouter(prefix="/api", tags=["s100"])

# ---- S-102 (GEBCO) 타일 그리드 상수 ----
# 23행 × 45열 = 1,035 타일 / 타일당 ~8° × 8°
# S3: gebco/s102/2025/Split/102KRTDGEBCO_2025_{N}.h5
_S102_ROW_SOUTH = [
     82.0021,  74.0021,  66.0021,  58.0021,  50.0021,  42.0021,  34.0021,
     26.0021,  18.0021,  10.0021,   2.0021,  -5.9979, -13.9979, -21.9979,
    -29.9979, -37.9979, -45.9979, -53.9979, -61.9979, -69.9979, -77.9979,
    -85.9979, -89.9979,
]
_S102_ROW_NORTH = [89.9979] + _S102_ROW_SOUTH[:-1]   # row 0 north = 89.9979
_S102_COL_WEST  = [-179.9979 + i * 8.0 for i in range(45)]
_S102_COL_EAST  = [-179.9979 + (i + 1) * 8.0 for i in range(45)]
_S102_S3_PREFIX = "gebco/s102/2025/Split"
_S102_TILE_LIMIT = 50


# ---- S-111 / S-413 타일 상수 ----
_RES        = 0.25
_N_LAT      = 721
_N_LON      = 1440
_S111_TILE_DEG  = 20.0
_S413_TILE_DEG  = 20.0
_S1X1_TILE_LIMIT = 30   # presigned URL 한 번에 최대 타일 수

_S1X1_TILE_DEG = {
    "s111": _S111_TILE_DEG,
    "s413": _S413_TILE_DEG,
}


def _s1x1_find_tiles(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float,
    tile_deg: float,
) -> list[dict]:
    """
    bbox에 걸치는 S-111/S-413 타일 목록 반환.
    타일 경계: 북 = 90 - row*tile_deg, 남 = 90 - (row+1)*tile_deg
               서 = -180 + col*tile_deg, 동 = -180 + (col+1)*tile_deg
    view_tiles.py / countForecastTiles(JS)와 동일한 20° 균등 격자.
    """
    total_rows    = round(180.0 / tile_deg)   # 9
    tiles_per_row = round(360.0 / tile_deg)   # 18

    result = []
    for row in range(total_rows):
        north = round(90.0 - row * tile_deg, 4)
        south = round(max(90.0 - (row + 1) * tile_deg, -90.0), 4)

        if south >= max_lat or north <= min_lat:
            continue

        for col in range(tiles_per_row):
            west = round(-180.0 + col * tile_deg, 4)
            east = round(west + tile_deg, 4)

            if west >= max_lon or east <= min_lon:
                continue

            tile_idx = row * tiles_per_row + col + 1
            result.append({
                "tile_idx": tile_idx,
                "bbox": [west, south, east, north],
            })

    return result


def _s102_find_tiles(
    min_lon: float, min_lat: float, max_lon: float, max_lat: float
) -> list[dict]:
    tiles = []
    for r in range(23):
        if _S102_ROW_SOUTH[r] >= max_lat or _S102_ROW_NORTH[r] <= min_lat:
            continue
        for c in range(45):
            if _S102_COL_WEST[c] >= max_lon or _S102_COL_EAST[c] <= min_lon:
                continue
            n = r * 45 + c + 1
            tiles.append({
                "tile_number": n,
                "filename": f"102KRTDGEBCO_2025_{n}.h5",
                "s3_key": f"{_S102_S3_PREFIX}/102KRTDGEBCO_2025_{n}.h5",
                "tile_bbox": [
                    round(_S102_COL_WEST[c], 4),
                    round(_S102_ROW_SOUTH[r], 4),
                    round(_S102_COL_EAST[c], 4),
                    round(_S102_ROW_NORTH[r], 4),
                ],
            })
    return tiles


@s100_router.get(
    "/s100/tiles",
    summary="Get S-100 tile presigned URLs",
    description=(
        "bbox에 걸치는 S-100 타일 목록과 S3 presigned URL을 반환합니다. "
        "현재 product=s102 (GEBCO 수심) 지원."
    ),
)
async def get_s100_tiles(
    product: str = Query(..., example="s102", description="S-100 product code"),

    # ---- 공간 방식 1: NW/SE 코너 ----
    nw_lon: Optional[float] = Query(default=None, example=128.0),
    nw_lat: Optional[float] = Query(default=None, example=36.0),
    se_lon: Optional[float] = Query(default=None, example=130.0),
    se_lat: Optional[float] = Query(default=None, example=34.0),

    # ---- 공간 방식 2: 중심점 + 버퍼 ----
    lat: Optional[float] = Query(default=None, example=35.0),
    lon: Optional[float] = Query(default=None, example=129.0),
    buffer_km: Optional[float] = Query(default=None, ge=0.0, le=5000.0, example=500.0),

    expires_in: int = Query(default=3600, ge=60, le=86400, description="Presigned URL 유효 시간(초)"),
):
    if product.lower() != "s102":
        raise HTTPException(
            status_code=400,
            detail={"error": f"Unsupported product '{product}'. Currently only 's102' is available."},
        )

    # ---- bbox 결정 ----
    nw_se = [nw_lon, nw_lat, se_lon, se_lat]
    cb    = [lat, lon, buffer_km]

    if all(p is not None for p in nw_se):
        if any(p is not None for p in cb):
            raise HTTPException(
                status_code=422,
                detail={"error": "Cannot mix nw/se and lat/lon/buffer params."},
            )
        min_lon_v, min_lat_v, max_lon_v, max_lat_v = nw_lon, se_lat, se_lon, nw_lat

    elif lat is not None and lon is not None:
        buf = buffer_km if buffer_km is not None else 500.0
        bdlat = buf / 111.0
        bdlon = buf / (111.0 * np.cos(np.radians(lat)))
        min_lon_v = lon - bdlon
        min_lat_v = lat - bdlat
        max_lon_v = lon + bdlon
        max_lat_v = lat + bdlat

    else:
        raise HTTPException(
            status_code=422,
            detail={"error": "Provide (nw_lon, nw_lat, se_lon, se_lat) or (lat, lon) with optional buffer_km."},
        )

    tiles = _s102_find_tiles(min_lon_v, min_lat_v, max_lon_v, max_lat_v)

    if not tiles:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "No tiles found in the requested bbox.",
                "bbox": [min_lon_v, min_lat_v, max_lon_v, max_lat_v],
            },
        )
    if len(tiles) > _S102_TILE_LIMIT:
        raise HTTPException(
            status_code=413,
            detail={
                "error": f"Too many tiles ({len(tiles)}) in bbox. Limit is {_S102_TILE_LIMIT}.",
                "tile_count": len(tiles),
                "limit": _S102_TILE_LIMIT,
                "hint": "Reduce the bbox size.",
            },
        )

    # ---- MongoDB에서 s3_key 조회 ----
    coll = await get_s100_assets_collection()
    tile_idx_list = [f"{t['tile_number']:04d}" for t in tiles]
    docs = await coll.find(
        {"product": "s102", "tile.idx": {"$in": tile_idx_list}},
        {"tile": 1, "s3": 1, "_id": 0},
    ).to_list(length=_S102_TILE_LIMIT + 10)
    doc_map = {d["tile"]["idx"]: d for d in docs}

    result = []
    missing = []
    for t in tiles:
        tile_idx_str = f"{t['tile_number']:04d}"
        doc = doc_map.get(tile_idx_str)
        if doc is None:
            missing.append(t["tile_number"])
            continue
        s3_key = doc["s3"]["key"]
        try:
            url = await asyncio.to_thread(
                s3.generate_presigned_url,
                "get_object",
                Params={"Bucket": S3_BUCKET, "Key": s3_key},
                ExpiresIn=expires_in,
            )
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail={"error": "Failed to generate presigned URL.", "tile": t["tile_number"], "reason": str(e)},
            )
        result.append({**t, "s3_key": s3_key, "presigned_url": url})

    return {
        "product": product.lower(),
        "tile_count": len(result),
        "missing_tiles": missing,
        "bbox_requested": [
            round(min_lon_v, 4), round(min_lat_v, 4),
            round(max_lon_v, 4), round(max_lat_v, 4),
        ],
        "expires_in_seconds": expires_in,
        "tiles": result,
    }


@s100_router.get(
    "/s100/forecast-tiles",
    summary="Get S-111/S-413 forecast tile presigned URLs",
    description=(
        "run_time_utc + bbox로 S-111(해류) 또는 S-413(기상/파랑) 타일 목록과 "
        "S3 presigned URL을 반환합니다."
    ),
)
async def get_forecast_tiles(
    product: str = Query(..., example="s413",
                         description="s111 또는 s413"),
    run_time_utc: str = Query(..., example="2026-03-03T12:00:00Z",
                              description="런타임 (UTC ISO-8601)"),

    # ---- 공간 방식 1: NW/SE 코너 ----
    nw_lon: Optional[float] = Query(default=None, example=118.0),
    nw_lat: Optional[float] = Query(default=None, example=42.0),
    se_lon: Optional[float] = Query(default=None, example=132.0),
    se_lat: Optional[float] = Query(default=None, example=30.0),

    # ---- 공간 방식 2: 중심점 + 버퍼 ----
    lat: Optional[float] = Query(default=None, example=35.0),
    lon: Optional[float] = Query(default=None, example=129.0),
    buffer_km: Optional[float] = Query(default=None, ge=0.0, le=5000.0,
                                       example=500.0),

    expires_in: int = Query(default=3600, ge=60, le=86400,
                            description="Presigned URL 유효 시간(초)"),
):
    product_lower = product.strip().lower()
    if product_lower not in _S1X1_TILE_DEG:
        raise HTTPException(
            status_code=400,
            detail={"error": f"Unsupported product '{product}'. Use 's111' or 's413'."},
        )

    # ---- run_time_utc 파싱 ----
    try:
        run_dt = _parse_utc(run_time_utc)
    except Exception:
        raise HTTPException(
            status_code=422,
            detail={"error": f"Invalid run_time_utc: '{run_time_utc}'"},
        )

    # ---- bbox 결정 ----
    nw_se = [nw_lon, nw_lat, se_lon, se_lat]
    cb    = [lat, lon, buffer_km]

    if all(p is not None for p in nw_se):
        if any(p is not None for p in cb):
            raise HTTPException(
                status_code=422,
                detail={"error": "Cannot mix nw/se and lat/lon/buffer params."},
            )
        min_lon_v, min_lat_v, max_lon_v, max_lat_v = nw_lon, se_lat, se_lon, nw_lat

    elif lat is not None and lon is not None:
        buf   = buffer_km if buffer_km is not None else 500.0
        bdlat = buf / 111.0
        bdlon = buf / (111.0 * np.cos(np.radians(lat)))
        min_lon_v = lon - bdlon
        min_lat_v = lat - bdlat
        max_lon_v = lon + bdlon
        max_lat_v = lat + bdlat

    else:
        raise HTTPException(
            status_code=422,
            detail={"error": "Provide (nw_lon, nw_lat, se_lon, se_lat) or (lat, lon) with optional buffer_km."},
        )

    tile_deg = _S1X1_TILE_DEG[product_lower]

    # ---- bbox 교차 타일 계산 ----
    candidate_tiles = _s1x1_find_tiles(
        min_lon_v, min_lat_v, max_lon_v, max_lat_v, tile_deg
    )

    if not candidate_tiles:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "No tiles found in the requested bbox.",
                "bbox": [round(min_lon_v, 4), round(min_lat_v, 4),
                         round(max_lon_v, 4), round(max_lat_v, 4)],
            },
        )
    if len(candidate_tiles) > _S1X1_TILE_LIMIT:
        raise HTTPException(
            status_code=413,
            detail={
                "error": f"Too many tiles ({len(candidate_tiles)}) in bbox. Limit is {_S1X1_TILE_LIMIT}.",
                "tile_count": len(candidate_tiles),
                "limit": _S1X1_TILE_LIMIT,
                "hint": "Reduce the bbox size.",
            },
        )

    # ---- MongoDB에서 해당 런타임 + 타일 메타 조회 ----
    coll = await get_s100_assets_collection()
    run_str = _to_z(run_dt)

    tile_idx_list = [f"{t['tile_idx']:03d}" for t in candidate_tiles]
    docs = await coll.find(
        {
            "product":       product_lower,
            "run_time_utc":  run_str,
            "tile.idx":      {"$in": tile_idx_list},
        },
        {"tile": 1, "s3": 1, "steps": 1, "_id": 0},
    ).to_list(length=_S1X1_TILE_LIMIT + 10)

    # tile_idx → doc 매핑
    doc_map = {d["tile"]["idx"]: d for d in docs}

    # ---- presigned URL 생성 ----
    result = []
    missing = []

    for t in candidate_tiles:
        idx = f"{t['tile_idx']:03d}"
        doc = doc_map.get(idx)

        if doc is None or doc.get("missing") or doc.get("s3") is None:
            missing.append(idx)
            continue

        s3_key = doc["s3"]["key"]
        try:
            url = await asyncio.to_thread(
                s3.generate_presigned_url,
                "get_object",
                Params={"Bucket": S3_BUCKET, "Key": s3_key},
                ExpiresIn=expires_in,
            )
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail={"error": "Failed to generate presigned URL.",
                        "tile_idx": idx, "reason": str(e)},
            )

        result.append({
            "tile_idx":     idx,
            "bbox":         t["bbox"],
            "steps":        doc.get("steps"),
            "s3_key":       s3_key,
            "presigned_url": url,
        })

    return {
        "product":       product_lower,
        "run_time_utc":  run_str,
        "tile_count":    len(result),
        "missing_tiles": missing,
        "bbox_requested": [
            round(min_lon_v, 4), round(min_lat_v, 4),
            round(max_lon_v, 4), round(max_lat_v, 4),
        ],
        "expires_in_seconds": expires_in,
        "tiles": result,
    }

# =============================================================================
# 소스별 변수명 ALIASES
# =============================================================================

# ECMWF 변수명 매핑
ALIASES_ECMWF = {
    "10u": ["10u", "u10"],
    "10v": ["10v", "v10"],
    "swh": ["swh"],
}

# NOAA 변수명 매핑
ALIASES_NOAA = {
    "UGRD": ["u"],
    "VGRD": ["v"],
    "WIND": ["ws"],
    "WDIR": ["wdir"],
    "HTSGW": ["swh"],
    "PERPW": ["perpw"],
    "DIRPW": ["dirpw"],
}


def get_aliases_by_source(source: str) -> dict:
    """소스에 따른 ALIASES 딕셔너리 반환"""
    source_lower = source.lower()
    if source_lower == "noaa":
        return ALIASES_NOAA
    elif source_lower == "ecmwf":
        return ALIASES_ECMWF
    else:
        return ALIASES_ECMWF  # 기본값


# =============================================================================
# bbox limits & cell budget
# =============================================================================
BBOX_LIMITS = {
    "min_lon": -118.8389887,
    "min_lat":  -52.3232408,
    "max_lon":  194.4699022,
    "max_lat":   69.7861536,
}
MAX_CELLS = 5_250_000
ASSUMED_DLON = 0.083
ASSUMED_DLAT = 0.083


# =============================================================================
# Router
# =============================================================================
# router (griddata용)
router = APIRouter(prefix="/api", tags=["grid"])

# meta_router (sources, variables용)
meta_router = APIRouter(prefix="/api", tags=["meta"])

@router.get(
    "/griddata",
    response_model=GridDataResponse,
    summary="Get gridded variable data",
    description="Reads a GRIB2/NetCDF from S3 (by forecast run+step) and returns encoded grid values."
)
async def get_griddata(
    # ---- forecast identity (필수) ----
    source: str = Query(..., example="ecmwf"),
    dataset_code: str = Query(..., example="original"),
    model: str = Query(..., example="ifs"),
    variable: str = Query(..., example="swh"),
    run_time_utc: str = Query(..., example="2025-07-16T00:00:00Z"),
    step_hours: int = Query(..., ge=0, le=360, example=24),

    # ---- spatial (방식 1: 중심점 + 버퍼) ----
    lat: Optional[float] = Query(default=None, example=35.0, description="Center point latitude"),
    lon: Optional[float] = Query(default=None, example=129.0, description="Center point longitude"),
    buffer_km: Optional[float] = Query(default=None, ge=0.0, le=500.0, example=50.0, description="Buffer radius (km)"),

    # ---- spatial (method 2: NW-SE corners) ----
    nw_lon: Optional[float] = Query(default=None, example=128.0, description="Northwest (top-left) longitude"),
    nw_lat: Optional[float] = Query(default=None, example=36.0, description="Northwest (top-left) latitude"),
    se_lon: Optional[float] = Query(default=None, example=130.0, description="Southeast (bottom-right) longitude"),
    se_lat: Optional[float] = Query(default=None, example=34.0, description="Southeast (bottom-right) latitude"),
    ) -> GridDataResponse:
    
    type_ = "forecast"
    
    # ---- bbox 생성 로직 ----
    # 우선순위: 1) nw/se 모서리 → 2) 중심점+버퍼 → 3) 전체 영역
    
    nw_se_params = [nw_lon, nw_lat, se_lon, se_lat]
    center_buffer_params = [lat, lon, buffer_km]
    
    if all(p is not None for p in nw_se_params):
        if any(p is not None for p in center_buffer_params):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "Cannot use both nw/se and lat/lon/buffer parameters simultaneously.",
                    "hint": "Use either (nw_lon, nw_lat, se_lon, se_lat) OR (lat, lon, buffer_km)"
                }
            )
        effective_bbox = [nw_lon, se_lat, se_lon, nw_lat]
    
    elif lat is not None and lon is not None:
        buffer = buffer_km if buffer_km is not None else 50.0
        
        if buffer == 0:
            buffer_deg_lat = 0.001
            buffer_deg_lon = 0.001
        else:
            buffer_deg_lat = buffer / 111.0
            buffer_deg_lon = buffer / (111.0 * np.cos(np.radians(lat)))
            min_buffer_deg = 0.125
            buffer_deg_lat = max(buffer_deg_lat, min_buffer_deg)
            buffer_deg_lon = max(buffer_deg_lon, min_buffer_deg / np.cos(np.radians(lat)))
        
        effective_bbox = [
            lon - buffer_deg_lon,
            lat - buffer_deg_lat,
            lon + buffer_deg_lon,
            lat + buffer_deg_lat,
        ]
    
    else:
        effective_bbox = [
            BBOX_LIMITS["min_lon"], BBOX_LIMITS["min_lat"],
            BBOX_LIMITS["max_lon"], BBOX_LIMITS["max_lat"],
        ]

    _validate_bbox_limits_raw(effective_bbox)
    
    # ---- 변수 정규화 (소스별) ----
    norm_var = _norm_var(variable, source)
    
    # ---- valid_time 계산 (응답용) ----
    run_dt = _parse_utc(run_time_utc)
    valid_dt = run_dt + timedelta(hours=int(step_hours))
    valid_time_utc = _to_z(valid_dt)
    
    # ========== computed wind: U/V 성분 계산 ==========
    if norm_var in ("wind_speed_10m", "wind_dir_10m"):
        coll = await get_assets_collection()
        
        if source.lower() == "noaa":
            u_var = "UGRD"
            v_var = "VGRD"
        else:
            u_var = "10u"
            v_var = "10v"

        # all-steps 여부에 따라 U/V 문서 조회 분기
        if dataset_code == DATASET_CODE_ALL_STEPS:
            doc_u = await coll.find_one({
                "source": source,
                "dataset_code": dataset_code,
                "model": model,
                "variable": u_var,
                "run_time_utc": _to_z(run_dt),
            })
            doc_v = await coll.find_one({
                "source": source,
                "dataset_code": dataset_code,
                "model": model,
                "variable": v_var,
                "run_time_utc": _to_z(run_dt),
            })
        else:
            doc_u = await _find_by_natural_key(
                coll,
                source=source,
                dataset_code="original",
                model=model,
                type_=type_,
                variable=u_var,
                run_time_utc=_to_z(run_dt),
                step_hours=step_hours,
            )
            doc_v = await _find_by_natural_key(
                coll,
                source=source,
                dataset_code="original",
                model=model,
                type_=type_,
                variable=v_var,
                run_time_utc=_to_z(run_dt),
                step_hours=step_hours,
            )
        
        if not (doc_u and doc_v):
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "Required U/V components not found for computed wind at this run+step.",
                    "needed": [u_var, v_var],
                    "source": source,
                    "dataset_code": dataset_code,
                    "run_time_utc": _to_z(run_dt),
                    "step_hours": int(step_hours),
                }
            )
        
        est = _estimate_cells_from_doc_or_assume(effective_bbox, doc_u)
        if est > MAX_CELLS:
            raise HTTPException(
                status_code=413,
                detail={
                    "error": "Requested bbox is too large for this dataset resolution.",
                    "requested_bbox": effective_bbox,
                    "estimated_cells": est,
                    "limit": MAX_CELLS,
                }
            )

        if "s3" not in doc_u or "key" not in doc_u.get("s3", {}):
            raise HTTPException(status_code=409, detail={"error": f"{u_var} metadata has no s3.key"})
        if "s3" not in doc_v or "key" not in doc_v.get("s3", {}):
            raise HTTPException(status_code=409, detail={"error": f"{v_var} metadata has no s3.key"})

        tmpu = tempfile.NamedTemporaryFile(delete=False, suffix=_tmp_suffix_from_doc(doc_u))
        tmpv = tempfile.NamedTemporaryFile(delete=False, suffix=_tmp_suffix_from_doc(doc_v))
        ds_u = ds_v = None
        try:
            s3.download_file(S3_BUCKET, doc_u["s3"]["key"], tmpu.name)
            s3.download_file(S3_BUCKET, doc_v["s3"]["key"], tmpv.name)
            
            ds_u = _open_dataset_safely(tmpu.name)
            ds_v = _open_dataset_safely(tmpv.name)
            
            ds_u = _normalize_grib_coordinates(ds_u, source)
            ds_v = _normalize_grib_coordinates(ds_v, source)

            # all-steps면 원하는 step 추출
            if dataset_code == DATASET_CODE_ALL_STEPS:
                ds_u = _extract_step_from_all_steps(ds_u, step_hours)
                ds_v = _extract_step_from_all_steps(ds_v, step_hours)
            
            da_u, lat_inc_u, _ = _select_da(ds_u, u_var, effective_bbox, source=source)
            da_v, lat_inc_v, _ = _select_da(ds_v, v_var, effective_bbox, source=source)
            
            da_u = _ensure_lat_lon_names(da_u)
            da_v = _ensure_lat_lon_names(da_v)
            
            da_u, da_v = xr.align(da_u, da_v, join="inner")
            
            speed = np.hypot(da_u.values, da_v.values)
            direc = (270.0 - np.degrees(np.arctan2(da_v.values, da_u.values))) % 360.0

            target = speed if norm_var == "wind_speed_10m" else direc
            da_like = da_u

            arr2, dlon, dlat, width, height = _prepare_array_for_response(
                xr.DataArray(target, coords=da_like.coords, dims=da_like.dims),
                lat_inc_u,
            )

            if norm_var == "wind_speed_10m":
                unit_meta, name_en_meta, std_name_meta = "m s-1", "10 m wind speed", "wind_speed_10m"
            else:
                unit_meta, name_en_meta, std_name_meta = "degree", "10 m wind direction (from)", "wind_from_direction"

            arr_flat = arr2.astype(np.float32).flatten()
            data_list = [None if np.isnan(x) else float(x) for x in arr_flat]

            return {
                "timestamp": valid_time_utc,
                "run_time_utc": _to_z(run_dt),
                "step_hours": int(step_hours),
                "valid_time_utc": valid_time_utc,

                "variable": norm_var,
                "unit": unit_meta,
                "name_en": name_en_meta,
                "standard_name": std_name_meta,

                "bbox": [
                    float(da_like["lon"].values.min()),
                    float(da_like["lat"].values.min()),
                    float(da_like["lon"].values.max()),
                    float(da_like["lat"].values.max()),
                ],
                "resolution": [dlon, dlat],
                "shape": [width, height],
                "indexOrder": "row-major-bottom-up",
                "valueEncoding": {"type": "float32", "scale": 1.0, "offset": 0.0, "nodata": None},
                "data": data_list,
            }
        finally:
            with contextlib.suppress(Exception):
                tmpu.close(); os.unlink(tmpu.name)
                tmpv.close(); os.unlink(tmpv.name)
                ds_u and ds_u.close()
                ds_v and ds_v.close()

    # ========== original variable (S3 GRIB2/NC) ==========
    coll = await get_assets_collection()

    # all-steps는 런당 파일 1개 → step_hours 없이 조회
    if dataset_code == DATASET_CODE_ALL_STEPS:
        doc = await coll.find_one({
            "source": source,
            "dataset_code": dataset_code,
            "model": model,
            "variable": norm_var,
            "run_time_utc": _to_z(run_dt),
        })
    else:
        doc = await _find_by_natural_key(
            coll,
            source=source,
            dataset_code=dataset_code,
            model=model,
            type_=type_,
            variable=norm_var,
            run_time_utc=_to_z(run_dt),
            step_hours=step_hours,
        )

    if not doc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "No matching dataset found for this run+step.",
                "source": source,
                "dataset_code": dataset_code,
                "model": model,
                "type": type_,
                "variable": norm_var,
                "run_time_utc": _to_z(run_dt),
                "step_hours": int(step_hours),
            }
        )

    est = _estimate_cells_from_doc_or_assume(effective_bbox, doc)
    if est > MAX_CELLS:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "Requested bbox is too large for this dataset resolution.",
                "requested_bbox": effective_bbox,
                "estimated_cells": est,
                "limit": MAX_CELLS,
            }
        )

    if "s3" not in doc or "key" not in doc.get("s3", {}):
        raise HTTPException(
            status_code=409,
            detail={"error": "This metadata record has no s3.key", "doc_keys": list(doc.keys())}
        )

    s3_key = doc["s3"]["key"]
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=_tmp_suffix_from_doc(doc))
    ds = None
    try:
        s3.download_file(S3_BUCKET, s3_key, tmp.name)
        ds = _open_dataset_safely(tmp.name)
        ds = _normalize_grib_coordinates(ds, source)

        # all-steps면 원하는 step 추출
        if dataset_code == DATASET_CODE_ALL_STEPS:
            ds = _extract_step_from_all_steps(ds, step_hours)

        da, lat_inc, _ = _select_da(ds, norm_var, effective_bbox, source=source)
        da = _ensure_lat_lon_names(da)

        arr2, dlon, dlat, width, height = _prepare_array_for_response(da, lat_inc)

        unit_meta = doc.get("unit")
        name_en_meta = doc.get("name_en")
        std_name_meta = doc.get("standard_name")

        arr_flat = arr2.astype(np.float32).flatten()
        data_list = [None if np.isnan(x) else float(x) for x in arr_flat]

        return {
            "timestamp": valid_time_utc,
            "run_time_utc": _to_z(run_dt),
            "step_hours": int(step_hours),
            "valid_time_utc": valid_time_utc,

            "variable": norm_var,
            "unit": unit_meta,
            "name_en": name_en_meta,
            "standard_name": std_name_meta,

            "bbox": [
                float(da["lon"].values.min()),
                float(da["lat"].values.min()),
                float(da["lon"].values.max()),
                float(da["lat"].values.max()),
            ],
            "resolution": [dlon, dlat],
            "shape": [width, height],
            "indexOrder": "row-major-bottom-up",
            "valueEncoding": {"type": "float32", "scale": 1.0, "offset": 0.0, "nodata": None},
            "data": data_list,
        }
    finally:
        with contextlib.suppress(Exception):
            tmp.close(); os.unlink(tmp.name)
            ds and ds.close()


# =============================================================================
# GET /api/sources
# =============================================================================

@meta_router.get("/sources", summary="List of available data sources", include_in_schema=False)
async def get_sources():
    coll = await get_directories_collection()

    docs = await coll.find(
        {"parent": None},
        {"_id": 1, "name": 1}
    ).to_list(length=100)

    if not docs:
        raise HTTPException(status_code=404, detail="No sources found in directories.")

    MODEL_MAP = {
        "ecmwf": "ifs",
        "noaa": "gfs",
    }

    sources = [
        {
            "source": (name := doc.get("name") or doc["_id"].strip("/")),
            "model": MODEL_MAP.get(name)
        }
        for doc in docs
    ]

    return {"sources": sources}


# =============================================================================
# GET /api/variables?source=ecmwf
# =============================================================================

@meta_router.get("/variables", summary="List of available variables per source", include_in_schema=False)
async def get_variables(source: str):
    source_lower = source.strip().lower()

    MODEL_MAP = {
        "ecmwf": "ifs",
        "noaa": "gfs",
    }
    model = MODEL_MAP.get(source_lower)
    if not model:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown source '{source}'. Available: {list(MODEL_MAP.keys())}"
        )

    coll = await get_directories_collection()

    pattern = (
        rf"^{source_lower}/{model}/"
        r"\d{4}/"
        r"\d{4}-\d{2}/"
        r"\d{4}-\d{2}-\d{2}/"
        r"\d{2}Z/"
        r"(original|computed)/$"
    )

    docs = await coll.find(
        {"_id": {"$regex": pattern}},
        {"_id": 1, "children_dirs": 1}
    ).to_list(length=10000)

    if not docs:
        raise HTTPException(
            status_code=404,
            detail=f"No variable directories found for source='{source}'."
        )

    original_vars: set[str] = set()
    computed_vars: set[str] = set()

    for doc in docs:
        path: str = doc["_id"]
        children: list = doc.get("children_dirs", [])

        if "/original/" in path:
            for child in children:
                original_vars.add(child.rstrip("/"))
        elif "/computed/" in path:
            for child in children:
                computed_vars.add(child.rstrip("/"))

    variables = (
        [{"variable": v, "dataset_code": "original"} for v in sorted(original_vars)]
        + [{"variable": v, "dataset_code": "computed"} for v in sorted(computed_vars)]
    )

    return {
        "source": source_lower,
        "model": model,
        "variables": variables,
    }


# =============================================================================
# GET /api/gridfile
# =============================================================================

@router.get(
    "/gridfile",
    summary="Get presigned S3 URL for a grid file",
    description="Returns metadata and a temporary presigned URL for direct file download."
)
async def get_gridfile(
    source: str = Query(..., example="ecmwf"),
    dataset_code: str = Query(..., example="original"),
    model: str = Query(..., example="ifs"),
    variable: str = Query(..., example="swh"),
    run_time_utc: str = Query(..., example="2025-07-16T00:00:00Z"),
    step_hours: int = Query(..., ge=0, le=360, example=24),
    expires_in: int = Query(default=3600, ge=60, le=86400, description="Presigned URL validity duration (seconds)"),
):
    type_ = "forecast"

    norm_var = _norm_var(variable, source)

    if norm_var in ("wind_speed_10m", "wind_dir_10m"):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Computed variables (wind_speed_10m, wind_dir_10m) have no single source file.",
                "hint": "Use /api/griddata to get computed wind values."
            }
        )

    run_dt = _parse_utc(run_time_utc)
    valid_dt = run_dt + timedelta(hours=int(step_hours))
    valid_time_utc = _to_z(valid_dt)

    coll = await get_assets_collection()

    # all-steps는 step_hours 없이 조회
    if dataset_code == DATASET_CODE_ALL_STEPS:
        doc = await coll.find_one({
            "source": source,
            "dataset_code": dataset_code,
            "model": model,
            "variable": norm_var,
            "run_time_utc": _to_z(run_dt),
        })
    else:
        doc = await _find_by_natural_key(
            coll,
            source=source,
            dataset_code=dataset_code,
            model=model,
            type_=type_,
            variable=norm_var,
            run_time_utc=_to_z(run_dt),
            step_hours=step_hours,
        )

    if not doc:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "No matching dataset found for this run+step.",
                "source": source,
                "dataset_code": dataset_code,
                "model": model,
                "type": type_,
                "variable": norm_var,
                "run_time_utc": _to_z(run_dt),
                "step_hours": int(step_hours),
            }
        )

    if "s3" not in doc or "key" not in doc.get("s3", {}):
        raise HTTPException(
            status_code=409,
            detail={"error": "This metadata record has no s3.key", "doc_keys": list(doc.keys())}
        )

    s3_key = doc["s3"]["key"]
    try:
        presigned_url = await asyncio.to_thread(
            s3.generate_presigned_url,
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": s3_key},
            ExpiresIn=expires_in,
        )
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail={"error": "Failed to generate presigned URL.", "reason": str(e)}
        )

    return {
        "run_time_utc": _to_z(run_dt),
        "step_hours": int(step_hours),
        "valid_time_utc": valid_time_utc,

        "source": source,
        "dataset_code": dataset_code,
        "model": model,
        "variable": norm_var,
        "unit": doc.get("unit"),
        "name_en": doc.get("name_en"),
        "standard_name": doc.get("standard_name"),

        "file": {
            "url": presigned_url,
            "expires_in_seconds": expires_in,
            "format": doc.get("format"),
            "size_bytes": doc.get("size_bytes"),
            "s3_key": s3_key,
        }
    }


# =============================================================================
# Helpers - 변수명 처리
# =============================================================================

def resolve_var(ds: xr.Dataset, var: str, source: str = "ecmwf") -> str:
    aliases = get_aliases_by_source(source)
    candidates = aliases.get(var, [var])
    
    for name in candidates:
        if name in ds.data_vars:
            return name
    
    if var in ds.data_vars:
        return var
    
    raise KeyError(
        f"Variable '{var}' not found in {source} dataset. "
        f"Tried: {candidates}. Available: {list(ds.data_vars)}"
    )


def _norm_var(v: str, source: str = "ecmwf") -> str:
    low = v.strip().lower()
    source_lower = source.lower()

    if source_lower == "noaa":
        for key in ALIASES_NOAA.keys():
            if low == key.lower():
                return key
        for key, aliases in ALIASES_NOAA.items():
            if low in [a.lower() for a in aliases]:
                return key

    if low in ("wind_speed_10m", "ws", "ws10", "spd", "wind"):
        return "wind_speed_10m"
    if low in ("wind_dir_10m", "wdir", "wdir10", "dir", "wd"):
        return "wind_dir_10m"

    if source_lower == "ecmwf":
        if low in ("u10", "10u"):
            return "10u"
        if low in ("v10", "10v"):
            return "10v"
        if low in ("swh", "significant_wave_height", "hs"):
            return "swh"

    return v.strip()


# =============================================================================
# Helpers - 좌표 정규화
# =============================================================================

def _normalize_grib_coordinates(ds: xr.Dataset, source: str) -> xr.Dataset:
    ds = _normalize_lonlat(ds)
    
    if "lon" not in ds.coords:
        return ds
    
    lon_vals = ds["lon"].values
    
    if lon_vals.max() > 180:
        ds = ds.assign_coords(lon=(ds["lon"] + 180) % 360 - 180)
        ds = ds.sortby("lon")
    
    if source.lower() == "noaa":
        lon_corrected = np.round(ds["lon"].values / 0.25) * 0.25
        lat_corrected = np.round(ds["lat"].values / 0.25) * 0.25
        ds = ds.assign_coords({"lon": lon_corrected, "lat": lat_corrected})
    
    return ds


def _normalize_lonlat(ds: xr.Dataset) -> xr.Dataset:
    rename_map = {}
    if "longitude" in ds.coords and "lon" not in ds.coords:
        rename_map["longitude"] = "lon"
    if "latitude" in ds.coords and "lat" not in ds.coords:
        rename_map["latitude"] = "lat"
    return ds.rename(rename_map) if rename_map else ds


# =============================================================================
# Helpers - all-steps step 추출
# =============================================================================

def _extract_step_from_all_steps(ds: xr.Dataset, step_hours: int) -> xr.Dataset:
    """
    all-steps grib2에서 특정 step만 추출.
    cfgrib은 step을 timedelta64, valid_time을 datetime64로 읽음.
    """
    # 방법 1: step 좌표가 timedelta64인 경우 (cfgrib 기본)
    if "step" in ds.coords and ds["step"].ndim > 0 and ds.sizes.get("step", 1) > 1:
        target = np.timedelta64(step_hours, "h")
        steps = ds["step"].values
        if target in steps:
            return ds.sel(step=target)
        idx = int(np.argmin(np.abs(steps - target)))
        return ds.isel(step=idx)

    # 방법 2: time 차원이 여러 개인 경우
    if "time" in ds.dims and ds.sizes.get("time", 1) > 1:
        time_vals = ds["time"].values
        base = time_vals[0]
        target_dt = base + np.timedelta64(step_hours, "h")
        idx = int(np.argmin(np.abs(time_vals - target_dt)))
        return ds.isel(time=idx)

    # 방법 3: 이미 단일 스텝
    return ds.squeeze(drop=True)


# =============================================================================
# Helpers - 마스킹 처리
# =============================================================================

def _handle_grib_masking(da: xr.DataArray, source: str, fill_strategy: str = "nan") -> xr.DataArray:
    if not hasattr(da.values, 'mask'):
        return da
    
    if fill_strategy == "nan":
        filled_values = np.where(da.values.mask, np.nan, da.values.data)
    elif fill_strategy == "zero":
        filled_values = np.where(da.values.mask, 0, da.values.data)
    else:
        filled_values = da.values.filled(np.nan)
    
    return xr.DataArray(filled_values, coords=da.coords, dims=da.dims, attrs=da.attrs)


# =============================================================================
# Helpers - 데이터 선택
# =============================================================================

def _select_da(ds: xr.Dataset, var: str, bbox: Optional[List[float]], source: str = "ecmwf"):
    var2 = resolve_var(ds, var, source)
    da = ds[var2]
    
    da = _handle_grib_masking(da, source, fill_strategy="nan")
    
    if "time" in da.dims and da.sizes.get("time", 1) == 1:
        da = da.isel(time=0)
    
    lon_vals = ds["lon"].values
    lat_vals = ds["lat"].values
    lon_inc = bool(lon_vals[1] > lon_vals[0]) if lon_vals.size > 1 else True
    lat_inc = bool(lat_vals[1] > lat_vals[0]) if lat_vals.size > 1 else True
    
    if bbox and len(bbox) == 4:
        min_lon, min_lat, max_lon, max_lat = map(float, bbox)
        bbox_width = max_lon - min_lon
        bbox_height = max_lat - min_lat
        
        if bbox_width < 0.01 and bbox_height < 0.01:
            center_lon = (min_lon + max_lon) / 2
            center_lat = (min_lat + max_lat) / 2
            da = da.sel(lon=center_lon, lat=center_lat, method='nearest')
            if 'lon' not in da.dims and 'lat' not in da.dims:
                selected_lon = float(da['lon'].values)
                selected_lat = float(da['lat'].values)
                da = da.expand_dims({'lon': [selected_lon], 'lat': [selected_lat]})
        else:
            lon_slice = slice(min_lon, max_lon) if lon_inc else slice(max_lon, min_lon)
            lat_slice = slice(min_lat, max_lat) if lat_inc else slice(max_lat, min_lat)
            da = da.sel(lon=lon_slice, lat=lat_slice)
    
    return da, lat_inc, lon_inc


def _ensure_lat_lon_names(da: xr.DataArray) -> xr.DataArray:
    rename_map = {}
    if "latitude" in da.dims:
        rename_map["latitude"] = "lat"
    if "longitude" in da.dims:
        rename_map["longitude"] = "lon"
    return da.rename(rename_map) if rename_map else da


def _prepare_array_for_response(da: xr.DataArray, lat_inc: bool):
    da2 = da.transpose("lat", "lon")
    lat_vals = da2["lat"].values
    lon_vals = da2["lon"].values
    arr2 = da2.values

    if not lat_inc:
        arr2 = arr2[::-1, :]
        lat_vals = lat_vals[::-1]

    dlon = float(abs(np.mean(np.diff(lon_vals)))) if lon_vals.size > 1 else 0.25
    dlat = float(abs(np.mean(np.diff(lat_vals)))) if lat_vals.size > 1 else 0.25

    h, w = arr2.shape
    return arr2, dlon, dlat, w, h


# =============================================================================
# Helpers - 파일 및 시간
# =============================================================================

def _parse_utc(dt_str: str) -> datetime:
    s = dt_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def _to_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _find_by_natural_key(
    coll,
    *,
    source: str,
    dataset_code: str,
    model: str,
    type_: str,
    variable: str,
    run_time_utc: str,
    step_hours: int,
):
    return await coll.find_one({
        "source": source,
        "dataset_code": dataset_code,
        "model": model,
        "type": type_,
        "variable": variable,
        "run_time_utc": run_time_utc,
        "step_hours": int(step_hours),
    })


def _tmp_suffix_from_doc(doc: dict) -> str:
    fmt = (doc.get("format") or "").lower()
    ctype = (doc.get("content_type") or "").lower()
    name = (doc.get("name") or "").lower()

    if "grib" in fmt or "grib" in ctype or name.endswith((".grib2", ".grib")):
        return ".grib2"
    if "netcdf" in fmt or name.endswith((".nc", ".netcdf")):
        return ".nc"
    return ".bin"


def _open_dataset_safely(path: str) -> xr.Dataset:
    last_err = None
    for engine in ("cfgrib", "h5netcdf"):
        try:
            return xr.open_dataset(path, engine=engine)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Failed to open dataset: {last_err}")


# =============================================================================
# Helpers - bbox 검증 및 셀 추정
# =============================================================================

def _validate_bbox_limits_raw(bbox: Optional[List[float]]):
    if not bbox:
        return
    if len(bbox) != 4:
        raise HTTPException(status_code=422, detail="bbox must be [minLon, minLat, maxLon, maxLat]")
    min_lon, min_lat, max_lon, max_lat = map(float, bbox)
    lim = BBOX_LIMITS
    if (
        min_lon < lim["min_lon"] or max_lon > lim["max_lon"] or
        min_lat < lim["min_lat"] or max_lat > lim["max_lat"]
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Requested bbox exceeds allowed range.",
                "allowed_bbox": [lim["min_lon"], lim["min_lat"], lim["max_lon"], lim["max_lat"]],
                "hint": "Reduce bbox (minLon,minLat,maxLon,maxLat)."
            }
        )


def _estimate_cells_from_doc_or_assume(bbox: List[float], doc: dict) -> int:
    res = (doc or {}).get("resolution") or {}
    dlon = float(res.get("lon_deg") or ASSUMED_DLON)
    dlat = float(res.get("lat_deg") or ASSUMED_DLAT)
    return _estimate_cells_assuming_resolution(bbox, dlon=dlon, dlat=dlat)


def _estimate_cells_assuming_resolution(bbox: List[float], dlon=ASSUMED_DLON, dlat=ASSUMED_DLAT) -> int:
    min_lon, min_lat, max_lon, max_lat = map(float, bbox)

    def to0360(x: float) -> float:
        return x + 360.0 if x < 0 else x

    a, b = to0360(min_lon), to0360(max_lon)
    lon_span = (360.0 - a) + b if a > b else (b - a)
    lat_span = abs(max_lat - min_lat)

    width = int(np.floor(lon_span / dlon)) + 1
    height = int(np.floor(lat_span / dlat)) + 1
    return max(0, width) * max(0, height)