import requests
from datetime import datetime
import os



def generate_gfswave_url(date: str, forecast_hour: str = "00", fxx: str = "f000", variable: str = "WDIR"):
    """
    GFS Wave 데이터 URL 생성 함수
    """
    base_url = "https://nomads.ncep.noaa.gov/cgi-bin/filterwave_gfs_0p25.pl"
    file = f"gfswave.t{forecast_hour}z.global.0p25.{fxx}.grib2"
    dir_path = f"/gfswave.{date}/{forecast_hour}"

    params = {
        "file": file,
        f"var_{variable}": "on",
        "lev_surface": "on",
        "subregion": "",
        "leftlon": 0,
        "rightlon": 360,
        "toplat": 90,
        "bottomlat": -90,
        "dir": dir_path
    }

    query = "&".join([f"{k}={v}" for k, v in params.items()])
    return f"{base_url}?{query}"

def download_gfswave_file(url, save_path):
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    response = requests.get(url, headers=headers, stream=True)
    if response.status_code != 200:
        raise Exception(f"❌ Failed to download: {response.status_code} - {response.text[:100]}")
    
    with open(save_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    print(f"✅ Download complete: {save_path}")

