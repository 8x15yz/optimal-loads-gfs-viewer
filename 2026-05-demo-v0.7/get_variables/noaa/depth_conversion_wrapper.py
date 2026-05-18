#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# depth_conversion_wrapper.py
"""
DepthConversion CLI wrapper — S-111 / S-413 변환 + S3 업로드 + MongoDB 메타 등록

[사용 흐름]
  noaa_gfs_ingest.py 변수 루프 완료 후 호출
  ok, keys = run_conversion(mode=2, ...) → S-111 변환
  ok, keys = run_conversion(mode=3, ...) → S-413 변환

  실패 시: rollback_s3(s3_client, 누적된 keys) 호출

[환경변수]
  DEPTH_CONVERTER_PATH     DepthConversion 실행파일 경로
  DEPTH_CONVERTER_ENABLED  true/false (기본 true)
  S3_BUCKET
  AWS_REGION
  MONGO_S100_COL           s100assets_metadata (기본값)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

UTC = timezone.utc

# ── 상수 ──────────────────────────────────────────────────────────────────────
SOURCE  = "noaa"
MODEL   = "gfs"

# GFS 0.25° 전지구 격자
RES   = 0.25
N_LAT = 721
N_LON = 1440

PRODUCT_META = {
    2: {
        "product":   "s111",
        "tile_deg":  35.0,
        "variables": {
            "stored": ["surfaceCurrentSpeed", "surfaceCurrentDirection"],
            "source": ["UGRD", "VGRD"],
        },
    },
    3: {
        "product":   "s413",
        "tile_deg":  22.5,
        "variables": {
            "stored": ["significantWaveHeight", "peakWaveDirection",
                       "windDirection", "windSpeed"],
            "source": ["HTSGW", "DIRPW", "WDIR", "WIND"],
        },
    },
}

BUCKET = os.getenv("S3_BUCKET",  "optimal-loads")
REGION = os.getenv("AWS_REGION", "ap-northeast-2")

CONVERTER_PATH    = os.getenv("DEPTH_CONVERTER_PATH", "")
CONVERTER_ENABLED = os.getenv("DEPTH_CONVERTER_ENABLED", "true").strip().lower() == "true"


# ── 유틸 ──────────────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_gfs_steps(max_step: int = 384) -> list[int]:
    """noaa_gfs_ingest.py 와 동일한 스텝 목록 반환."""
    steps = list(range(0, min(144, max_step) + 1, 3))
    if max_step >= 150:
        steps += list(range(150, max_step + 1, 6))
    return sorted(set(steps))


# ── 타일 bbox 계산 ─────────────────────────────────────────────────────────────

def compute_tile_bbox(tile_idx: int, tile_deg: float) -> dict:
    """
    depth_convert.py compute_tiles() 와 동일한 로직.
    tile_idx: 1-based
    반환: {"idx": int, "north": float, "south": float, "west": float, "east": float}
    """
    n = round(tile_deg / RES)                    # 타일당 격자 수
    tiles_per_row = N_LON // n                   # 경도 방향 타일 수 (1행)

    # tile_idx → (row, col) 0-based
    row = (tile_idx - 1) // tiles_per_row
    col = (tile_idx - 1) %  tiles_per_row

    # depth_convert.py: 북 → 남 스캔, lat[0]=-90 기준
    ls = (N_LAT - 1) - row * n                   # 북쪽 위도 인덱스
    le = max(ls - n + 1, 0)                      # 남쪽 위도 인덱스
    js = col * n                                 # 서쪽 경도 인덱스
    je = min(js + n - 1, N_LON - 1)             # 동쪽 경도 인덱스

    north = round(-90.0 + ls * RES, 4)
    south = round(-90.0 + le * RES, 4)
    west  = round(-180.0 + js * RES, 4)
    east  = round(-180.0 + je * RES, 4)

    return {"idx": tile_idx, "north": north, "south": south,
            "west": west, "east": east}


# ── 타일 인덱스 파싱 ───────────────────────────────────────────────────────────

def parse_tile_idx(filename: str) -> Optional[int]:
    """
    '413KR00_20260303_12Z_001.h5' → 1
    '111KR00_20260303_12Z_128.h5' → 128
    """
    m = re.search(r"_(\d{3})\.h5$", filename)
    return int(m.group(1)) if m else None


# ── S3 키 ──────────────────────────────────────────────────────────────────────

def build_s100_s3_key(product: str, run_time_utc: datetime, filename: str) -> str:
    """
    s111/2026/03/03/12Z/111KR00_20260303_12Z_001.h5
    s413/2026/03/03/12Z/413KR00_20260303_12Z_001.h5
    """
    return (
        f"{product}/"
        f"{run_time_utc:%Y/%m/%d/%H}Z/"
        f"{filename}"
    )


# ── 메타도큐먼트 빌더 ──────────────────────────────────────────────────────────

def build_s100_doc(
    *,
    product: str,
    run_time_utc: datetime,
    tile_bbox: dict,
    steps_info: dict,
    variables: dict,
    s3_key: str,
    size_bytes: int,
) -> dict:
    tile_idx = tile_bbox["idx"]
    natural_key = (
        f"{product}|forecast"
        f"|run={iso_z(run_time_utc)}"
        f"|tile={tile_idx:03d}"
    )
    return {
        "natural_key":   natural_key,
        "product":       product,
        "source":        SOURCE,
        "model":         MODEL,
        "type":          "forecast",
        "run_time_utc":  iso_z(run_time_utc),
        "year":          run_time_utc.year,
        "month":         run_time_utc.month,
        "tile":          tile_bbox,
        "steps":         steps_info,
        "variables":     variables,
        "s3": {
            "bucket": BUCKET,
            "region": REGION,
            "key":    s3_key,
        },
        "size_bytes":    size_bytes,
        "format":        "hdf5",
        "content_type":  "application/x-hdf5",
        "created_at":    utc_now(),
    }


# ── 롤백 ──────────────────────────────────────────────────────────────────────

def rollback_s3(s3_client, keys: list[str]) -> None:
    """
    S3에 업로드된 s100 파일들을 삭제한다.
    변환 실패 시 이미 올라간 S-111/S-413 파일 정리용.
    """
    if not keys or s3_client is None:
        return
    print(f"[wrapper] 🔄 S3 롤백 시작 — {len(keys)}개 삭제")
    for key in keys:
        try:
            s3_client.delete_object(Bucket=BUCKET, Key=key)
            print(f"[wrapper] 🔄 롤백 삭제: {key}")
        except Exception as e:
            print(f"[wrapper] ⚠️ 롤백 실패: {key} — {e}")


# ── 핵심 함수 ──────────────────────────────────────────────────────────────────

def run_conversion(
    mode: int,
    run_time_utc: datetime,
    run_set_dir: Path,
    s3_client,
    s100_col,
) -> tuple[bool, list[str]]:
    """
    DepthConversion CLI 실행 → S3 업로드 → MongoDB upsert.

    Parameters
    ----------
    mode          : 2=S-111, 3=S-413
    run_time_utc  : 런타임 datetime (UTC)
    run_set_dir   : noaa/gfs/fc/YYYY/MM/DD/HHZ/ 경로
    s3_client     : boto3 S3 client
    s100_col      : pymongo s100assets_metadata 컬렉션 (None 이면 MongoDB 스킵)

    Returns
    -------
    tuple[bool, list[str]]
        bool      : 성공 여부
        list[str] : 업로드된 S3 키 목록 (실패 시에도 부분 업로드된 것 포함)
    """
    if not CONVERTER_ENABLED:
        print(f"[wrapper] DEPTH_CONVERTER_ENABLED=false → 변환 스킵 (mode={mode})")
        return True, []

    if not CONVERTER_PATH:
        print("[wrapper] ❌ DEPTH_CONVERTER_PATH 환경변수가 비어있습니다.")
        return False, []

    converter = Path(CONVERTER_PATH)
    if not converter.exists():
        print(f"[wrapper] ❌ 변환툴을 찾을 수 없습니다: {converter}")
        return False, []

    meta      = PRODUCT_META[mode]
    product   = meta["product"]
    tile_deg  = meta["tile_deg"]
    variables = meta["variables"]

    input_dir  = run_set_dir / "wave"
    output_dir = run_set_dir / product
    stage_dir  = run_set_dir / f"{product}_staging"
    monitor_log = run_set_dir / f"{product}_monitor.log"
    name       = run_time_utc.strftime("%Y%m%d_%HZ")

    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. CLI 실행 ────────────────────────────────────────────────────────────
    cmd = [
        str(converter),
        "--mode",         str(mode),
        "--input",        str(input_dir),
        "--name",         name,
        "--output",       str(output_dir),
        "--tile",         str(tile_deg),
        "--stage-output", str(stage_dir),
        "--monitor-log",  str(monitor_log),
        "--yes",
    ]

    print(f"[wrapper] ▶ {product.upper()} 변환 시작: {' '.join(cmd)}")
    start = utc_now()

    try:
        result = subprocess.run(
            cmd,
            capture_output=False,   # stdout/stderr 그대로 출력
            text=True,
            cwd=str(converter.parent),
        )
    except Exception as e:
        print(f"[wrapper] ❌ subprocess 실행 오류: {e}")
        return False, []

    elapsed = (utc_now() - start).total_seconds()
    print(f"[wrapper] 변환 완료 ({elapsed:.0f}s) returncode={result.returncode}")

    # ── 2. 성공 확인 (monitor log에서 Result: SUCCESS 확인) ───────────────────
    success = False
    if monitor_log.exists():
        log_text = monitor_log.read_text(encoding="utf-8", errors="ignore")
        if "Result" in log_text and "SUCCESS" in log_text:
            success = True
    # monitor log 없으면 returncode로 판단
    if not success and result.returncode == 0:
        h5_files = list(output_dir.glob("*.h5"))
        success = len(h5_files) > 0

    if not success:
        print(f"[wrapper] ❌ {product.upper()} 변환 실패 — S3/MongoDB 업로드 스킵")
        return False, []

    # ── 3. H5 파일 목록 수집 ──────────────────────────────────────────────────
    h5_files = sorted(output_dir.glob("*.h5"))
    if not h5_files:
        print(f"[wrapper] ❌ {output_dir} 에 .h5 파일이 없습니다.")
        return False, []

    print(f"[wrapper] {product.upper()} 타일 {len(h5_files)}개 처리 시작")

    # steps 정보 (모든 타일 공통)
    steps_list = build_gfs_steps()
    steps_info = {
        "count":            len(steps_list),
        "first_valid_time": iso_z(run_time_utc + timedelta(hours=steps_list[0])),
        "last_valid_time":  iso_z(run_time_utc + timedelta(hours=steps_list[-1])),
    }

    cnt_ok = cnt_fail = 0
    uploaded_keys: list[str] = []   # 롤백용 누적

    for h5_path in h5_files:
        tile_idx = parse_tile_idx(h5_path.name)
        if tile_idx is None:
            print(f"[wrapper] ⚠️ 타일 인덱스 파싱 실패: {h5_path.name} → 스킵")
            cnt_fail += 1
            continue

        tile_bbox  = compute_tile_bbox(tile_idx, tile_deg)
        s3_key     = build_s100_s3_key(product, run_time_utc, h5_path.name)
        size_bytes = h5_path.stat().st_size

        # ── S3 업로드 ──────────────────────────────────────────────────────────
        if s3_client is not None:
            try:
                s3_client.upload_file(
                    str(h5_path),
                    BUCKET,
                    s3_key,
                    ExtraArgs={"ContentType": "application/x-hdf5"},
                )
                uploaded_keys.append(s3_key)
            except Exception as e:
                print(f"[wrapper] ❌ S3 업로드 실패 tile={tile_idx:03d}: {e}")
                cnt_fail += 1
                continue

        # ── MongoDB upsert ─────────────────────────────────────────────────────
        doc = build_s100_doc(
            product=product,
            run_time_utc=run_time_utc,
            tile_bbox=tile_bbox,
            steps_info=steps_info,
            variables=variables,
            s3_key=s3_key,
            size_bytes=size_bytes,
        )

        if s100_col is not None:
            try:
                s100_col.update_one(
                    {"natural_key": doc["natural_key"]},
                    {"$setOnInsert": doc},
                    upsert=True,
                )
            except Exception as e:
                print(f"[wrapper] ❌ MongoDB upsert 실패 tile={tile_idx:03d}: {e}")
                cnt_fail += 1
                continue

        cnt_ok += 1
        print(f"[wrapper] ✅ tile={tile_idx:03d} s3={s3_key}")

    print(
        f"[wrapper] {product.upper()} 완료 — "
        f"성공: {cnt_ok}, 실패: {cnt_fail} / 전체: {len(h5_files)}"
    )

    # ── 4. staging 폴더 정리 ──────────────────────────────────────────────────
    if stage_dir.exists():
        try:
            shutil.rmtree(stage_dir)
        except Exception:
            pass

    return cnt_fail == 0, uploaded_keys