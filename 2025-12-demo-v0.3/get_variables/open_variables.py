import xarray as xr
from pathlib import Path

files = [
    "original_10u_20251217_18Z_step144.grib2",
    "original_10v_20251217_18Z_step144.grib2",
    "original_mwp_20251217_18Z_step144.grib2",
    "original_pp1d_20251217_18Z_step144.grib2",
    "original_swh_20251217_18Z_step144.grib2",
]

for f in files:
    print("=" * 80)
    print("FILE:", f)
    try:
        ds = xr.open_dataset(f, engine="cfgrib")
        print("data_vars:", list(ds.data_vars))
        print("coords:", list(ds.coords))
    except Exception as e:
        print("ERROR:", e)
