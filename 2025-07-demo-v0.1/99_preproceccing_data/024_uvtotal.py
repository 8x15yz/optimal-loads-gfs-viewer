import xarray as xr

# 1. 원본 파일 경로
src = "./SMOC_20250813_R20250812.nc"  # 경로 맞게 변경

# 2. NetCDF 열기
ds = xr.open_dataset(src, engine="netcdf4", chunks={})

# 3. 꺼낼 변수와 좌표 선택
want_vars = ["vtotal", "utotal"]
coords = [c for c in ["time", "depth", "latitude", "longitude", "lat", "lon"] if c in ds.variables]

# 실제로 존재하는 변수만 필터링
vars_in_ds = [v for v in want_vars if v in ds.data_vars]
subset = ds[vars_in_ds + coords]

# 4. 압축 저장
encoding = {}
for v in vars_in_ds:
    encoding[v] = dict(zlib=True, complevel=4, shuffle=True)
for c in coords:
    encoding[c] = {}

out_path = "./SMOC_subset.nc"  # 저장 경로
subset.to_netcdf(out_path, engine="netcdf4", format="NETCDF4", encoding=encoding)

print("저장 완료:", out_path)


# # ### 확인
# import xarray as xr

# # 저장한 파일 경로
# path = "./SMOC_subset.nc"

# # 열기
# ds = xr.open_dataset(path)

# # 전체 구조 확인
# print(ds)

# # 변수 목록
# print("변수들:", list(ds.data_vars))

# # vtotal 데이터 일부 보기
# print(ds['vtotal'])

# # numpy 배열로 변환해서 첫 값 출력
# print(ds['vtotal'].values[0, 0, 0, 0])