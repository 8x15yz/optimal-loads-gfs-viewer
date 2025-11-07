🌊 Optimal-LOADS Ocean Gridded Data Demo
1. Overview

This demo system automatically retrieves, processes, and serves global ocean and atmospheric gridded datasets (e.g., NOAA GFS, CMEMS).
All datasets are stored in AWS S3 and MongoDB, and exposed through a FastAPI backend for visualization and analysis.

The system automatically handles GRIB2 and NetCDF formats and returns JSON-encoded grid arrays.
It is designed as a prototype for future S-100-based marine data services such as S-102 (Bathymetry), S-111 (Currents), and S-104 (Water Level).

2. Data Sources
Category	Source	Variables	Interval	Format
Atmosphere	NOAA GFS	wind speed, u/v components	3 h	GRIB2
Ocean	Copernicus Marine (CMEMS)	VHM0, VMDR, VTPK	3 h	NetCDF
Bathymetry	GEBCO / IHO S-102	depth, uncertainty	static	HDF5

Reference: IHO S-102 Bathymetric Surface Product Specification (Edition 2.0.0)

3. Data Processing Pipeline

Data Download

Retrieve timeseries datasets using copernicusmarine.subset() or wget.

Supports automatic handling for dateline-crossing regions (±180°).

Preprocessing and Validation

Load datasets with xarray (using netcdf4 or h5netcdf engine).

Normalize variable names, units, and coordinates.

Metadata Storage

Stored in MongoDB Atlas collection assets_metadata.

Example fields: { variable, valid_time_utc, source, bbox, s3.key, size_bytes }.

S3 Upload

Upload via boto3.upload_file()

ACL: private, ContentType: application/x-netcdf.

API Encoding

Values encoded as uint16 with scale=100, nodata=65535.

Index order: row-major-bottom-up (South → North).

4. API Service Structure
🔹 Endpoints
Path	Description
/inventory	Displays dataset metadata in HTML.
/api/griddata	Reads NetCDF/GRIB2 from S3 and returns JSON-encoded grid values.
🔹 Example Request
GET /api/griddata?variable=VHM0&forecast_datetime=2024-03-31T00:00:00Z&source=cmems&bbox=128,34,130,36

🔹 Example Response (GridDataResponse)
{
  "timestamp": "2024-03-31T00:00:00Z",
  "variable": "VHM0",
  "bbox": [128.0, 34.0, 130.0, 36.0],
  "encoding": {
    "type": "uint16",
    "scale": 100,
    "nodata": 65535
  },
  "data": [[45, 52, 60, ...], ...]
}

5. Encoding and Coordinate Conventions
Property	Description
Value Encoding	uint16 + scale = 100, nodata = 65535
Index Order	row-major-bottom-up (South → North)
Lon/Lat Range	lon: −180 ~ 180°, lat: −90 ~ 90°
CRS	EPSG 4326 (WGS-84)
6. Deployment
🔹 Docker Build & Run
# Build the image
sudo docker build --no-cache -t fastapi-inventory:latest .

# Run the container
sudo docker run -d --name fastapi-inventory \
  --restart=always \
  --env-file .env \
  -p 80:8000 \
  fastapi-inventory:latest

🔹 Environment Variables (.env example)
APP_TITLE=Optimal-LOADS Inventory
MONGO_URI=mongodb+srv://...
MONGO_DB=optimal_loads
MONGO_COLL=assets_metadata
AWS_REGION=ap-northeast-2
S3_BUCKET=optimal-loads

7. Demo Examples
Dataset	Variables	Description
Waves (cmems/027)	VHM0, VMDR, VTPK	Significant wave height, mean direction, peak period
Wind (cmems/012)	eastward_wind, northward_wind	10 m wind components
Bathymetry (S-102)	depth, uncertainty	Static bathymetric grid (HDF5-based; integration planned)
8. References

IHO (2019). S-102 Bathymetric Surface Product Specification, Edition 2.0.0

Copernicus Marine User Manual

NOAA Global Forecast System (GFS) GRIB2 Key Reference

ISO 19115-2:2009 / ISO 19123:2005 — Coverage schema and metadata standards

9. Future Roadmap

Add depth parameter support (3D spatiotemporal grids).

Integrate S-102 bathymetry with real-time forecast data.

Connect to OpenBridge viewer for UKC simulation and S-100 interoperability testing.


🌊 Optimal-LOADS 해양 격자 데이터 데모
1. 개요

이 데모는 **전지구 해양 기상 데이터(GFS, CMEMS 등)**를 자동으로 수집하고,
S3 및 MongoDB에 저장한 뒤 FastAPI 기반 API 서비스를 통해
격자 데이터를 조회·시각화할 수 있도록 구성된 프로토타입입니다.

데이터는 GRIB2 및 NetCDF 형식을 자동 처리하며,
JSON 인코딩된 격자 배열을 API로 제공합니다.
이 시스템은 향후 **S-100 기반 해양 데이터 서비스(S-102, S-111 등)**와의 연계를 위해 설계되었습니다.

2. 데이터 소스
구분	소스	변수 예시	주기	형식
대기	NOAA GFS	wind speed, u/v component	3시간	GRIB2
해양	Copernicus Marine (CMEMS)	VHM0, VMDR, VTPK	3시간	NetCDF
수심	GEBCO / S-102	depth, uncertainty	정적	HDF5

※ S-102 표준: IHO Bathymetric Surface Product Specification (Edition 2.0.0) 참조

3. 데이터 처리 파이프라인

데이터 다운로드

copernicusmarine.subset() 또는 wget을 통해 시계열 데이터 획득

Dateline(−180/180) 구간 자동 분할 다운로드 지원

전처리 및 검증

xarray로 데이터 구조 읽기 (엔진: netcdf4, h5netcdf)

변수 이름, 단위, 시간 좌표 등 정규화

메타데이터 저장

MongoDB Atlas 컬렉션: assets_metadata

필드 예시: { variable, valid_time_utc, source, bbox, s3.key, size_bytes }

S3 업로드

boto3.upload_file() 사용

ACL: private, ContentType: application/x-netcdf

API 응답 변환

uint16 + scale=100 + nodata=65535 방식으로 압축 인코딩

인덱스 순서: row-major-bottom-up (남→북)

4. API 서비스 구조
🔹 주요 엔드포인트
경로	설명
/inventory	저장된 데이터 메타정보를 HTML로 표시
/api/griddata	S3에서 NetCDF/GRIB2 파일을 읽고 격자 값을 JSON으로 반환
🔹 예시 요청
GET /api/griddata?variable=VHM0&forecast_datetime=2024-03-31T00:00:00Z&source=cmems&bbox=128,34,130,36

🔹 응답 예시 (GridDataResponse)
{
  "timestamp": "2024-03-31T00:00:00Z",
  "variable": "VHM0",
  "bbox": [128.0, 34.0, 130.0, 36.0],
  "encoding": {
    "type": "uint16",
    "scale": 100,
    "nodata": 65535
  },
  "data": [ [45, 52, 60, ...], ... ]
}

5. 인코딩 및 좌표 체계
항목	값
값 인코딩	uint16 + scale=100, nodata=65535
인덱스 순서	row-major-bottom-up (남→북)
위경도 범위	lon: −180 ~ 180°, lat: −90 ~ 90°
좌표계	EPSG:4326 (WGS84)
6. 배포 및 실행
🔹 Docker 빌드 및 구동
# 이미지 재빌드
sudo docker build --no-cache -t fastapi-inventory:latest .

# 컨테이너 실행
sudo docker run -d --name fastapi-inventory \
  --restart=always \
  --env-file .env \
  -p 80:8000 \
  fastapi-inventory:latest

🔹 환경변수 (.env 예시)
APP_TITLE=Optimal-LOADS Inventory
MONGO_URI=mongodb+srv://...
MONGO_DB=optimal_loads
MONGO_COLL=assets_metadata
AWS_REGION=ap-northeast-2
S3_BUCKET=optimal-loads

7. 데모 예시

Wave (cmems/027): 유의파고 VHM0, 평균파향 VMDR, 피크주기 VTPK

Wind (cmems/012): eastward_wind, northward_wind

Bathymetry (S-102): 수심 그리드 및 불확실도 (HDF5 기반, 추후 연계 예정)

8. 참고 문헌

IHO (2019). S-102 Bathymetric Surface Product Specification, Edition 2.0.0

Copernicus Marine User Manual

NOAA Global Forecast System (GFS) GRIB2 Key Reference

ISO 19115-2:2009 / ISO 19123:2005 (Coverage Schema)

9. 향후 계획

depth 파라미터 지원 (3D 시공간 격자)

S-102 수심 격자와 실시간 해양예보 융합

OpenBridge 기반 뷰어 연동 및 UKC 시뮬레이션 지원