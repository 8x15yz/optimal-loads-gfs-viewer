# 🌊 Optimal-LOADS Ocean Gridded Data Demo  
**최적물류 해양 격자 데이터 데모 시스템**

---

> 🧭 **(2025-11) Demo v0.2 is now launching 🚀**  
> 데모 버전 0.2가 런칭되었습니다.  
> 본 버전은 CMEMS 파랑(`VHM0`, `VMDR`, `VTPK`) 및 GFS 바람(`eastward_wind`, `northward_wind`)  
> 데이터를 통합한 **FastAPI + S3 + MongoDB 기반 서비스**를 포함합니다.  

---

## 🧭 1. Overview / 개요  

**EN**  
This demo system automatically retrieves, processes, and serves global **ocean and atmospheric gridded datasets** (e.g., NOAA GFS, CMEMS).  
All datasets are stored in **AWS S3** and **MongoDB**, and exposed via a **FastAPI backend** for visualization and analysis.  
The pipeline handles **GRIB2 / NetCDF** data formats and returns **JSON-encoded grid arrays** for use in front-end applications.  

It is designed as a prototype for future **IHO S-100-based marine data services** (e.g., S-102 Bathymetry, S-111 Currents, S-104 Water Level).  

**KO**  
이 데모는 **전지구 해양·대기 격자 데이터(GFS, CMEMS 등)**를 자동으로 수집하고,  
S3 및 MongoDB에 저장한 후 **FastAPI 기반 API 서비스**를 통해  
격자 데이터를 조회·시각화할 수 있도록 구성된 프로토타입입니다.  

데이터는 GRIB2 및 NetCDF 형식을 자동 처리하며,  
JSON 인코딩된 격자 배열을 API로 제공합니다.  
향후 **S-100 기반 해양 데이터 서비스(S-102, S-111 등)**와 연계를 목표로 설계되었습니다.

---

## 🌐 2. Data Sources / 데이터 소스  

| Category / 구분 | Source / 소스 | Variables / 변수 | Interval / 주기 | Format / 형식 |
|-----------------|----------------|------------------|-----------------|----------------|
| Atmosphere / 대기 | NOAA GFS | `wind speed`, `u/v components` | 3h | GRIB2 |
| Ocean / 해양 | Copernicus Marine (CMEMS) | `VHM0`, `VMDR`, `VTPK` | 3h | NetCDF |
| Bathymetry / 수심 | GEBCO / IHO S-102 | `depth`, `uncertainty` | static | HDF5 |

> Reference / 참조: *IHO S-102 Bathymetric Surface Product Specification (Edition 2.0.0)*

---

## ⚙️ 3. Data Processing Pipeline / 데이터 처리 파이프라인  

**EN**
1. **Download** – Retrieve timeseries datasets using `copernicusmarine.subset()` or `wget`.  
   Supports dateline-crossing (±180°).  
2. **Preprocess** – Read with `xarray`, normalize variable names, units, coordinates.  
3. **Metadata Storage** – Store metadata to `MongoDB Atlas` (`assets_metadata`).  
4. **Upload** – Push to S3 via `boto3.upload_file()` with `ACL: private`.  
5. **Encoding** – Convert to `uint16` with `scale=100`, `nodata=65535`.  
   Index order: *row-major-bottom-up* (South → North).  

**KO**
1. **데이터 다운로드** – `copernicusmarine.subset()` 또는 `wget`을 이용해 시계열 데이터 획득.  
   Dateline(±180°) 구간 자동 분할 처리 지원.  
2. **전처리 및 검증** – `xarray`로 파일을 읽고 변수명·단위·좌표를 정규화.  
3. **메타데이터 저장** – MongoDB 컬렉션(`assets_metadata`)에 저장.  
4. **S3 업로드** – `boto3.upload_file()` 사용, `ACL=private`.  
5. **API 인코딩** – `uint16 + scale=100 + nodata=65535` 변환,  
   인덱스 순서는 남→북(`row-major-bottom-up`).

---

## 🧩 4. API Service / API 서비스  

### 🔹 Endpoints / 주요 엔드포인트  

| Path | Description / 설명 |
|------|-------------------|
| `/inventory` | View dataset metadata as HTML / 저장된 메타데이터 HTML 조회 |
| `/api/griddata` | Returns JSON-encoded grid values / S3에서 읽어 격자 데이터 반환 |

### 🔹 Example Request / 예시 요청
```bash
GET /api/griddata?variable=VHM0&forecast_datetime=2024-03-31T00:00:00Z&source=cmems&bbox=128,34,130,36
```

### 🔹 Example Response / 예시 응답
```json
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
```

---

## 🧮 5. Encoding & Coordinate Conventions / 인코딩 및 좌표 체계  

| Property / 항목 | Description / 설명 |
|------------------|--------------------|
| Value Encoding | `uint16 + scale = 100`, `nodata = 65535` |
| Index Order | `row-major-bottom-up (South → North)` |
| Lon/Lat Range | lon: −180 ~ 180°, lat: −90 ~ 90° |
| CRS | EPSG:4326 (WGS84) |

---

## 🚀 6. Deployment / 배포  

### 🔹 Docker Build & Run / 도커 빌드 및 실행
```bash
# Build image / 이미지 빌드
sudo docker build --no-cache -t fastapi-inventory:latest .

# Run container / 컨테이너 실행
sudo docker run -d --name fastapi-inventory   --restart=always   --env-file .env   -p 80:8000   fastapi-inventory:latest
```

### 🔹 .env Example / 환경변수 예시
```
APP_TITLE=Optimal-LOADS Inventory
MONGO_URI=mongodb+srv://...
MONGO_DB=optimal_loads
MONGO_COLL=assets_metadata
AWS_REGION=ap-northeast-2
S3_BUCKET=optimal-loads
```

---

## 🌊 7. Demo Examples / 데모 예시  

| Dataset | Variables / 변수 | Description / 설명 |
|----------|------------------|--------------------|
| **Waves (cmems/027)** | `VHM0`, `VMDR`, `VTPK` | Significant wave height, mean direction, peak period / 유의파고, 평균 파향, 피크주기 |
| **Wind (cmems/012)** | `eastward_wind`, `northward_wind` | 10 m wind components / 10m U/V 풍속 |
| **Bathymetry (S-102)** | `depth`, `uncertainty` | Static bathymetric grid (HDF5-based) / 수심 및 불확실도 (정적 HDF5 격자) |

---

## 📚 8. References / 참고 문헌  

- **IHO (2019).** *S-102 Bathymetric Surface Product Specification, Edition 2.0.0*  
- **Copernicus Marine User Manual**  
- **NOAA GFS GRIB2 Key Reference**  
- **ISO 19115-2:2009 / ISO 19123:2005** — Coverage schema & metadata standards  

---

## 🔭 9. Future Roadmap / 향후 계획  

**EN**
- Add `depth` parameter support (3D spatiotemporal grids)  
- Integrate S-102 bathymetry with real-time forecasts  
- Connect to **OpenBridge** viewer for UKC and S-100 interoperability  

**KO**
- `depth` 파라미터를 통한 3D 시공간 격자 지원  
- S-102 수심 격자와 실시간 해양예보 통합  
- **OpenBridge** 기반 뷰어 연동 및 UKC 시뮬레이션 확장  

---

## 🧑‍💻 Author / 작성자  

**BlueMap – Optimal-LOADS Project**  
Lead Developer: *Jay Kim (김현주)*  
📧 Contact: [info@bluemap.kr](mailto:info@bluemap.kr)

---

> © 2025 BlueMap / Optimal-LOADS Consortium.  
> Designed for research and demonstration purposes under the **Optimal Logistics Data Space (최적물류 데이터 스페이스)** project.
