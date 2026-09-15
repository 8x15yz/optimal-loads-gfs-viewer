from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Union

class InventoryItem(BaseModel):
    source: Optional[str] = None
    dataset_code: Optional[str] = None
    variable: Optional[str] = None
    unit: Optional[str] = Field(None, description="변수 단위 (예: m, degree, s, m s-1)")
    name_en: Optional[str] = Field(None, description="사람이 읽는 변수명(영문)")
    standard_name: Optional[str] = Field(None, description="CF 표준 변수명(가능한 경우)")
    year: Optional[int] = None
    month: Optional[int] = None
    valid_time_utc: Optional[str] = None
    name: Optional[str] = None
    size_bytes: Optional[int] = None
    s3: Optional[dict] = None     # { bucket, region, key }


class ValueEncoding(BaseModel):
    type: str = Field("float32", description="Value encoding type: float32 (default) or uint16")
    scale: Optional[float] = Field(None, description="Scale factor (uint16 only)")
    offset: Optional[float] = Field(None, description="Offset (uint16 only)")
    nodata: Optional[Union[int, float]] = Field(None, description="No-data marker (int for uint16; null for float32)")


class GridDataResponse(BaseModel):
    timestamp: str = Field(..., description="Valid time of the data (ISO 8601, UTC) = run_time_utc + step_hours")
    variable: str = Field(..., description="Requested variable code")
    unit: Optional[str] = Field(None, description="Unit of the variable (e.g. m, degree, s, m/s)")
    name_en: Optional[str] = Field(None, description="Full English name of the variable")
    standard_name: Optional[str] = Field(None, description="CF standard name; null if not available")
    bbox: List[float] = Field(..., description="[minLon, minLat, maxLon, maxLat] bounding box (degree)")
    resolution: List[float] = Field(..., description="[longitude step, latitude step] (degree)")
    shape: List[int] = Field(..., description="[number of columns (lon), number of rows (lat)]")
    indexOrder: str = Field("row-major-bottom-up", description="Ordering of the data array")
    valueEncoding: ValueEncoding = Field(..., description="How values in `data` are encoded")
    data: List[Optional[Union[int, float]]] = Field(..., description="Data array (flat); null = no data")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "timestamp": "2026-03-03T06:00:00Z",
            "variable": "DIRPW",
            "unit": "degree",
            "name_en": "Peak Wave Direction",
            "standard_name": None,
            "bbox": [128.75, 34.75, 129.25, 35.25],
            "resolution": [0.25, 0.25],
            "shape": [3, 3],
            "indexOrder": "row-major-bottom-up",
            "valueEncoding": {"type": "float32", "scale": 1, "offset": 0, "nodata": None},
            "data": [62.9000015258789, 48.8400001525879, 40.9300003051758,
                     21.1100006103516, 73.5400009155273, 50.5200004577637,
                     None, None, 53.060001373291],
        }
    })


class ErrorResponse(BaseModel):
    error: str