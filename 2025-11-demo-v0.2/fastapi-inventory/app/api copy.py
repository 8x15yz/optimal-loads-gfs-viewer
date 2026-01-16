from __future__ import annotations
from fastapi import APIRouter, Query, HTTPException
from typing import Optional, List, Union
from app.models import GridDataResponse
from app.db import get_collection
import numpy as np
import xarray as xr
import tempfile, os, contextlib, boto3


# ---- env / S3 ----
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-2")
S3_BUCKET  = os.getenv("S3_BUCKET", "optimal-loads")
s3 = boto3.client("s3", region_name=AWS_REGION)


router = APIRouter(prefix="/api", tags=["grid"])

@router.get(
    "/griddata",
    response_model=GridDataResponse,
    summary="Get gridded variable data",
    description="Reads a NetCDF from S3 and returns encoded grid values."
)
async def get_griddata(
    variable: str = Query(..., example="VHM0"),
    forecast_datetime: str = Query(..., example="2024-03-31T00:00:00Z"),
    source: str = Query(..., example="cmems"),
    bbox: Optional[List[float]] = Query(
        default=None,
        description="[minLon, minLat, maxLon, maxLat] (optional)",
        example=[128.0, 34.0, 130.0, 36.0]
    ),
    depth: Optional[Union[float, str]] = Query(
        default=None,
        description="(optional) depth in meters or 'surface', e.g., 0.5, 10, 'surface'"
    )
) -> GridDataResponse:
    # ---- 변수 정규화 (별칭 허용) ----
    norm_var = _norm_var(variable)

    # ✅ 공통 선검증: bbox 미지정이면 전역 허용범위로 간주하여 검사
    effective_bbox = bbox or [
        BBOX_LIMITS["min_lon"], BBOX_LIMITS["min_lat"],
        BBOX_LIMITS["max_lon"], BBOX_LIMITS["max_lat"],
    ]

    # 형식/전역범위 검사
    _validate_bbox_limits_raw(effective_bbox)

    # 0.083° 가정으로 셀 수 추정 → 한도 초과 시 거절
    est = _estimate_cells_assuming_resolution(effective_bbox)
    if est > MAX_CELLS:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "Requested bbox is too large for this resolution.",
                "requested_bbox": effective_bbox,
                "estimated_cells": est,
                "limit": MAX_CELLS,
                "hint": "Reduce bbox size. Example: bbox=128&bbox=34&bbox=130&bbox=36"
            }
        )
    # ========== [A] computed (wind_speed / wind_dir) ==========
    if norm_var in ("wind_speed", "wind_dir"):
        coll = await get_collection()

        # 1) U,V 메타 가져오기
        doc_u = await coll.find_one({"source": source, "variable": "eastward_wind",  "valid_time_utc": forecast_datetime})
        doc_v = await coll.find_one({"source": source, "variable": "northward_wind", "valid_time_utc": forecast_datetime})
        if not (doc_u and doc_v):
            raise HTTPException(status_code=404, detail="Required U/V components not found for computed wind.")

        tmpu = tempfile.NamedTemporaryFile(delete=False, suffix=".nc")
        tmpv = tempfile.NamedTemporaryFile(delete=False, suffix=".nc")
        ds_u = ds_v = None
        try:
            s3.download_file(S3_BUCKET, doc_u["s3"]["key"], tmpu.name)
            s3.download_file(S3_BUCKET, doc_v["s3"]["key"], tmpv.name)
            ds_u = _open_dataset_safely(tmpu.name)
            ds_v = _open_dataset_safely(tmpv.name)

            ds_u = _normalize_lonlat(ds_u)
            ds_v = _normalize_lonlat(ds_v)

            da_u, lat_inc_u, _ = _select_da(ds_u, "eastward_wind",  bbox)
            da_v, lat_inc_v, _ = _select_da(ds_v, "northward_wind", bbox)
            da_u = _ensure_lat_lon_names(da_u)
            da_v = _ensure_lat_lon_names(da_v)

            # depth
            da_u, _ = _select_depth_if_present(da_u, depth)
            da_v, _ = _select_depth_if_present(da_v, depth)

            # 좌표 정합 (공통 교집합으로)
            da_u, da_v = xr.align(da_u, da_v, join="inner")

            # 계산 (meteorological FROM-direction)
            speed = np.hypot(da_u.values, da_v.values)
            direc = (90.0 - np.degrees(np.arctan2(da_v.values, da_u.values))) % 360.0

            target = speed if norm_var == "wind_speed" else direc
            da_like = da_u  # 좌표/차원 템플릿

            # 응답 배열 준비
            arr2, dlon, dlat, width, height = _prepare_array_for_response(
                xr.DataArray(target, coords=da_like.coords, dims=da_like.dims),
                lat_inc_u
            )

            # 메타
            if norm_var == "wind_speed":
                unit_meta, name_en_meta, std_name_meta = "m s-1", "10 m wind speed", "wind_speed"
            else:
                unit_meta, name_en_meta, std_name_meta = "degree", "10 m wind direction (from)", "wind_from_direction"

            # float32로 변환하고 NaN을 None으로 변환 (JSON 호환)
            arr_flat = arr2.astype(np.float32).flatten()
            data_list = [None if np.isnan(x) else float(x) for x in arr_flat]
            
            return {
                "timestamp": forecast_datetime,
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

    # ========== [B] original (기존 로직) ==========
    coll = await get_collection()
    print("Looking for document:", {"variable": norm_var, "valid_time_utc": forecast_datetime, "source": source})
    doc = await coll.find_one({
        "variable": norm_var,  # <- 정규화된 이름 사용
        "valid_time_utc": forecast_datetime,
        "source": source
    })
    if not doc:
        raise HTTPException(status_code=404, detail="No matching dataset found.")

    s3_key = doc["s3"]["key"]
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".nc")
    ds = None
    try:
        s3.download_file(S3_BUCKET, s3_key, tmp.name)
        ds = _open_dataset_safely(tmp.name)

        if bbox is not None and len(bbox) != 4:
            raise HTTPException(status_code=422, detail="bbox must have 4 numbers: [minLon, minLat, maxLon, maxLat]")

        da, lat_inc, _ = _select_da(ds, norm_var, bbox)
        da = _ensure_lat_lon_names(da)
        da, chosen_depth = _select_depth_if_present(da, depth)

        arr2, dlon, dlat, width, height = _prepare_array_for_response(da, lat_inc)

        _fallback_meta = {
            "VHM0": {"unit": "m",      "name_en": "Significant wave height",
                     "standard_name": "sea_surface_wave_significant_height"},
            "VMDR": {"unit": "degree", "name_en": "Mean wave direction",
                     "standard_name": "sea_surface_wave_from_direction"},
            "VTPK": {"unit": "s",      "name_en": "Peak wave period",
                     "standard_name": "sea_surface_wave_period_at_variance_spectral_density_maximum"},
            "uo":   {"unit": "m s-1",  "name_en": "Eastward surface current (u)",
                     "standard_name": "eastward_sea_water_velocity"},
            "vo":   {"unit": "m s-1",  "name_en": "Northward surface current (v)",
                     "standard_name": "northward_sea_water_velocity"},
        }
        vi = (doc.get("variable_info") or {})
        unit_meta = doc.get("unit") or vi.get("units") or _fallback_meta.get(norm_var, {}).get("unit")
        name_en_meta = doc.get("name_en") or vi.get("display_name_en") or _fallback_meta.get(norm_var, {}).get("name_en")
        std_name_meta = doc.get("standard_name") or vi.get("standard_name") or _fallback_meta.get(norm_var, {}).get("standard_name")

        # float32로 변환하고 NaN을 None으로 변환 (JSON 호환)
        arr_flat = arr2.astype(np.float32).flatten()
        data_list = [None if np.isnan(x) else float(x) for x in arr_flat]
        
        return {
            "timestamp": forecast_datetime,
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


# ---- helpers (API 전용) ----
def _open_dataset_safely(path: str) -> xr.Dataset:
    last_err = None
    for engine in ("h5netcdf", "netcdf4"):
        try:
            return xr.open_dataset(path, engine=engine)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Failed to open dataset: {last_err}")

def _normalize_lonlat(ds: xr.Dataset) -> xr.Dataset:
    rename_map = {}
    if "longitude" in ds.coords and "lon" not in ds.coords:
        rename_map["longitude"] = "lon"
    if "latitude" in ds.coords and "lat" not in ds.coords:
        rename_map["latitude"] = "lat"
    return ds.rename(rename_map) if rename_map else ds

def _ensure_lon_range(lon_vals: np.ndarray, x: float) -> float:
    if lon_vals.max() > 180 and x < 0:
        return x + 360.0
    return x

def _select_da(ds: xr.Dataset, var: str, bbox: Optional[List[float]]):
    ds = _normalize_lonlat(ds)
    if var not in ds:
        raise KeyError(f"Variable '{var}' not found in dataset.")
    da = ds[var]
    if "time" in da.dims and da.sizes.get("time", 1) == 1:
        da = da.isel(time=0)

    if bbox and len(bbox) == 4:
        min_lon, min_lat, max_lon, max_lat = map(float, bbox)
        lon_vals = ds["lon"].values
        min_lon = _ensure_lon_range(lon_vals, min_lon)
        max_lon = _ensure_lon_range(lon_vals, max_lon)
        lon_inc = bool(lon_vals[1] > lon_vals[0])
        lat_vals = ds["lat"].values
        lat_inc = bool(lat_vals[1] > lat_vals[0])
        lon_slice = slice(min_lon, max_lon) if lon_inc else slice(max_lon, min_lon)
        lat_slice = slice(min_lat, max_lat) if lat_inc else slice(max_lat, min_lat)
        da = da.sel(lon=lon_slice, lat=lat_slice)
    else:
        lon_vals = ds["lon"].values
        lat_vals = ds["lat"].values
        lon_inc = bool(lon_vals[1] > lon_vals[0])
        lat_inc = bool(lat_vals[1] > lat_vals[0])

    return da, lat_inc, lon_inc

def _ensure_lat_lon_names(da: xr.DataArray) -> xr.DataArray:
    rename_map = {}
    if "latitude" in da.dims: rename_map["latitude"] = "lat"
    if "longitude" in da.dims: rename_map["longitude"] = "lon"
    return da.rename(rename_map) if rename_map else da

def _select_depth_if_present(da: xr.DataArray, depth_param):
    if "depth" not in da.dims:
        return da, None
    zvals = da["depth"].values
    if depth_param is None or (isinstance(depth_param, str) and depth_param.lower() == "surface"):
        return da.isel(depth=0), float(zvals[0])
    try:
        target = float(depth_param)
        da2 = da.sel(depth=target, method="nearest")
        if "depth" in da2.dims and da2.sizes["depth"] == 1:
            da2 = da2.isel(depth=0)
        return da2, float(da2.coords["depth"].values)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid depth parameter. Use a number (meters) or 'surface'.")

def _prepare_array_for_response(da: xr.DataArray, lat_inc: bool):
    da2 = da.transpose("lat", "lon")
    lat_vals = da2["lat"].values
    lon_vals = da2["lon"].values
    arr2 = da2.values
    if not lat_inc:
        arr2 = arr2[::-1, :]
        lat_vals = lat_vals[::-1]
    dlon = float(abs(np.mean(np.diff(lon_vals)))) if lon_vals.size > 1 else np.nan
    dlat = float(abs(np.mean(np.diff(lat_vals)))) if lat_vals.size > 1 else np.nan
    h, w = arr2.shape
    return arr2, dlon, dlat, w, h

# --- 추가: 변수 정규화 & 계산 유틸 ---
def _norm_var(v: str) -> str:
    raw = v.strip()
    low = raw.lower()

    # computed alias만 매핑
    if low in ("wind", "wind_speed", "spd", "ws"):
        return "wind_speed"
    if low in ("wdir", "wind_dir", "dir", "wd"):
        return "wind_dir"

    # CMEMS/WAVE 변수들의 정규 표기 유지
    if low in ("vhm0", "vmdr", "vtpk"):
        return low.upper()

    # ocean current 등은 원형 유지 (데이터에 맞춤)
    return raw




# ---- helpers (API 전용) ----
def _open_dataset_safely(path: str) -> xr.Dataset:
    last_err = None
    for engine in ("h5netcdf", "netcdf4"):
        try:
            return xr.open_dataset(path, engine=engine)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Failed to open dataset: {last_err}")

def _normalize_lonlat(ds: xr.Dataset) -> xr.Dataset:
    rename_map = {}
    if "longitude" in ds.coords and "lon" not in ds.coords:
        rename_map["longitude"] = "lon"
    if "latitude" in ds.coords and "lat" not in ds.coords:
        rename_map["latitude"] = "lat"
    return ds.rename(rename_map) if rename_map else ds

def _ensure_lon_range(lon_vals: np.ndarray, x: float) -> float:
    if lon_vals.max() > 180 and x < 0:
        return x + 360.0
    return x

def _select_da(ds: xr.Dataset, var: str, bbox: Optional[List[float]]):
    ds = _normalize_lonlat(ds)
    if var not in ds:
        raise KeyError(f"Variable '{var}' not found in dataset.")
    da = ds[var]
    if "time" in da.dims and da.sizes.get("time", 1) == 1:
        da = da.isel(time=0)

    if bbox and len(bbox) == 4:
        min_lon, min_lat, max_lon, max_lat = map(float, bbox)
        lon_vals = ds["lon"].values
        min_lon = _ensure_lon_range(lon_vals, min_lon)
        max_lon = _ensure_lon_range(lon_vals, max_lon)
        lon_inc = bool(lon_vals[1] > lon_vals[0])
        lat_vals = ds["lat"].values
        lat_inc = bool(lat_vals[1] > lat_vals[0])
        lon_slice = slice(min_lon, max_lon) if lon_inc else slice(max_lon, min_lon)
        lat_slice = slice(min_lat, max_lat) if lat_inc else slice(max_lat, min_lat)
        da = da.sel(lon=lon_slice, lat=lat_slice)
    else:
        lon_vals = ds["lon"].values
        lat_vals = ds["lat"].values
        lon_inc = bool(lon_vals[1] > lon_vals[0])
        lat_inc = bool(lat_vals[1] > lat_vals[0])

    return da, lat_inc, lon_inc

def _ensure_lat_lon_names(da: xr.DataArray) -> xr.DataArray:
    rename_map = {}
    if "latitude" in da.dims: rename_map["latitude"] = "lat"
    if "longitude" in da.dims: rename_map["longitude"] = "lon"
    return da.rename(rename_map) if rename_map else da

def _select_depth_if_present(da: xr.DataArray, depth_param):
    if "depth" not in da.dims:
        return da, None
    zvals = da["depth"].values
    if depth_param is None or (isinstance(depth_param, str) and depth_param.lower() == "surface"):
        return da.isel(depth=0), float(zvals[0])
    try:
        target = float(depth_param)
        da2 = da.sel(depth=target, method="nearest")
        if "depth" in da2.dims and da2.sizes["depth"] == 1:
            da2 = da2.isel(depth=0)
        return da2, float(da2.coords["depth"].values)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid depth parameter. Use a number (meters) or 'surface'.")

def _prepare_array_for_response(da: xr.DataArray, lat_inc: bool):
    da2 = da.transpose("lat", "lon")
    lat_vals = da2["lat"].values
    lon_vals = da2["lon"].values
    arr2 = da2.values
    if not lat_inc:
        arr2 = arr2[::-1, :]
        lat_vals = lat_vals[::-1]
    dlon = float(abs(np.mean(np.diff(lon_vals)))) if lon_vals.size > 1 else np.nan
    dlat = float(abs(np.mean(np.diff(lat_vals)))) if lat_vals.size > 1 else np.nan
    h, w = arr2.shape
    return arr2, dlon, dlat, w, h

# ---- bbox limits & cell budget ----
BBOX_LIMITS = {
    "min_lon": -118.8389887,
    "min_lat":  -52.3232408,
    "max_lon":  194.4699022,
    "max_lat":   69.7861536,
}
MAX_CELLS = 5_250_000

# 보수적(가장 촘촘함) 해상도 가정: 0.083°
ASSUMED_DLON = 0.083
ASSUMED_DLAT = 0.083

def _validate_bbox_limits_raw(bbox: Optional[List[float]]):
    if not bbox:
        return
    if len(bbox) != 4:
        raise HTTPException(status_code=422, detail="bbox must be [minLon, minLat, maxLon, maxLat]")
    min_lon, min_lat, max_lon, max_lat = map(float, bbox)
    lim = BBOX_LIMITS
    if (min_lon < lim["min_lon"] or max_lon > lim["max_lon"] or
        min_lat < lim["min_lat"] or max_lat > lim["max_lat"]):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Requested bbox exceeds allowed range.",
                "allowed_bbox": [lim["min_lon"], lim["min_lat"], lim["max_lon"], lim["max_lat"]],
                "hint": "Reduce bbox (minLon,minLat,maxLon,maxLat). Example: bbox=115&bbox=25&bbox=142&bbox=43"
            }
        )

def _estimate_cells_assuming_resolution(bbox: List[float], dlon=ASSUMED_DLON, dlat=ASSUMED_DLAT) -> int:
    """DS를 열지 않고 0.083° 고정 해상도 기준으로 픽셀 수 추정.
       dateline crossing(0~360 도메인)도 지원.
    """
    min_lon, min_lat, max_lon, max_lat = map(float, bbox)
    # 경도 0~360 정규화 후 스팬 계산
    def to0360(x: float) -> float:
        return x + 360.0 if x < 0 else x
    a, b = to0360(min_lon), to0360(max_lon)
    if a > b:
        lon_span = (360.0 - a) + b  # dateline crossing
    else:
        lon_span = b - a
    lat_span = abs(max_lat - min_lat)

    # 경계 포함 가정(+1)
    width  = int(np.floor(lon_span / dlon)) + 1
    height = int(np.floor(lat_span / dlat)) + 1
    return max(0, width) * max(0, height)
