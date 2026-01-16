# -*- coding: utf-8 -*-
import os, numpy as np, xarray as xr, folium

# ===== 설정 =====
PATH = "cmems_obs-wind_glo_phy_nrt_l4_0.125deg_PT1H_2025081221_R20250812T12_09.nc"
TIME_IDX = 0
REGION = (28, 33, 30, 35)   # (lat_min, lat_max, lon_min, lon_max) : 수에즈
STEP = 1                    # 무거우면 2~5
OUT_DIR = "folium_maps_suez"
os.makedirs(OUT_DIR, exist_ok=True)

# ===== 유틸 =====
def get_latlon_names(ds):
    lat = [c for c in ds.coords if c.lower() in ("lat","latitude")][0]
    lon = [c for c in ds.coords if c.lower() in ("lon","longitude")][0]
    return lat, lon

def orient_slice(a, b, asc): return slice(a, b) if asc else slice(b, a)

def add_title_overlay(m, title_text):
    html = f"""
    <div style="position:fixed;top:10px;left:50%;transform:translateX(-50%);
                z-index:9999;pointer-events:none;background:rgba(255,255,255,0);">
      <h1 style="margin:0;font-size:20px;color:#111;
                 text-shadow:0 0 6px rgba(255,255,255,.85);">{title_text}</h1>
    </div>
    """
    m.get_root().html.add_child(folium.Element(html))

def make_label_map(ds_like, var_name, region, time_idx=0, step=1, zoom=6, title=""):
    lat_name, lon_name = get_latlon_names(ds_like)
    lat_min, lat_max, lon_min, lon_max = region

    lats = ds_like[lat_name].values; lons = ds_like[lon_name].values
    lat_asc = lats[0] < lats[-1];    lon_asc = lons[0] < lons[-1]

    sub = ds_like[[var_name]].isel(time=time_idx).sel(
        **{lat_name: orient_slice(lat_min, lat_max, lat_asc),
           lon_name: orient_slice(lon_min, lon_max, lon_asc)}
    )

    lats = sub[lat_name].values; lons = sub[lon_name].values
    vals = sub[var_name].values  # (lat, lon)

    LON, LAT = np.meshgrid(lons, lats)
    lon_flat = LON.ravel(); lat_flat = LAT.ravel(); val_flat = vals.ravel()

    center = [(lat_min+lat_max)/2, (lon_min+lon_max)/2]
    m = folium.Map(location=center, zoom_start=zoom, control_scale=True, tiles="OpenStreetMap")
    if title: add_title_overlay(m, title)

    unit = (sub[var_name].attrs.get("units","") or "").lower()
    is_deg = unit.startswith("deg") or ("degree" in unit)

    for i in range(0, len(val_flat), step):
        v = val_flat[i]
        if np.isnan(v):
            text, color = "NaN", "red"
        else:
            val_txt = f"{float(v):.4f}"

            text, color = (f"{val_txt}°" if is_deg else val_txt), "black"

        folium.Marker(
            location=[float(lat_flat[i]), float(lon_flat[i])],
            icon=folium.DivIcon(html=f"<div style='font-size:8px;color:{color};line-height:1'>{text}</div>")
        ).add_to(m)
    return m

# ===== 데이터 열기 & 계산 =====
ds = xr.open_dataset(PATH)
lat_name, lon_name = get_latlon_names(ds)

u = ds["eastward_wind"].rename("eastward_wind")
v = ds["northward_wind"].rename("northward_wind")
u.attrs.update(units="m s-1", long_name="Eastward wind component")
v.attrs.update(units="m s-1", long_name="Northward wind component")

# 풍속
speed = np.sqrt(u**2 + v**2).rename("wind_speed")
speed.attrs.update(units="m s-1", long_name="Wind speed (magnitude)")

# 방향(단 하나): (90° - atan2(v,u)) mod 360  → 북=0°, 시계방향
wd_deg = (90.0 - np.degrees(np.arctan2(v, u))) % 360.0
wd_deg = wd_deg.rename("wind_direction_deg")
wd_deg.attrs.update(units="degree", long_name="Wind direction (deg, 0°=North, clockwise)")

# Folium용 공통 Dataset
ds_like = xr.Dataset({
    "eastward_wind": u, "northward_wind": v,
    "wind_speed": speed, "wind_direction_deg": wd_deg,
    lat_name: ds[lat_name], lon_name: ds[lon_name], "time": ds["time"]
})

tstr = np.datetime_as_string(ds["time"].values[TIME_IDX], unit="m")
fname = os.path.basename(PATH)

# ===== HTML 생성 (u, v, speed, 단일 direction) =====
plots = [
    ("eastward_wind",     f"[U] eastward_wind (m/s) — {fname} — t={tstr}"),
    ("northward_wind",    f"[V] northward_wind (m/s) — {fname} — t={tstr}"),
    ("wind_speed",        f"[Speed] wind_speed (m/s) — {fname} — t={tstr}"),
    ("wind_direction_deg",f"[Direction] (90° - atan2(v,u)) mod 360 — {fname} — t={tstr}"),
]

# -*- coding: utf-8 -*-
import math
import numpy as np
import folium

def _pick_coord_name(ds, cand):
    """좌표 이름(lat/lon)이 'latitude'/'longitude' 또는 'lat'/'lon' 등 다양한 경우 대응"""
    for c in cand:
        if c in ds.coords:
            return c
        if c in ds.dims:
            return c
    raise KeyError(f"좌표 이름을 찾지 못했습니다. 후보: {cand}")

def _slice_region(ds, lat_name, lon_name, region):
    """REGION=(lat_min, lat_max, lon_min, lon_max) 영역 슬라이싱 (좌표 오름/내림 정렬 모두 대응)"""
    lat_min, lat_max, lon_min, lon_max = region

    lat_vals = ds[lat_name].values
    lon_vals = ds[lon_name].values

    lat_asc = lat_vals[0] < lat_vals[-1]
    lon_asc = lon_vals[0] < lon_vals[-1]

    lat_slice = slice(lat_min, lat_max) if lat_asc else slice(lat_max, lat_min)
    lon_slice = slice(lon_min, lon_max) if lon_asc else slice(lon_max, lon_min)

    return ds.sel({lat_name: lat_slice, lon_name: lon_slice})

def _inject_global_css(m, label_font_px):
    """모든 var 라벨에 공통 적용할 CSS를 HTML에 주입 (가독성 높은 스타일)"""
    style = f"""
    <style>
      .val-label {{
        font-size:{int(label_font_px)}px;
        font-weight:700;
        color:#111;
        line-height:1;
        white-space:nowrap;
        text-shadow:
          -1px -1px 0 #fff, 1px -1px 0 #fff,
          -1px  1px 0 #fff, 1px  1px 0 #fff;
        background: rgba(255,255,255,0.65);
        padding:2px 4px;
        border-radius:3px;
        pointer-events:none;
      }}
      .map-title {{
        position: absolute;
        top: 8px; left: 12px;
        z-index: 1000;
        background: rgba(255,255,255,0.85);
        padding: 6px 10px;
        border-radius: 6px;
        font-size: 16px;
        font-weight: 600;
      }}
    </style>
    """
    m.get_root().html.add_child(folium.Element(style))

def _add_title(m, title):
    if not title:
        return
    m.get_root().html.add_child(
        folium.Element(f"<div class='map-title'>{title}</div>")
    )

def make_label_map(
    ds_like,
    var,
    REGION,
    time_idx=0,
    step=1,
    zoom=6,
    title=None,
    label_font_px=18,
    decimals=3,
    value_formatter=None,
    tiles="OpenStreetMap"
):
    """
    ds_like[var]를 지도 위 격자 위치에 라벨(숫자)로 표시한 Folium Map 생성.

    Parameters
    ----------
    ds_like : xarray.Dataset
        'time' 축과 위경도 축을 가진 데이터셋 (이름은 가변적이어도 됨).
    var : str
        표시할 변수명 (ds_like[var]).
    REGION : tuple
        (lat_min, lat_max, lon_min, lon_max)
    time_idx : int
        사용할 시간 인덱스.
    step : int
        샘플링 간격 (1 이면 모든 격자).
    zoom : int
        초기 지도 줌 레벨.
    title : str or None
        지도 좌상단에 표시할 제목(선택).
    label_font_px : int
        var 라벨 표기 글씨 크기(px). 모든 var에 통일 적용.
    decimals : int
        기본 숫자 포맷 소수점 자리.
    value_formatter : callable or None
        사용자 포맷터. 예: lambda v: f"{v:.1f}"
    tiles : str
        베이스맵 타일 이름.
    """
    # 좌표/시간 이름 탐색
    lat_name = _pick_coord_name(ds_like, ["latitude", "lat", "Latitude", "y"])
    lon_name = _pick_coord_name(ds_like, ["longitude", "lon", "Longitude", "x"])
    time_name = None
    for cand in ["time", "Time", "t"]:
        if cand in ds_like.coords or cand in ds_like.dims:
            time_name = cand
            break

    # 시간 선택
    data = ds_like[var]
    if time_name and time_name in data.dims:
        data = data.isel({time_name: time_idx})

    # 영역 슬라이싱
    data = _slice_region(data, lat_name, lon_name, REGION)

    # 중심점 계산
    lat_min, lat_max, lon_min, lon_max = REGION
    center_lat = (lat_min + lat_max) / 2.0
    center_lon = (lon_min + lon_max) / 2.0

    # 지도 생성 & CSS/타이틀 주입
    m = folium.Map(location=[center_lat, center_lon], zoom_start=zoom, tiles=tiles)
    _inject_global_css(m, label_font_px)
    _add_title(m, title)

    # 격자 좌표/값
    lats = data[lat_name].values
    lons = data[lon_name].values
    vals = data.values  # 2D (lat, lon)

    # step 샘플링
    lats_s = lats[::step]
    lons_s = lons[::step]
    vals_s = vals[::step, ::step]

    # 라벨 생성
    for i, lat in enumerate(lats_s):
        for j, lon in enumerate(lons_s):
            v = vals_s[i, j]
            if v is None or (isinstance(v, float) and math.isnan(v)):
                continue

            if value_formatter is not None:
                label = value_formatter(v)
            else:
                label = f"{float(v):.{int(decimals)}f}"

            folium.Marker(
                [float(lat), float(lon)],
                icon=folium.DivIcon(html=f"<div class='val-label'>{label}</div>")
            ).add_to(m)

    return m

os.makedirs(OUT_DIR, exist_ok=True)
for var, title in plots:
    print(f"Rendering {var} …")
    m = make_label_map(
        ds_like, var, REGION,
        time_idx=TIME_IDX,
        step=STEP,         # 1이면 전 격자, 2면 2칸마다
        zoom=6,
        title=title,
        label_font_px=18,  # ← 모든 var 라벨 통일 크기
        decimals=3         # ← 표기 자리수 통일
    )
    out = os.path.join(OUT_DIR, f"{var}_t{TIME_IDX}_labels.html")
    m.save(out)
    print(f" -> {out}")

print("✅ All HTMLs saved in:", OUT_DIR)


