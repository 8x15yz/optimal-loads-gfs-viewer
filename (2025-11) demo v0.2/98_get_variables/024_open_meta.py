import xarray as xr

FILENAME = "cop_CUR_uo_20251106_00Z.nc"

DEPTH_CANDS = ("depth", "deptht", "z", "lev", "depthu", "depthv")
LAT_CANDS   = ("lat", "latitude", "nav_lat", "y")
LON_CANDS   = ("lon", "longitude", "nav_lon", "x")
TIME_CANDS  = ("time", "t")

def _standardize_latlon(da: xr.DataArray) -> xr.DataArray:
    # 좌표 이름 표준화 → lat/lon
    rename_map = {}
    for c in da.coords:
        if c in LAT_CANDS: rename_map[c] = "lat"
        if c in LON_CANDS: rename_map[c] = "lon"
    if rename_map:
        da = da.rename(rename_map)
    return da

def _reduce_to_2d(da: xr.DataArray, depth_strategy="surface") -> xr.DataArray:
    # 1) time 축 처리
    tname = next((t for t in TIME_CANDS if t in da.dims), None)
    if tname:
        if da.sizes[tname] == 0:
            raise ValueError("No time steps")
        if da.sizes[tname] > 1:
            da = da.isel({tname: 0})
        da = da.squeeze(drop=True)

    # 2) depth 축 처리
    zname = next((z for z in DEPTH_CANDS if z in da.dims), None)
    if zname:
        if da.sizes[zname] == 0:
            raise ValueError("No depth levels")
        if da.sizes[zname] > 1:
            if depth_strategy == "surface":
                try:
                    da = da.sel({zname: 0}, method="nearest")
                except Exception:
                    da = da.isel({zname: 0})
            elif depth_strategy == "mean":
                da = da.mean(dim=zname, keep_attrs=True)
            else:
                da = da.isel({zname: 0})
        da = da.squeeze(drop=True)

    # 3) lat/lon 표준화 → 4) 순서 강제
    da = _standardize_latlon(da)
    da = da.transpose("lat", "lon")
    return da

def _subset_bbox(da: xr.DataArray, bbox):
    # bbox: [minLon, minLat, maxLon, maxLat]
    min_lon, min_lat, max_lon, max_lat = bbox
    da = da.sel(lat=slice(min_lat, max_lat))
    if min_lon <= max_lon:
        da = da.sel(lon=slice(min_lon, max_lon))
    else:
        # 180도 경계 횡단
        left  = da.sel(lon=slice(min_lon, 180))
        right = da.sel(lon=slice(-180, max_lon))
        da = xr.concat([left, right], dim="lon")
    return da

def main():
    try:
        ds = xr.open_dataset(FILENAME)
    except FileNotFoundError:
        print(f"❌ 파일을 찾을 수 없습니다: {FILENAME}")
        return

    print("\n=== BASIC NETCDF INFO (RAW) ===")
    print(ds)

    print("\n=== COORDS (RAW) ===")
    for c in ds.coords:
        print(f"- {c}: shape={ds[c].shape}")

    print("\n=== VARIABLES (RAW) ===")
    for v in ds.data_vars:
        print(f"- {v}: shape={ds[v].shape}, dtype={ds[v].dtype}")

    # ---- uo → 2D로 축소
    da = ds["uo"]
    da2d = _reduce_to_2d(da)  # (lat, lon)

    print("\n=== AFTER REDUCE TO 2D ===")
    print(f"dims: {da2d.dims}, shape={da2d.shape}")
    print(f"lat: {da2d['lat'].values[0]} .. {da2d['lat'].values[-1]}, "
          f"N={da2d.sizes['lat']}")
    print(f"lon: {da2d['lon'].values[0]} .. {da2d['lon'].values[-1]}, "
          f"N={da2d.sizes['lon']}")

    # ---- (선택) BBOX 적용: 한국 동남부 예시
    # bbox = [128, 34, 130, 36]
    # da2d = _subset_bbox(da2d, bbox)
    # print("\n=== AFTER BBOX SUBSET ===")
    # print(f"dims: {da2d.dims}, shape={da2d.shape}")

    # 그리드 간격, 크기 등 확인
    dlat = float(da2d["lat"][1] - da2d["lat"][0]) if da2d.sizes["lat"] > 1 else float("nan")
    dlon = float(da2d["lon"][1] - da2d["lon"][0]) if da2d.sizes["lon"] > 1 else float("nan")
    print(f"\nΔlat ≈ {dlat}, Δlon ≈ {dlon}")
    print(f"width={da2d.sizes['lon']}, height={da2d.sizes['lat']}")

    ds.close()
    print("\n✅ 완료")

if __name__ == "__main__":
    main()
