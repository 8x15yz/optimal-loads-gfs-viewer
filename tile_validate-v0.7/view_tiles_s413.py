"""
S-413 스타일 H5 타일들을 전지구 0.25도 격자로 조립해 컨투어로 표시합니다.

예)
  python view_tiles_s413.py downloaded_s413_260521_06z
  python view_tiles_s413.py --tile-dir downloaded --feature SignificantWaveHeight --field significantWaveHeight --time-step Group_001
  python view_tiles_s413.py --feature WaveWind --field windSpeed --time-point 20260522T030000Z
  python view_tiles_s413.py --feature PeakWaveDirection --field peakWaveDirection --time-step Group_008
"""

import argparse
import warnings
from pathlib import Path

import h5py
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np

warnings.filterwarnings("ignore")

# 전지구 0.25도 격자: lat 90 -> -90, lon -180 -> 179.75
N_LAT, N_LON = 721, 1440
RESOLUTION = 0.25
LAT_AXIS = np.linspace(90, -90, N_LAT, dtype=np.float32)
LON_AXIS = np.linspace(-180, 179.75, N_LON, dtype=np.float32)

DEFAULT_FIELDS = {
    "SignificantWaveHeight": "significantWaveHeight",
    "PeakWaveDirection": "peakWaveDirection",
    "WaveWind": "windSpeed",
}


def first_instance_name(h5: h5py.File, feature: str) -> str | None:
    """예: SignificantWaveHeight -> SignificantWaveHeight.01"""
    if feature not in h5:
        return None

    for name, obj in h5[feature].items():
        if isinstance(obj, h5py.Group) and name.startswith(feature + "."):
            return name
    return None


def find_group_by_time_point(instance: h5py.Group, time_point: str) -> str | None:
    """timePoint 속성으로 Group_XXX를 찾음."""
    for name, obj in instance.items():
        if not name.startswith("Group_"):
            continue
        if obj.attrs.get("timePoint") == time_point:
            return name
    return None


def read_tile(fp: Path, feature: str, field: str, time_step: str | None, time_point: str | None):
    """타일 1개에서 좌표 속성과 values 배열을 읽음."""
    with h5py.File(fp, "r") as h5:
        instance_name = first_instance_name(h5, feature)
        if instance_name is None:
            return None

        instance = h5[f"{feature}/{instance_name}"]

        group_name = time_step
        if time_point:
            group_name = find_group_by_time_point(instance, time_point)

        if not group_name:
            return None

        values_path = f"{feature}/{instance_name}/{group_name}/values"
        if values_path not in h5:
            return None

        values = h5[values_path][:]
        if field not in values.dtype.names:
            raise KeyError(
                f"{fp.name}: field '{field}'가 없습니다. "
                f"사용 가능 필드: {values.dtype.names}"
            )

        attrs = instance.attrs
        lat0 = float(attrs["gridOriginLatitude"])
        lon0 = float(attrs["gridOriginLongitude"])
        dlat = float(attrs["gridSpacingLatitudinal"])
        dlon = float(attrs["gridSpacingLongitudinal"])
        nlat = int(attrs["numPointsLatitudinal"])
        nlon = int(attrs["numPointsLongitudinal"])
        actual_time = h5[f"{feature}/{instance_name}/{group_name}"].attrs.get("timePoint", group_name)

        data = values[field].astype(np.float32)

        # S-413 변환 결과에서 결측은 보통 -9999.0
        data = np.where(data <= -9990, np.nan, data)

        return {
            "data": data,
            "lat0": lat0,
            "lon0": lon0,
            "dlat": dlat,
            "dlon": dlon,
            "nlat": nlat,
            "nlon": nlon,
            "group_name": group_name,
            "time_point": actual_time,
        }


def put_on_global_grid(global_grid: np.ndarray, tile: dict) -> bool:
    """타일 배열을 전지구 격자 위치에 삽입."""
    lat0 = tile["lat0"]
    lon0 = tile["lon0"]
    dlat = tile["dlat"]
    dlon = tile["dlon"]
    nlat = tile["nlat"]
    nlon = tile["nlon"]
    data = tile["data"]

    lat_last = lat0 + (nlat - 1) * dlat
    lon_last = lon0 + (nlon - 1) * dlon

    lat_north = max(lat0, lat_last)
    lat_south = min(lat0, lat_last)
    lon_west = min(lon0, lon_last)
    lon_east = max(lon0, lon_last)

    gi_top = round((90 - lat_north) / RESOLUTION)
    gi_bottom = gi_top + nlat
    gj_left = round((lon_west + 180) / RESOLUTION)
    gj_right = gj_left + nlon

    if gi_top < 0 or gi_bottom > N_LAT or gj_left < 0 or gj_right > N_LON:
        return False

    # global_grid는 북->남. 타일도 dlat<0이면 북->남, dlat>0이면 남->북이므로 뒤집어 맞춤.
    data_for_global = data if dlat < 0 else data[::-1, :]

    # 경도도 dlon<0인 경우를 대비
    if dlon < 0:
        data_for_global = data_for_global[:, ::-1]

    global_grid[gi_top:gi_bottom, gj_left:gj_right] = data_for_global
    return True


def tile_box(tile: dict, label: str):
    lat0 = tile["lat0"]
    lon0 = tile["lon0"]
    dlat = tile["dlat"]
    dlon = tile["dlon"]
    nlat = tile["nlat"]
    nlon = tile["nlon"]

    lat_last = lat0 + (nlat - 1) * dlat
    lon_last = lon0 + (nlon - 1) * dlon

    lat_south = min(lat0, lat_last)
    lon_west = min(lon0, lon_last)

    # 셀 중심 기준이라 보기 좋게 한 칸 크기를 더해 테두리 표시
    width = nlon * abs(dlon)
    height = nlat * abs(dlat)

    return lon_west, lat_south, width, height, label


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "tile_dir_arg",
        nargs="?",
        help="Tile data folder, for example downloaded_s413_260521_06z",
    )
    parser.add_argument("--tile-dir", default=str(Path(__file__).parent / "downloaded"))
    parser.add_argument("--feature", default="SignificantWaveHeight",
                        choices=["SignificantWaveHeight", "PeakWaveDirection", "WaveWind"])
    parser.add_argument("--field", default=None,
                        help="예: significantWaveHeight, peakWaveDirection, windSpeed, windDirection")
    parser.add_argument("--time-step", default="Group_001",
                        help="예: Group_001. --time-point를 주면 무시됩니다.")
    parser.add_argument("--time-point", default=None,
                        help="예: 20260522T030000Z")
    parser.add_argument("--save", default=None,
                        help="이미지 저장 경로. 예: s413_view.png")
    args = parser.parse_args()

    tile_dir = Path(args.tile_dir_arg or args.tile_dir)
    field = args.field or DEFAULT_FIELDS[args.feature]

    files = sorted(tile_dir.glob("*.h5"))
    print(f"타일 {len(files)}개 로드 중... ({tile_dir})")
    print(f"feature={args.feature}, field={field}, time_step={args.time_step}, time_point={args.time_point}")

    global_grid = np.full((N_LAT, N_LON), np.nan, dtype=np.float32)
    boxes = []
    used_time = None
    loaded = 0
    skipped = 0

    for fp in files:
        tile = read_tile(fp, args.feature, field, args.time_step, args.time_point)
        if tile is None:
            skipped += 1
            continue

        ok = put_on_global_grid(global_grid, tile)
        if not ok:
            skipped += 1
            continue

        loaded += 1
        used_time = tile["time_point"]
        label = fp.stem.split("_")[-1]
        boxes.append(tile_box(tile, label))

    valid_count = np.count_nonzero(~np.isnan(global_grid))
    print(f"로드 타일 수: {loaded}, 스킵: {skipped}")
    print(f"유효 격자 수: {valid_count:,}")

    if valid_count == 0:
        raise RuntimeError("표시할 유효 격자가 없습니다. feature/field/time-step 또는 tile-dir를 확인하세요.")

    fig, ax = plt.subplots(figsize=(14, 6))

    im = ax.contourf(
        LON_AXIS,
        LAT_AXIS,
        global_grid,
        levels=60,
        cmap="YlOrRd",
        extend="both",
    )

    cbar_label = field
    if field in ("significantWaveHeight",):
        cbar_label += " (m)"
    elif field in ("windSpeed",):
        cbar_label += " (m/s)"
    elif field in ("windDirection", "peakWaveDirection"):
        cbar_label += " (degree)"

    plt.colorbar(im, ax=ax, label=cbar_label)

    title_time = used_time or args.time_point or args.time_step
    ax.set_title(f"S-413 H5 Tile Assembly | {args.feature}/{field} | {title_time} | tiles: {loaded}")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_xlim(-180, 180)
    ax.set_ylim(-90, 90)

    for lon_west, lat_south, width, height, label in boxes:
        ax.add_patch(
            mpatches.Rectangle(
                (lon_west, lat_south),
                width,
                height,
                linewidth=0.8,
                edgecolor="skyblue",
                facecolor="none",
                zorder=5,
            )
        )
        ax.text(
            lon_west + width,
            lat_south + height,
            label,
            fontsize=7,
            color="skyblue",
            ha="right",
            va="top",
            zorder=6,
            clip_on=True,
        )

    plt.tight_layout()

    if args.save:
        plt.savefig(args.save, dpi=200)
        print(f"저장 완료: {args.save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
