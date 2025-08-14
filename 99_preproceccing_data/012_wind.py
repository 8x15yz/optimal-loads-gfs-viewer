# # # # import xarray as xr

# # # # path = "cmems_obs-wind_glo_phy_nrt_l4_0.125deg_PT1H_2025081221_R20250812T12_09.nc"
# # # # ds = xr.open_dataset(path)

# # # # print(ds)  # 전체 변수/차원 확인
# # # # print(ds.variables.keys())  # 변수 목록만


# # # import xarray as xr
# # # import numpy as np

# # # path = "cmems_obs-wind_glo_phy_nrt_l4_0.125deg_PT1H_2025081221_R20250812T12_09.nc"
# # # ds = xr.open_dataset(path)

# # # u = ds['eastward_wind']
# # # v = ds['northward_wind']

# # # # 풍속 (m/s)
# # # wind_speed = np.sqrt(u**2 + v**2)

# # # # 풍향 (북쪽=0°, 시계방향)
# # # wind_dir = (np.degrees(np.arctan2(u, v)) + 360) % 360

# # # wind_speed.name = 'wind_speed'
# # # wind_dir.name = 'wind_direction'

# # # # 저장 예시
# # # wind_speed.to_netcdf("wind_speed.nc")
# # # wind_dir.to_netcdf("wind_direction.nc")


# # import xarray as xr

# # # 파일 경로
# # speed_file = "wind_speed.nc"
# # direction_file = "wind_direction.nc"

# # # 파일 열기
# # ds_speed = xr.open_dataset(speed_file)
# # ds_direction = xr.open_dataset(direction_file)

# # # 데이터셋 구조 출력
# # print("=== Wind Speed Dataset ===")
# # print(ds_speed)
# # print("\n=== Variables in Wind Speed ===")
# # for var in ds_speed.data_vars:
# #     print(f"{var}: {ds_speed[var].attrs}")

# # print("\n=== Wind Direction Dataset ===")
# # print(ds_direction)
# # print("\n=== Variables in Wind Direction ===")
# # for var in ds_direction.data_vars:
# #     print(f"{var}: {ds_direction[var].attrs}")

# # # 특정 변수 데이터 확인 (예: 첫 5개 값)
# # print("\nSample wind speed values:")
# # print(ds_speed[list(ds_speed.data_vars)[0]].values.flatten()[:5])

# # print("\nSample wind direction values:")
# # print(ds_direction[list(ds_direction.data_vars)[0]].values.flatten()[:5])


# import os
# import folium
# import numpy as np
# import xarray as xr

# # === 데이터 열기 ===
# ds_speed = xr.open_dataset("wind_direction.nc")

# # === 값 라벨 버전 (NaN은 빨간색 'NaN', 유효값은 검정 숫자) ===
# def plot_values_with_labels(ds, var, bbox, step=1):
#     lat_min, lat_max, lon_min, lon_max = bbox
#     subset = ds.sel(lat=slice(lat_min, lat_max), lon=slice(lon_min, lon_max))
#     vals = subset[var].isel(time=0).values

#     lon_grid, lat_grid = np.meshgrid(subset.lon.values, subset.lat.values)
#     lon_flat = lon_grid.ravel()
#     lat_flat = lat_grid.ravel()
#     val_flat = vals.ravel()

#     center_lat = (lat_min + lat_max) / 2
#     center_lon = (lon_min + lon_max) / 2
#     m = folium.Map(location=[center_lat, center_lon], zoom_start=6, control_scale=True)

#     for i in range(0, len(val_flat), step):
#         v = val_flat[i]
#         is_nan = np.isnan(v)
#         color = "red" if is_nan else "black"
#         text = "NaN" if is_nan else f"{v:.2f}"

#         folium.Marker(
#             location=[lat_flat[i], lon_flat[i]],
#             icon=folium.DivIcon(
#                 html=f"<div style='font-size:8px;color:{color};line-height:1'>{text}</div>"
#             )
#         ).add_to(m)
#     return m

# # ✅ 수에즈 운하 근방 범위
# suez_bbox = (28, 33, 30, 35)
# os.makedirs("folium_maps_suez", exist_ok=True)

# # Wind Speed 값 라벨 표시
# m_speed_labels = plot_values_with_labels(ds_speed, "wind_direction", suez_bbox, step=1)
# m_speed_labels.save("folium_maps_suez/wind_direction.html")

# print("✅ wind_direction 값 라벨 지도 생성 완료")

import xarray as xr

# 파일 경로
path = "cmems_obs-wind_glo_phy_nrt_l4_0.125deg_PT1H_2025081221_R20250812T12_09.nc"

# 데이터 열기
ds = xr.open_dataset(path)

# time 변수 확인
time_values = ds.time.values

print("=== Time Coordinate Info ===")
print(f"Total time steps: {len(time_values)}")
print(f"Start time: {time_values[0]}")
print(f"End time:   {time_values[-1]}")

# 전체 변수 목록 (참고)
print("\nVariables:", list(ds.variables.keys()))
