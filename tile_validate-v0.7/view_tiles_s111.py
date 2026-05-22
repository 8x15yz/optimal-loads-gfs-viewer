"""
downloaded/ 의 S-111 h5 타일 전체를 전지구 격자로 조립해 컨투어 뷰어 표시

Usage:
  python view_tiles_s111.py downloaded_s111_260521_06z
  python view_tiles_s111.py --tile-dir downloaded_s111_260521_06z
"""

import argparse
import warnings
warnings.filterwarnings("ignore")

from pathlib import Path
import numpy as np
import h5py
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

parser = argparse.ArgumentParser()
parser.add_argument(
    "tile_dir_arg",
    nargs="?",
    help="Tile data folder, for example downloaded_s111_260521_06z",
)
parser.add_argument("--tile-dir", default=str(Path(__file__).parent / "downloaded"))
parser.add_argument("--time-step", default="Group_008")
args = parser.parse_args()

TILE_DIR  = Path(args.tile_dir_arg or args.tile_dir)
TIME_STEP = args.time_step   # 2026-05-21 03Z (GRIB 유효시각과 동일)

# ── 전지구 격자 초기화 (0.25도, lat: 90→-90, lon: -180→179.75) ──────────
NLat, NLon   = 721, 1440
lat_axis     = np.linspace(90,    -90,    NLat)   # 북→남
lon_axis     = np.linspace(-180,  179.75, NLon)   # 서→동
global_speed = np.full((NLat, NLon), np.nan, dtype=np.float32)

# ── 타일별 조립 ────────────────────────────────────────────────────────────
files = sorted(TILE_DIR.glob("*.h5"))
print(f"타일 {len(files)} 개 로드 중...")

tile_boxes = []   # (lon_west, lat_south, lon_width, lat_height, filename)

for fp in files:
    with h5py.File(fp, "r") as f:
        sc_path = "SurfaceCurrent/SurfaceCurrent.01"
        if sc_path not in f:
            continue

        attrs = dict(f[sc_path].attrs)
        lat0  = float(attrs["gridOriginLatitude"])    # 남쪽 기준점
        lon0  = float(attrs["gridOriginLongitude"])   # 서쪽 기준점
        dlat  = float(attrs["gridSpacingLatitudinal"])
        dlon  = float(attrs["gridSpacingLongitudinal"])
        nlat  = int(attrs["numPointsLatitudinal"])
        nlon  = int(attrs["numPointsLongitudinal"])

        grp_key = f"{sc_path}/{TIME_STEP}/values"
        if grp_key not in f:
            continue

        raw   = f[grp_key][:]                                 # structured array (nlat, nlon)
        speed = raw["surfaceCurrentSpeed"].astype(np.float32) # 남→북

        # fill value (0 이하) → NaN
        speed = np.where(speed > 0, speed, np.nan)

        # 전지구 격자 인덱스 계산
        gi_bottom = round((90 - lat0)                      / 0.25)
        gi_top    = round((90 - (lat0 + (nlat-1)*dlat))   / 0.25)
        gj_left   = round((lon0 + 180)                    / 0.25)
        gj_right  = gj_left + nlon

        if gi_top < 0 or gi_bottom >= NLat or gj_left < 0 or gj_right > NLon:
            continue

        global_speed[gi_top:gi_bottom + 1, gj_left:gj_right] = speed[::-1, :]

        # 테두리·라벨용 정보 저장
        tile_boxes.append((
            lon0,
            lat0,
            nlon * dlon,          # 경도 폭
            nlat * dlat,          # 위도 높이
            fp.stem.split("_")[-1],  # "001"
        ))

valid_count = np.count_nonzero(~np.isnan(global_speed))
print(f"유효 격자 수: {valid_count:,}")

# ── 컨투어 뷰어 ────────────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(14, 6))

im = ax.contourf(lon_axis, lat_axis, global_speed,
                 levels=60, cmap="YlOrRd", extend="max")

plt.colorbar(im, ax=ax, label="Surface Current Speed (m/s)")
ax.set_title(f"Tiled H5 Assembly  |  {TIME_STEP} = 2026-05-21 03Z  |  tiles: {len(files)}")
ax.set_xlabel("Longitude")
ax.set_ylabel("Latitude")
ax.set_xlim(-180, 180)
ax.set_ylim(-90,  90)

# ── 타일 테두리 + 파일명 라벨 ───────────────────────────────────────────────
for lon_west, lat_south, w, h, fname in tile_boxes:
    ax.add_patch(mpatches.Rectangle(
        (lon_west, lat_south), w, h,
        linewidth=0.8, edgecolor="skyblue", facecolor="none", zorder=5,
    ))
    ax.text(
        lon_west + w, lat_south + h,   # 우상단 꼭짓점
        fname,
        fontsize=7, color="skyblue",
        ha="right", va="top", zorder=6,
        clip_on=True,
    )

plt.tight_layout()
plt.show()
