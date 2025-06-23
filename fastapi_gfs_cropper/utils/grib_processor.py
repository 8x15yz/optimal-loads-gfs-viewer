import xarray as xr
import numpy as np
import tempfile
import requests
import os
from pathlib import Path

def download_grib_file(url: str, filename="temp.grib2") -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    response = requests.get(url, headers=headers)
    
    if response.status_code != 200:
        raise Exception(f"Download failed: {response.status_code} - {response.text[:100]}")

    with open(filename, "wb") as f:
        f.write(response.content)
    
    return filename

import subprocess

import subprocess
import numpy as np
import os
import tempfile

def extract_variable(grib_path, var_name="WVDIR"):
    # exe_path = "wgrib2" ## ✅배포할 때
    exe_path =  "delete_/wgrib2.exe" ## ✅테스트할 때
    
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp_file:
        output_path = tmp_file.name 

    try:
        result = subprocess.run(
            [
                exe_path, grib_path,
                "-match", f":{var_name}:",
                "-no_header",
                "-text", output_path
            ],
            capture_output=True,
            text=True,
            check=True
        )
        
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"wgrib2 failed:\nSTDOUT: {e.stdout}\nSTDERR: {e.stderr}")

    try:
        with open(output_path, "r") as f:
            values = [float(x) for x in f.read().strip().split()]

        nlat = 721
        nlon = 1440

        if len(values) != nlat * nlon:
            raise ValueError(f"Expected {nlat * nlon} values, got {len(values)}.")

        data = np.array(values).reshape((nlat, nlon))
    except Exception as e:
        raise ValueError(f"Failed to parse wgrib2 output: {e}")
    finally:
        os.remove(output_path)

    lats = np.linspace(-90, 90, nlat)
    lons = np.linspace(0, 360 - 360/nlon, nlon)

    return data, lats, lons