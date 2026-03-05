# api.py
from __future__ import annotations

from fastapi import APIRouter, Query, HTTPException
from typing import Optional, List
from datetime import datetime, timezone, timedelta

from app.models import GridDataResponse
from app.db import get_assets_collection

import numpy as np
import xarray as xr
import tempfile, os, contextlib, boto3


# ---- env / S3 ----
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-2")
S3_BUCKET  = os.getenv("S3_BUCKET", "optimal-loads")
s3 = boto3.client("s3", region_name=AWS_REGION)


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
router = APIRouter(prefix="/api", tags=["grid"])


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

    # ---- spatial ----
    bbox: Optional[List[float]] = Query(
        default=None,
        description="[minLon, minLat, maxLon, maxLat] (optional)",
        example=[128.0, 34.0, 130.0, 36.0]
    ),
) -> GridDataResponse:
    
    type_ = "forecast"
    
    # ---- bbox 선검증 ----
    effective_bbox = bbox or [
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
        
        # ✅ 소스별로 U/V 변수명이 다름
        if source.lower() == "noaa":
            u_var = "UGRD"
            v_var = "VGRD"
        elif source.lower() == "ecmwf":
            u_var = "10u"
            v_var = "10v"
        else:
            u_var = "10u"
            v_var = "10v"
        
        doc_u = await _find_by_natural_key(
            coll,
            source=source,
            dataset_code="original",
            model=model,
            type_=type_,
            variable=u_var,
            run_time_utc=_to_z(run_dt),
            step_hours=step_hours
        )
        doc_v = await _find_by_natural_key(
            coll,
            source=source,
            dataset_code="original",
            model=model,
            type_=type_,
            variable=v_var,
            run_time_utc=_to_z(run_dt),
            step_hours=step_hours
        )
        
        if not (doc_u and doc_v):
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "Required U/V components not found for computed wind at this run+step.",
                    "needed": [u_var, v_var],
                    "source": source,
                    "run_time_utc": _to_z(run_dt),
                    "step_hours": int(step_hours)
                }
            )
        
        # bbox cell budget 체크
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

        # s3 key 검증
        if "s3" not in doc_u or "key" not in doc_u.get("s3", {}):
            raise HTTPException(status_code=409, detail={"error": f"{u_var} metadata has no s3.key"})
        if "s3" not in doc_v or "key" not in doc_v.get("s3", {}):
            raise HTTPException(status_code=409, detail={"error": f"{v_var} metadata has no s3.key"})

        tmpu = tempfile.NamedTemporaryFile(delete=False, suffix=_tmp_suffix_from_doc(doc_u))
        tmpv = tempfile.NamedTemporaryFile(delete=False, suffix=_tmp_suffix_from_doc(doc_v))
        ds_u = ds_v = None
        try:
            # S3에서 다운로드
            s3.download_file(S3_BUCKET, doc_u["s3"]["key"], tmpu.name)
            s3.download_file(S3_BUCKET, doc_v["s3"]["key"], tmpv.name)
            
            # Dataset 열기
            ds_u = _open_dataset_safely(tmpu.name)
            ds_v = _open_dataset_safely(tmpv.name)
            
            # 소스별 정규화 (좌표 체계 통일)
            ds_u = _normalize_grib_coordinates(ds_u, source)
            ds_v = _normalize_grib_coordinates(ds_v, source)
            
            # 소스별 변수명으로 선택
            da_u, lat_inc_u, _ = _select_da(ds_u, u_var, bbox, source=source)
            da_v, lat_inc_v, _ = _select_da(ds_v, v_var, bbox, source=source)
            
            da_u = _ensure_lat_lon_names(da_u)
            da_v = _ensure_lat_lon_names(da_v)
            
            # 좌표 정합 (격자 불일치 해결)
            da_u, da_v = xr.align(da_u, da_v, join="inner")
            
            # 바람 속도/방향 계산
            speed = np.hypot(da_u.values, da_v.values)
            direc = (90.0 - np.degrees(np.arctan2(da_v.values, da_u.values))) % 360.0

            target = speed if norm_var == "wind_speed_10m" else direc
            da_like = da_u  # 좌표/차원 템플릿

            arr2, dlon, dlat, width, height = _prepare_array_for_response(
                xr.DataArray(target, coords=da_like.coords, dims=da_like.dims),
                lat_inc_u
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

                "bbox": bbox if bbox else [
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
    
    doc = await _find_by_natural_key(
        coll,
        source=source,
        dataset_code=dataset_code,
        model=model,
        type_=type_,
        variable=norm_var,
        run_time_utc=_to_z(run_dt),
        step_hours=step_hours
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
        
        # 소스별 좌표 정규화
        ds = _normalize_grib_coordinates(ds, source)

        if bbox is not None and len(bbox) != 4:
            raise HTTPException(status_code=422, detail="bbox must have 4 numbers: [minLon, minLat, maxLon, maxLat]")

        # 소스 정보 전달
        da, lat_inc, _ = _select_da(ds, norm_var, bbox, source=source)
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

            "bbox": bbox if bbox else [
                float(da["lon"].values.min()),
                float(da["lat"].values.min()),
                float(da["lon"].values.max()),
                float(da["lat"].values.max())
            ],
            "resolution": [dlon, dlat],
            "shape": [width, height],
            "indexOrder": "row-major-bottom-up",
            "valueEncoding": {"type": "float32", "scale": 1.0, "offset": 0.0, "nodata": None},
            "data": data_list
        }
    finally:
        with contextlib.suppress(Exception):
            tmp.close(); os.unlink(tmp.name)
            ds and ds.close()


# =============================================================================
# Helpers - 변수명 처리
# =============================================================================

def resolve_var(ds: xr.Dataset, var: str, source: str = "ecmwf") -> str:
    """
    소스별 변수명 별칭을 실제 데이터셋에 있는 변수명으로 해석
    """
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
    """
    사용자 입력 변수명을 소스별 표준 변수명으로 정규화
    """
    low = v.strip().lower()
    source_lower = source.lower()
    
    # Computed 바람 (소스 무관)
    if low in ("wind_speed_10m", "ws", "ws10", "spd", "wind"):
        return "wind_speed_10m"
    if low in ("wind_dir_10m", "wdir", "wdir10", "dir", "wd"):
        return "wind_dir_10m"
    
    # NOAA 변수명 정규화
    if source_lower == "noaa":
        for key in ALIASES_NOAA.keys():
            if low == key.lower():
                return key
        
        # 별칭으로 입력한 경우
        for key, aliases in ALIASES_NOAA.items():
            if low in [a.lower() for a in aliases]:
                return key
    
    # ECMWF 변수명 정규화
    elif source_lower == "ecmwf":
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
    """
    소스별로 다른 GRIB 좌표 체계를 ECMWF 스타일(-180~180)로 통일
    """
    ds = _normalize_lonlat(ds)  # longitude/latitude → lon/lat
    
    if "lon" not in ds.coords:
        return ds
    
    lon_vals = ds["lon"].values
    
    # 1. 경도 체계 확인 및 변환 (0~360 → -180~180)
    if lon_vals.max() > 180:
        ds = ds.assign_coords(lon=(ds["lon"] + 180) % 360 - 180)
        ds = ds.sortby("lon")
    
    # 2. NOAA: 부동소수점 오차 보정
    if source.lower() == "noaa":
        lon_corrected = np.round(ds["lon"].values / 0.25) * 0.25
        lat_corrected = np.round(ds["lat"].values / 0.25) * 0.25
        
        ds = ds.assign_coords({
            "lon": lon_corrected,
            "lat": lat_corrected
        })
    
    return ds


def _normalize_lonlat(ds: xr.Dataset) -> xr.Dataset:
    """longitude/latitude → lon/lat 이름 변경"""
    rename_map = {}
    if "longitude" in ds.coords and "lon" not in ds.coords:
        rename_map["longitude"] = "lon"
    if "latitude" in ds.coords and "lat" not in ds.coords:
        rename_map["latitude"] = "lat"
    return ds.rename(rename_map) if rename_map else ds


# =============================================================================
# Helpers - 마스킹 처리
# =============================================================================

def _handle_grib_masking(da: xr.DataArray, source: str, fill_strategy: str = "nan") -> xr.DataArray:
    """
    소스별 마스킹 처리 (numpy.ma.MaskedArray → 일반 배열)
    """
    if not hasattr(da.values, 'mask'):
        return da
    
    if fill_strategy == "nan":
        filled_values = np.where(da.values.mask, np.nan, da.values.data)
    elif fill_strategy == "zero":
        filled_values = np.where(da.values.mask, 0, da.values.data)
    else:
        filled_values = da.values.filled(np.nan)
    
    return xr.DataArray(
        filled_values,
        coords=da.coords,
        dims=da.dims,
        attrs=da.attrs
    )


# =============================================================================
# Helpers - 데이터 선택
# =============================================================================

def _select_da(ds: xr.Dataset, var: str, bbox: Optional[List[float]], source: str = "ecmwf"):
    """
    소스별 처리를 추가한 DataArray 선택
    """
    # 변수명 해석 (소스별 ALIASES 적용)
    var2 = resolve_var(ds, var, source)
    da = ds[var2]
    
    # 마스킹 처리
    da = _handle_grib_masking(da, source, fill_strategy="nan")
    
    if "time" in da.dims and da.sizes.get("time", 1) == 1:
        da = da.isel(time=0)
    
    if bbox and len(bbox) == 4:
        min_lon, min_lat, max_lon, max_lat = map(float, bbox)
        lon_vals = ds["lon"].values
        lat_vals = ds["lat"].values
        
        lon_inc = bool(lon_vals[1] > lon_vals[0]) if lon_vals.size > 1 else True
        lat_inc = bool(lat_vals[1] > lat_vals[0]) if lat_vals.size > 1 else True
        
        lon_slice = slice(min_lon, max_lon) if lon_inc else slice(max_lon, min_lon)
        lat_slice = slice(min_lat, max_lat) if lat_inc else slice(max_lat, min_lat)
        
        da = da.sel(lon=lon_slice, lat=lat_slice)
    else:
        lon_vals = ds["lon"].values
        lat_vals = ds["lat"].values
        lon_inc = bool(lon_vals[1] > lon_vals[0]) if lon_vals.size > 1 else True
        lat_inc = bool(lat_vals[1] > lat_vals[0]) if lat_vals.size > 1 else True
    
    return da, lat_inc, lon_inc


def _ensure_lat_lon_names(da: xr.DataArray) -> xr.DataArray:
    """latitude/longitude → lat/lon 이름 변경"""
    rename_map = {}
    if "latitude" in da.dims:
        rename_map["latitude"] = "lat"
    if "longitude" in da.dims:
        rename_map["longitude"] = "lon"
    return da.rename(rename_map) if rename_map else da


def _prepare_array_for_response(da: xr.DataArray, lat_inc: bool):
    """
    응답용 배열 준비 (row-major-bottom-up 형식)
    """
    da2 = da.transpose("lat", "lon")
    lat_vals = da2["lat"].values
    lon_vals = da2["lon"].values
    arr2 = da2.values

    # 위도가 감소하는 경우 뒤집기 (bottom-up)
    if not lat_inc:
        arr2 = arr2[::-1, :]
        lat_vals = lat_vals[::-1]

    dlon = float(abs(np.mean(np.diff(lon_vals)))) if lon_vals.size > 1 else np.nan
    dlat = float(abs(np.mean(np.diff(lat_vals)))) if lat_vals.size > 1 else np.nan
    h, w = arr2.shape
    return arr2, dlon, dlat, w, h


# =============================================================================
# Helpers - 파일 및 시간
# =============================================================================

def _parse_utc(dt_str: str) -> datetime:
    """ISO 8601 문자열을 UTC datetime으로 파싱"""
    s = dt_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def _to_z(dt: datetime) -> str:
    """datetime을 ISO 8601 Z 형식 문자열로 변환"""
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
    """MongoDB에서 자연키로 문서 찾기"""
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
    """문서 메타데이터에서 파일 확장자 추정"""
    fmt = (doc.get("format") or "").lower()
    ctype = (doc.get("content_type") or "").lower()
    name = (doc.get("name") or "").lower()

    if "grib" in fmt or "grib" in ctype or name.endswith((".grib2", ".grib")):
        return ".grib2"
    if "netcdf" in fmt or name.endswith((".nc", ".netcdf")):
        return ".nc"
    return ".bin"


def _open_dataset_safely(path: str) -> xr.Dataset:
    """cfgrib 또는 h5netcdf로 Dataset 열기"""
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
    """bbox가 허용 범위 내에 있는지 검증"""
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
                "hint": "Reduce bbox (minLon,minLat,maxLon,maxLat). Example: bbox=115&bbox=25&bbox=142&bbox=43"
            }
        )


def _estimate_cells_from_doc_or_assume(bbox: List[float], doc: dict) -> int:
    """문서 메타데이터 또는 가정된 해상도로 셀 수 추정"""
    res = (doc or {}).get("resolution") or {}
    dlon = float(res.get("lon_deg") or ASSUMED_DLON)
    dlat = float(res.get("lat_deg") or ASSUMED_DLAT)
    return _estimate_cells_assuming_resolution(bbox, dlon=dlon, dlat=dlat)


def _estimate_cells_assuming_resolution(bbox: List[float], dlon=ASSUMED_DLON, dlat=ASSUMED_DLAT) -> int:
    """주어진 해상도로 bbox 내 셀 수 추정"""
    min_lon, min_lat, max_lon, max_lat = map(float, bbox)

    def to0360(x: float) -> float:
        return x + 360.0 if x < 0 else x

    a, b = to0360(min_lon), to0360(max_lon)
    lon_span = (360.0 - a) + b if a > b else (b - a)
    lat_span = abs(max_lat - min_lat)

    width = int(np.floor(lon_span / dlon)) + 1
    height = int(np.floor(lat_span / dlat)) + 1
    return max(0, width) * max(0, height)