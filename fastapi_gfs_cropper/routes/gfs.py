from fastapi import APIRouter
from utils.grib_processor import extract_variable
from utils.s102_converter import crop_and_convert
from db.mongo import collection
from models.schema import GridData, WindRequest
from datetime import datetime, timedelta
import os
import requests


router = APIRouter()

@router.post("/wind-auto")
def fetch_and_store_gfs_auto(request: WindRequest):
    today = request.date
    hour = request.hour

    filename = f"gfswave.t{hour}z.global.0p25.f000.grib2"
    url = f"https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/v16.3/gfs.{today}/{hour}/wave/gridded/{filename}"

    save_dir = "data"
    os.makedirs(save_dir, exist_ok=True)
    grib_path = os.path.join(save_dir, filename)

    # ✅ 다운로드
    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, stream=True)
        if response.status_code != 200:
            return {"error": f"Download failed: {response.status_code} - {response.text[:100]}"}

        with open(grib_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        # ✅ 다운로드가 정상적으로 끝난 후 로그 출력
        print(f"✅ GRIB file downloaded: {grib_path}")

    except Exception as e:
        return {"error": f"Download error: {str(e)}"}

    # ✅ 파일 크기 확인
    if not os.path.exists(grib_path) or os.path.getsize(grib_path) < 10000:
        return {"error": "Downloaded file too small or missing, likely invalid."}

    # ✅ 변수 추출 (다운로드 끝난 후 실행)
    print("?")
    try:
        data, lat, lon = extract_variable(grib_path, var_name="WVDIR")
        if data is None:
            return {"error": "WVDIR variable not found in GRIB file."}
    except Exception as e:
        return {"error": f"Failed to parse GRIB file: {str(e)}"}

    # ✅ 위경도 영역 잘라내기
    bbox = [128.3, 34.35, 129.8, 35.85]


    try:
        cropped_data, resolution = crop_and_convert(data, lat, lon, bbox)
        print(f"✅ Cropped data resolution: {resolution}")
        print(f"✅ Cropped data shape: {cropped_data}")
    except Exception as e:
        return {"error": f"Cropping failed: {str(e)}"}

    # 1. "20250617" → datetime 객체로 변환
    parsed_date = datetime.strptime(request.date, "%Y%m%d")

    # 2. 원하는 포맷의 ISO 8601 문자열로 조합
    timestamp = f"{parsed_date.strftime('%Y-%m-%d')}T{request.hour}:00:00Z"


    # ✅ MongoDB 저장
    try:
        grid_doc = GridData(
            timestamp=timestamp,
            variable="windDirection",
            bbox=bbox,
            resolution=resolution,
            data=cropped_data
        )
        collection.insert_one(grid_doc.dict())
    except Exception as e:
        return {"error": f"MongoDB insert failed: {str(e)}"}

    return {
        "message": "✅ Wind direction data saved successfully",
        "data_count": len(cropped_data),
        "resolution": resolution,
        "source_url": url
    }


from fastapi import APIRouter, Query
from typing import List, Optional
@router.get("/wind-direction", response_model=List[GridData])
def get_wind_direction_data(date: Optional[str] = Query(None, description="date (YYYY-MM-DD format)")):
    
    """
    Retrieves wind direction data from MongoDB.
    """
    query = {"variable": "windDirection"}

    if date:
        try:
            dt_start = datetime.strptime(date, "%Y-%m-%d")
            dt_end = dt_start.replace(hour=23, minute=59, second=59)

            dt_start_str = dt_start.strftime("%Y-%m-%dT00:00:00Z")
            dt_end_str = dt_end.strftime("%Y-%m-%dT23:59:59Z")

            query["timestamp"] = {"$gte": dt_start_str, "$lte": dt_end_str}
        except ValueError:
            return {"error": "날짜 형식은 YYYY-MM-DD로 입력하세요."}

    try:
        results = list(collection.find(query))
        for r in results:
            r["_id"] = str(r["_id"])
        return results
    except Exception as e:
        return {"error": f"MongoDB 조회 실패: {str(e)}"}