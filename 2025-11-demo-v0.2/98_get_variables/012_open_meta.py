import xarray as xr

FILENAME = "cop_012_eastward_wind_20240805_00Z.nc"
ds = xr.open_dataset(FILENAME)
print(ds)