"""
로컬에서 NetCDF 파일로부터 풍속/풍향 계산 스크립트
API 없이 직접 실행 가능
"""
from __future__ import annotations
from typing import Optional, List, Union, Dict, Any
import numpy as np
import xarray as xr
import os
import json


def extract_netcdf_to_json(
    nc_file: str,
    variable: str,
    bbox: Optional[List[float]] = None,
    depth: Optional[Union[float, str]] = None,
    forecast_datetime: Optional[str] = None,
    source: str = "local",
    output_json: Optional[str] = None
):
    """
    단일 NetCDF 파일에서 변수를 읽어 JSON으로 출력
    (원본 eastward_wind, northward_wind 등을 그대로 출력)
    
    Parameters
    ----------
    nc_file : str
        NetCDF 파일 경로
    variable : str
        추출할 변수명 (예: "eastward_wind", "northward_wind")
    bbox : List[float], optional
        [minLon, minLat, maxLon, maxLat] 영역 제한
    depth : float or str, optional
        수심 레벨 (미터) 또는 "surface"
    forecast_datetime : str, optional
        예보 시간 (ISO 8601 형식)
    source : str
        데이터 소스명 (기본값: "local")
    output_json : str, optional
        JSON 형식으로 저장할 파일 경로
        
    Returns
    -------
    dict or xarray.DataArray
        output_json이 지정되면 dict, 아니면 DataArray
    """
    
    print(f"Loading {variable} from: {nc_file}")
    ds = _open_dataset_safely(nc_file)
    
    try:
        # 좌표 정규화
        ds = _normalize_lonlat(ds)
        
        # 변수 선택 및 bbox 적용
        da, lat_inc, _ = _select_da(ds, variable, bbox)
        da = _ensure_lat_lon_names(da)
        
        print(f"Variable shape: {da.shape}")
        
        # depth 선택 (있는 경우)
        da, selected_depth = _select_depth_if_present(da, depth)
        
        if selected_depth is not None:
            print(f"Selected depth: {selected_depth} m")
        
        # 메타데이터 설정
        unit = da.attrs.get("units", "unknown")
        long_name = da.attrs.get("long_name", variable)
        std_name = da.attrs.get("standard_name", variable)
        
        print(f"Result shape: {da.shape}")
        print(f"Result range: {float(np.nanmin(da.values)):.2f} ~ {float(np.nanmax(da.values)):.2f} {unit}")
        
        print("▶⭐⭐⭐⭐😸😸😸")
        print(da.values)
        # JSON 형식으로 변환
        if output_json:
            print(f"Creating JSON response...")
            json_response = _create_json_response(
                da,
                variable,
                unit,
                long_name,
                std_name,
                bbox,
                forecast_datetime
            )
            
            with open(output_json, 'w', encoding='utf-8') as f:
                json.dump(json_response, f, indent=2, ensure_ascii=False)
            print(f"JSON saved to: {output_json}")
            
            return json_response
        
        return da
        
    finally:
        ds.close()


def compute_wind_from_netcdf(
    u_file: str,
    v_file: str,
    variable: str = "wind_speed",  # "wind_speed" or "wind_dir"
    bbox: Optional[List[float]] = None,
    depth: Optional[Union[float, str]] = None,
    output_file: Optional[str] = None,
    forecast_datetime: Optional[str] = None,
    source: str = "local",
    output_json: Optional[str] = None
):
    """
    NetCDF 파일에서 U,V 성분을 읽어 풍속 또는 풍향을 계산
    
    Parameters
    ----------
    u_file : str
        eastward_wind (U 성분) NetCDF 파일 경로
    v_file : str
        northward_wind (V 성분) NetCDF 파일 경로
    variable : str
        계산할 변수: "wind_speed" 또는 "wind_dir"
    bbox : List[float], optional
        [minLon, minLat, maxLon, maxLat] 영역 제한
    depth : float or str, optional
        수심 레벨 (미터) 또는 "surface"
    output_file : str, optional
        결과를 저장할 NetCDF 파일 경로
    forecast_datetime : str, optional
        예보 시간 (ISO 8601 형식, 예: "2024-08-05T00:00:00Z")
    source : str
        데이터 소스명 (기본값: "local")
    output_json : str, optional
        JSON 형식으로 저장할 파일 경로
        
    Returns
    -------
    dict or xarray.DataArray
        output_json이 지정되면 dict, 아니면 DataArray
    """
    
    # 1) 파일 열기
    print(f"Loading U component from: {u_file}")
    ds_u = _open_dataset_safely(u_file)
    print(f"Loading V component from: {v_file}")
    ds_v = _open_dataset_safely(v_file)
    
    try:
        # 2) 좌표 정규화
        ds_u = _normalize_lonlat(ds_u)
        ds_v = _normalize_lonlat(ds_v)
        
        # 3) 변수 선택 및 bbox 적용
        da_u, lat_inc_u, _ = _select_da(ds_u, "eastward_wind", bbox)
        da_v, lat_inc_v, _ = _select_da(ds_v, "northward_wind", bbox)
        
        da_u = _ensure_lat_lon_names(da_u)
        da_v = _ensure_lat_lon_names(da_v)
        
        print(f"U component shape: {da_u.shape}")
        print(f"V component shape: {da_v.shape}")
        
        # 4) depth 선택 (있는 경우)
        da_u, selected_depth_u = _select_depth_if_present(da_u, depth)
        da_v, selected_depth_v = _select_depth_if_present(da_v, depth)
        
        if selected_depth_u is not None:
            print(f"Selected depth: {selected_depth_u} m")
        
        # 5) 좌표 정합 (공통 교집합)
        da_u, da_v = xr.align(da_u, da_v, join="inner")
        
        # 6) 계산
        norm_var = _norm_var(variable)
        
        if norm_var == "wind_speed":
            print("Computing wind speed...")
            result = np.hypot(da_u.values, da_v.values)
            unit = "m s-1"
            long_name = "10 m wind speed"
            std_name = "wind_speed"
        elif norm_var == "wind_dir":
            print("Computing wind direction...")
            # TO direction: (90 - atan2(v,u)) mod 360
            result = (90.0 - np.degrees(np.arctan2(da_v.values, da_u.values))) % 360.0

            unit = "degree"
            long_name = "10 m wind direction (from)"
            std_name = "wind_from_direction"
        else:
            raise ValueError(f"Unknown variable: {variable}. Use 'wind_speed' or 'wind_dir'")
        
        # 7) DataArray 생성
        da_result = xr.DataArray(
            result,
            coords=da_u.coords,
            dims=da_u.dims,
            attrs={
                "units": unit,
                "long_name": long_name,
                "standard_name": std_name
            }
        )
        
        print(f"Result shape: {da_result.shape}")
        print(f"Result range: {float(np.nanmin(result)):.2f} ~ {float(np.nanmax(result)):.2f} {unit}")
        
        # 8) 파일로 저장 (옵션)
        if output_file:
            print(f"Saving result to: {output_file}")
            ds_out = da_result.to_dataset(name=norm_var)
            ds_out.to_netcdf(output_file)
            print("Save complete!")
        
        # 9) JSON 형식으로 변환 (옵션)
        if output_json:
            print(f"Creating JSON response with original U/V components...")
            json_response = _create_json_response_with_components(
                da_result,
                da_u,
                da_v,
                norm_var,
                unit,
                long_name,
                std_name,
                bbox,
                forecast_datetime,
                lat_inc_u
            )
            
            with open(output_json, 'w', encoding='utf-8') as f:
                json.dump(json_response, f, indent=2, ensure_ascii=False)
            print(f"JSON saved to: {output_json}")
            
            return json_response
        
        return da_result
        
    finally:
        ds_u.close()
        ds_v.close()


# ---- Helper Functions ----

def _open_dataset_safely(path: str) -> xr.Dataset:
    """NetCDF 파일을 안전하게 열기"""
    last_err = None
    for engine in ("h5netcdf", "netcdf4"):
        try:
            return xr.open_dataset(path, engine=engine)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Failed to open dataset: {last_err}")


def _normalize_lonlat(ds: xr.Dataset) -> xr.Dataset:
    """좌표명을 lon/lat로 정규화"""
    rename_map = {}
    if "longitude" in ds.coords and "lon" not in ds.coords:
        rename_map["longitude"] = "lon"
    if "latitude" in ds.coords and "lat" not in ds.coords:
        rename_map["latitude"] = "lat"
    return ds.rename(rename_map) if rename_map else ds


def _ensure_lon_range(lon_vals: np.ndarray, x: float) -> float:
    """경도 범위 조정 (0-360 vs -180~180)"""
    if lon_vals.max() > 180 and x < 0:
        return x + 360.0
    return x


def _select_da(ds: xr.Dataset, var: str, bbox: Optional[List[float]]):
    """변수 선택 및 bbox 적용"""
    ds = _normalize_lonlat(ds)
    if var not in ds:
        raise KeyError(f"Variable '{var}' not found in dataset.")
    da = ds[var]
    
    # time 차원 제거 (단일 시간인 경우)
    if "time" in da.dims and da.sizes.get("time", 1) == 1:
        da = da.isel(time=0)  # ← 인덱스 선택만 (데이터 값 변경 없음)
    
    # bbox 적용
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
        da = da.sel(lon=lon_slice, lat=lat_slice)  # ← 영역 잘라내기만 (데이터 값 변경 없음)
    else:
        lon_vals = ds["lon"].values
        lat_vals = ds["lat"].values
        lon_inc = bool(lon_vals[1] > lon_vals[0])
        lat_inc = bool(lat_vals[1] > lat_vals[0])
    
    return da, lat_inc, lon_inc  # ← 메타데이터(좌표, 증가/감소 방향)만 반환


def _ensure_lat_lon_names(da: xr.DataArray) -> xr.DataArray:
    """차원명을 lat/lon으로 정규화"""
    rename_map = {}
    if "latitude" in da.dims:
        rename_map["latitude"] = "lat"
    if "longitude" in da.dims:
        rename_map["longitude"] = "lon"
    return da.rename(rename_map) if rename_map else da


def _select_depth_if_present(da: xr.DataArray, depth_param):
    """depth 차원이 있으면 선택"""
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
    except Exception as e:
        raise ValueError(f"Invalid depth parameter: {depth_param}. Use a number (meters) or 'surface'. Error: {e}")


def _norm_var(v: str) -> str:
    """변수명 정규화"""
    v = v.strip().lower()
    if v in ("wind", "wind_speed", "spd", "ws"):
        return "wind_speed"
    if v in ("wdir", "wind_dir", "dir", "wd"):
        return "wind_dir"
    return v


def _prepare_array_for_response(da: xr.DataArray, lat_inc: bool):
    """
    응답용 배열 준비 (lat를 bottom-up으로)
    
    Parameters
    ----------
    da : xr.DataArray
        예시: 
        - dims: ("lat", "lon") 또는 ("lon", "lat")
        - coords: lat=[34.0, 34.5, 35.0, 35.5, 36.0], lon=[128.0, 128.5, 129.0, 129.5, 130.0]
        - values: 2D numpy array (5x5 크기)
    lat_inc : bool
        예시:
        - True: lat가 증가하는 경우 (34.0 -> 36.0)
        - False: lat가 감소하는 경우 (36.0 -> 34.0)
    
    Returns
    -------
    tuple
        (arr2, dlon, dlat, width, height)
        예시: (numpy array (5x5), 0.5, 0.5, 5, 5)
    
    Examples
    --------
    >>> # lat 증가 케이스
    >>> da = xr.DataArray(
    ...     np.random.rand(5, 5),
    ...     coords={"lat": [34.0, 34.5, 35.0, 35.5, 36.0], 
    ...             "lon": [128.0, 128.5, 129.0, 129.5, 130.0]},
    ...     dims=["lat", "lon"]
    ... )
    >>> arr, dlon, dlat, w, h = _prepare_array_for_response(da, lat_inc=True)
    >>> # arr: 배열 그대로, dlon=0.5, dlat=0.5, w=5, h=5
    
    >>> # lat 감소 케이스
    >>> da2 = xr.DataArray(
    ...     np.random.rand(5, 5),
    ...     coords={"lat": [36.0, 35.5, 35.0, 34.5, 34.0], 
    ...             "lon": [128.0, 128.5, 129.0, 129.5, 130.0]},
    ...     dims=["lat", "lon"]
    ... )
    >>> arr2, dlon, dlat, w, h = _prepare_array_for_response(da2, lat_inc=False)
    >>> # arr2: 상하 반전됨, dlon=0.5, dlat=0.5, w=5, h=5
    """
    da2 = da.transpose("lat", "lon")
    lat_vals = da2["lat"].values
    lon_vals = da2["lon"].values
    arr2 = da2.values
    
    # lat가 감소하는 경우 뒤집기 (bottom-up)
    if not lat_inc:
        arr2 = arr2[::-1, :]
        lat_vals = lat_vals[::-1]
    
    dlon = float(abs(np.mean(np.diff(lon_vals)))) if lon_vals.size > 1 else np.nan
    dlat = float(abs(np.mean(np.diff(lat_vals)))) if lat_vals.size > 1 else np.nan
    h, w = arr2.shape
    
    return arr2, dlon, dlat, w, h


def _create_json_response(
    da: xr.DataArray,
    variable: str,
    unit: str,
    name_en: str,
    std_name: str,
    bbox: Optional[List[float]],
    timestamp: Optional[str]
) -> Dict[str, Any]:
    """
    API와 동일한 형식의 JSON 응답 생성
    """
    # lat 증가 여부 확인
    lat_vals = da["lat"].values
    lat_inc = bool(lat_vals[1] > lat_vals[0]) if len(lat_vals) > 1 else True
    
    # 배열 준비
    arr2, dlon, dlat, width, height = _prepare_array_for_response(da, lat_inc)
    
    # bbox 계산
    if bbox is None:
        bbox = [
            float(da["lon"].values.min()),
            float(da["lat"].values.min()),
            float(da["lon"].values.max()),
            float(da["lat"].values.max()),
        ]
    
    # 원본 값 그대로 내보내기 (스케일링 없음)
    enc = np.empty_like(arr2, dtype=object)
    mask_nan = np.isnan(arr2)
    enc[mask_nan] = None
    enc[~mask_nan] = arr2[~mask_nan].astype(float)
    
    scale = 1.0
    nodata = None
    
    # JSON 응답 구조
    response = {
        "timestamp": timestamp if timestamp else "unknown",
        "variable": variable,
        "unit": unit,
        "name_en": name_en,
        "standard_name": std_name,
        "bbox": bbox,
        "resolution": [dlon, dlat],
        "shape": [width, height],
        "indexOrder": "row-major-bottom-up",
        "valueEncoding": {
            "type": "uint16",
            "scale": scale,
            "offset": 0,
            "nodata": nodata
        },
        "data": enc.flatten().tolist()
    }
    
    return response


def _create_json_response_with_components(
    da_computed: xr.DataArray,
    da_u: xr.DataArray,
    da_v: xr.DataArray,
    variable: str,
    unit: str,
    name_en: str,
    std_name: str,
    bbox: Optional[List[float]],
    timestamp: Optional[str],
    lat_inc: bool
) -> Dict[str, Any]:
    """
    원본 U, V 성분과 계산 결과를 함께 포함하는 JSON 응답 생성
    data 형식: [{"eastward": u1, "northward": v1, "computed": result1}, ...]
    """
    # 배열 준비
    arr_computed, dlon, dlat, width, height = _prepare_array_for_response(da_computed, lat_inc)
    arr_u, _, _, _, _ = _prepare_array_for_response(da_u, lat_inc)
    arr_v, _, _, _, _ = _prepare_array_for_response(da_v, lat_inc)
    
    # bbox 계산
    if bbox is None:
        bbox = [
            float(da_computed["lon"].values.min()),
            float(da_computed["lat"].values.min()),
            float(da_computed["lon"].values.max()),
            float(da_computed["lat"].values.max()),
        ]
    
    # 데이터를 객체 배열로 변환
    data_list = []
    flat_u = arr_u.flatten()
    flat_v = arr_v.flatten()
    flat_computed = arr_computed.flatten()
    
    for i in range(len(flat_computed)):
        u_val = float(flat_u[i]) if not np.isnan(flat_u[i]) else None
        v_val = float(flat_v[i]) if not np.isnan(flat_v[i]) else None
        computed_val = float(flat_computed[i]) if not np.isnan(flat_computed[i]) else None
        
        data_list.append({
            "eastward": u_val,
            "northward": v_val,
            "computed": computed_val
        })
    
    # JSON 응답 구조
    response = {
        "timestamp": timestamp if timestamp else "unknown",
        "variable": variable,
        "unit": unit,
        "name_en": name_en,
        "standard_name": std_name,
        "bbox": bbox,
        "resolution": [dlon, dlat],
        "shape": [width, height],
        "indexOrder": "row-major-bottom-up",
        "valueEncoding": {
            "type": "object",
            "description": "Each data point contains eastward (U), northward (V), and computed value"
        },
        "data": data_list
    }
    
    return response


# ---- Main Execution ----

if __name__ == "__main__":
    # 예제 1: 원본 eastward_wind JSON 출력
    # http://43.201.101.103/api/griddata?variable=eastward_wind&forecast_datetime=2024-08-05T00:00:00Z&source=cmems&bbox=128&bbox=34&bbox=130&bbox=36
    
    u_file = "original_eastward_wind_20240805_00Z.nc"
    v_file = "original_northward_wind_20240805_00Z.nc"
    
    print("=" * 80)
    print("Example 1: Original Eastward Wind - JSON output")
    print("=" * 80)
    
    eastward_json = extract_netcdf_to_json(
        nc_file=u_file,
        variable="eastward_wind",
        forecast_datetime="2024-08-05T00:00:00Z",
        source="cmems",
        bbox=[128.0, 34.0, 130.0, 36.0],
        depth="surface",
        output_json="eastward_wind_response.json"
    )
    
    print(f"\nJSON Response Preview:")
    print(f"  - timestamp: {eastward_json['timestamp']}")
    print(f"  - variable: {eastward_json['variable']}")
    print(f"  - bbox: {eastward_json['bbox']}")
    print(f"  - shape: {eastward_json['shape']}")
    print(f"  - resolution: {eastward_json['resolution']}")
    print(f"  - data points: {len(eastward_json['data'])}")
    
    print("\n" + "=" * 80)
    print("Example 2: Original Northward Wind - JSON output")
    print("=" * 80)
    
    northward_json = extract_netcdf_to_json(
        nc_file=v_file,
        variable="northward_wind",
        forecast_datetime="2024-08-05T00:00:00Z",
        source="cmems",
        bbox=[128.0, 34.0, 130.0, 36.0],
        depth="surface",
        output_json="northward_wind_response.json"
    )
    
    print(f"\nJSON Response Preview:")
    print(f"  - timestamp: {northward_json['timestamp']}")
    print(f"  - variable: {northward_json['variable']}")
    print(f"  - shape: {northward_json['shape']}")
    
    print("\n" + "=" * 80)
    print("Example 3: Computed Wind Speed - JSON output")
    print("=" * 80)
    
    wind_speed_json = compute_wind_from_netcdf(
        u_file=u_file,
        v_file=v_file,
        variable="wind_speed",
        forecast_datetime="2024-08-05T00:00:00Z",
        source="cmems",
        bbox=[128.0, 34.0, 130.0, 36.0],
        depth="surface",
        output_json="wind_speed_response.json"
    )
    
    print(f"\nJSON Response Preview:")
    print(f"  - timestamp: {wind_speed_json['timestamp']}")
    print(f"  - variable: {wind_speed_json['variable']}")
    print(f"  - bbox: {wind_speed_json['bbox']}")
    print(f"  - shape: {wind_speed_json['shape']}")
    print(f"  - resolution: {wind_speed_json['resolution']}")
    print(f"  - data points: {len(wind_speed_json['data'])}")
    
    print("\n" + "=" * 80)
    print("Example 4: Computed Wind Direction - JSON output")
    print("=" * 80)
    
    wind_dir_json = compute_wind_from_netcdf(
        u_file=u_file,
        v_file=v_file,
        variable="wind_dir",
        forecast_datetime="2024-08-05T00:00:00Z",
        source="cmems",
        bbox=[128.0, 34.0, 130.0, 36.0],
        depth="surface",
        output_json="wind_direction_response.json"
    )
    
    print(f"\nJSON Response Preview:")
    print(f"  - timestamp: {wind_dir_json['timestamp']}")
    print(f"  - variable: {wind_dir_json['variable']}")
    print(f"  - shape: {wind_dir_json['shape']}")
    
    print("\n" + "=" * 80)
    print("Example 5: NetCDF output (no JSON)")
    print("=" * 80)
    
    da_result = compute_wind_from_netcdf(
        u_file=u_file,
        v_file=v_file,
        variable="wind_speed",
        bbox=None,  # 전체 영역
        depth="surface",
        output_file="computed_wind_speed_full.nc"
    )
    
    print(f"\nDataArray returned: {type(da_result)}")
    print(f"Shape: {da_result.shape}")
    
    print("\n" + "=" * 80)
    print("All computations complete!")
    print("Generated JSON files:")
    print("  - eastward_wind_response.json")
    print("  - northward_wind_response.json")
    print("  - wind_speed_response.json")
    print("  - wind_direction_response.json")
    print("=" * 80)
