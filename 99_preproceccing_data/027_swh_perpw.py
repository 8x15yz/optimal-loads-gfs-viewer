# import xarray as xr

# # 입력 / 출력 파일 경로
# input_file = "mfwamglocep_2025080800_R20250809_00H.nc"
# output_file = "wave_subset.nc"

# # 원본 파일 열기
# ds = xr.open_dataset(input_file)

# # 필요한 변수만 선택하고 이름 변경
# subset = ds[["VHM0", "VTM02", "VMDR"]].rename({
#     "VHM0": "swh",
#     "VTM02": "perpw",
#     "VMDR": "dirpw"
# })

# # 단위 메타데이터 추가
# subset["swh"].attrs["units"] = "m"
# subset["perpw"].attrs["units"] = "s"
# subset["dirpw"].attrs["units"] = "degree_true"

# # 압축 저장
# encoding = {var: {"zlib": True, "complevel": 4} for var in subset.data_vars}
# subset.to_netcdf(output_file, encoding=encoding)

# print(f"저장 완료: {output_file}")

# import xarray as xr

# output_file = "wave_subset.nc"
# ds = xr.open_dataset(output_file)

# print(ds)  # 전체 구조 확인
# print("\n데이터 변수 목록:", list(ds.data_vars))

# # 각 변수별 단위 확인
# for var in ds.data_vars:
#     print(f"{var} 단위:", ds[var].attrs.get("units", "단위 정보 없음"))

# # time 좌표 확인
# if "time" in ds.coords:
#     print("\n=== time 좌표 ===")
#     print(ds.coords["time"])
# else:
#     print("\n이 데이터셋에는 time 좌표가 없습니다.")


import xarray as xr
import pandas as pd
from pathlib import Path


# 입력 / 출력 경로
input_file = "mfwamglocep_2025080800_R20250809_00H.nc"
output_dir = "wave_split"  # 저장 폴더
output_dir_path = Path(output_dir)
output_dir_path.mkdir(exist_ok=True)

# 원본 파일 열기
ds = xr.open_dataset(input_file)

# 필요한 변수 선택 및 이름 변경
subset = ds[["VHM0", "VTM02", "VMDR"]].rename({
    "VHM0": "swh",
    "VTM02": "perpw",
    "VMDR": "dirpw"
})

# 단위 메타데이터 추가
subset["swh"].attrs["units"] = "m"
subset["perpw"].attrs["units"] = "s"
subset["dirpw"].attrs["units"] = "degree_true"

# 시간별로 분리 저장
for t in subset.time.values:
    time_str = pd.to_datetime(t).strftime("%Y%m%d_%H%M%S")
    output_file = output_dir_path / f"wave_{time_str}.nc"
    
    # 해당 시간 slice
    single_time = subset.sel(time=t)

    # 압축 저장
    encoding = {var: {"zlib": True, "complevel": 4} for var in single_time.data_vars}
    single_time.to_netcdf(output_file, encoding=encoding)
    print(f"저장 완료: {output_file}")
