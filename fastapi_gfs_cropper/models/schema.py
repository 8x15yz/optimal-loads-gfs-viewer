from pydantic import BaseModel, Field
from typing import List
from datetime import datetime

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

    
class WindRequest(BaseModel):
    date: str = Field(default_factory=lambda: datetime.utcnow().strftime("%Y%m%d"))
    hour: str = Field(default="06")