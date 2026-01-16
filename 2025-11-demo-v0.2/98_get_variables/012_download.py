from datetime import datetime, timedelta, timezone
from copernicusmarine import subset
from pathlib import Path
import boto3
import os
import sys
import time
import argparse

# ---- 사용자 설정 (012: Wind) ----
DATASET_ID = "cmems_obs-wind_glo_phy_nrt_l4_0.125deg_PT1H"
PRODUCT_CODE = "012"
VARS = ["eastward_wind", "northward_wind"]    # 10m U/V (m/s)
BUCKET = "optimal-loads"
S3_PREFIX_ROOT = f"cmems/{PRODUCT_CODE}"      # s3://optimal-loads/cmems/012/<var>/<YYYY>/<MM>/
REGION = "ap-northeast-2"

# 전구(전체) 다운로드 → bbox 없으면 None 유지
AREA = None   # (min_lon, max_lon, min_lat, max_lat)

UTC = timezone.utc
STEP = timedelta(hours=1)  # ★ hourly

# ---- 저장 경로: 스크립트 파일과 동일 폴더 ----
BASE_DIR = Path(__file__).resolve().parent


# ---- 유틸 ----
def parse_utc(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def snap_to_1h_floor(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def snap_to_1h_ceil(dt: datetime) -> datetime:
    if dt.minute == dt.second == dt.microsecond == 0:
        return dt
    return dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def has_hdf5_magic(path: Path) -> bool:
    sig = b'\x89HDF\r\n\x1a\n'
    try:
        with open(path, "rb") as f:
            return f.read(len(sig)) == sig
    except Exception:
        return False


def download_subset(tmp_path: Path, var: str, t: datetime, compress_level=None):
    kwargs = {
        "dataset_id": DATASET_ID,
        "variables": [var],
        "start_datetime": iso_z(t),
        "end_datetime": iso_z(t),
        "output_filename": str(tmp_path),
    }
    if compress_level:
        kwargs["netcdf_compression_level"] = int(compress_level)
    if AREA:
        kwargs.update({
            "minimum_longitude": AREA[0],
            "maximum_longitude": AREA[1],
            "minimum_latitude": AREA[2],
            "maximum_latitude": AREA[3],
        })
    subset(**kwargs)


def main():
    ap = argparse.ArgumentParser(description="CMEMS wind subset → S3 (사용자 지정 시/종 UTC)")
    ap.add_argument("START_UTC", help="예: 2024-08-05T00:00:00Z")
    ap.add_argument("END_UTC",   help="예: 2024-08-05T06:00:00Z (포함 범위)")
    ap.add_argument("--compress", type=int, default=None, help="NetCDF 압축레벨 1~9 (선택)")
    args = ap.parse_args()

    start_raw = parse_utc(args.START_UTC)
    end_raw   = parse_utc(args.END_UTC)
    if start_raw > end_raw:
        print("❌ START_UTC 가 END_UTC 보다 큽니다.")
        sys.exit(2)

    START_UTC = snap_to_1h_floor(start_raw)
    END_UTC   = snap_to_1h_ceil(end_raw)
    if START_UTC != start_raw or END_UTC != end_raw:
        print(f"ℹ️  입력 시간이 1시간 그리드와 달라 스냅되었습니다.")
        print(f"   ▶ START_UTC: {start_raw.isoformat()}  →  {START_UTC.isoformat()}")
        print(f"   ▶ END_UTC  : {end_raw.isoformat()}    →  {END_UTC.isoformat()}")

    s3 = boto3.client("s3", region_name=REGION)

    print(f"▶ RUN (UTC)     : {START_UTC.isoformat()}  →  {END_UTC.isoformat()}  (hourly)")
    print(f"▶ Local save    : {BASE_DIR}")
    print(f"▶ S3 prefix     : s3://{BUCKET}/{S3_PREFIX_ROOT}/<var>/<YYYY>/<MM>/")
    print(f"▶ Variables     : {', '.join(VARS)}")
    if AREA:
        print(f"▶ BBOX          : {AREA}")
    print("")

    t = START_UTC
    created = skipped = failed = 0

    while t <= END_UTC:
        for var in VARS:
            out_name = f"cop_{PRODUCT_CODE}_{var}_{t:%Y%m%d_%H}Z.nc"
            out_path = BASE_DIR / out_name
            tmp_path = BASE_DIR / (out_name + ".part.nc")

            yyyy = f"{t:%Y}"
            mm   = f"{t:%m}"
            s3_key = f"{S3_PREFIX_ROOT}/{var}/{yyyy}/{mm}/{out_name}"

            if out_path.exists():
                print(f"⏭️  exists, skip local: {out_name}")
                skipped += 1
            else:
                print(f"⏬ subset  var={var}  time={t.isoformat()}  → {out_name}")
                for p in [tmp_path, out_path]:
                    try:
                        if p.exists(): p.unlink()
                    except Exception:
                        pass

                try:
                    download_subset(tmp_path, var, t, compress_level=args.compress)

                    sz = tmp_path.stat().st_size
                    if sz < 10_000 or not has_hdf5_magic(tmp_path):
                        raise RuntimeError(f"invalid netcdf (size={sz}, hdf5={has_hdf5_magic(tmp_path)})")

                    tmp_path.replace(out_path)
                    created += 1

                except Exception as e:
                    print(f"  ❌ subset failed: {e}")
                    failed += 1
                    try:
                        if tmp_path.exists(): tmp_path.unlink()
                    except Exception:
                        pass
                    continue

                time.sleep(0.2)

        t += STEP

    print("")
    print(f"✅ Done. created={created}, skipped={skipped}, failed={failed}")
    if failed > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
