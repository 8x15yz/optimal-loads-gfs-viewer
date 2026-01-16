from datetime import datetime
import requests
import os

today = datetime.utcnow().strftime("%Y%m%d")
hour = "06"  # 00, 06, 12, 18 중에서 선택

filename = f"gfswave.t{hour}z.global.0p25.f000.grib2"
url = f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/v16.3/gfs.{today}/{hour}/wave/gridded/{filename}"
save_path = os.path.join("data", filename)

headers = {
    "User-Agent": "Mozilla/5.0"
}

print(f"📥 Downloading from: {url}")
response = requests.get(url, headers=headers, stream=True)
if response.status_code != 200:
    raise Exception(f"❌ Failed to download: {response.status_code} - {response.text[:100]}")

os.makedirs("data", exist_ok=True)
with open(save_path, "wb") as f:
    for chunk in response.iter_content(chunk_size=8192):
        f.write(chunk)

print(f"✅ Download complete: {save_path}")
