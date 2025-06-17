from pydantic import BaseModel
from typing import List

class GridPoint(BaseModel):
    lat: float
    lon: float
    value: float

class GridData(BaseModel):
    timestamp: str
    variable: str
    bbox: List[float]  # [min_lon, min_lat, max_lon, max_lat]
    resolution: List[float]
    data: List[GridPoint]