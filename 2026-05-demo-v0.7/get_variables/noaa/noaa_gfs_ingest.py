#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# noaa_gfs_ingest.py
"""
NOAA GFS (Wave / gridded / 0p25) → (var별 GRIB2) + S3 + Mongo assets_metadata + directories
+ ✅ ingestion_control (pause/resume + 중복 실행 방지)
+ ✅ ingestion_runs (변수별 run 로그: 상세 카운터 + attempts 배열 + 에러 로그)

- step 규칙 (GFS-Wave): 000~119 hourly, 120~384 3-hourly
- 다운로드는 NOMADS g2sub Grib Filter(filter_gfswave.pl)로 var 단위 subset 다운로드
- ✅ 기준 코드(noaa_gfs_pipeline.py)와 로직 통일
- ✅ ECMWF 수준의 상세 실행 로그 추적
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import boto3
import requests
from pymongo import ASCENDING, MongoClient, ReturnDocument

from depth_conversion_wrapper import rollback_s3, run_conversion

UTC = timezone.utc

# ✅ stale/heartbeat 설정
STALE_MINUTES = int(os.getenv("INGESTION_STALE_MINUTES", "120"))

# --------------------- 모델/소스 고정 ---------------------
SOURCE = "noaa"
MODEL = "gfs"
RESOL = "0p25"
TYPE_CODE = "fc"
DATASET_CODE = "original"  # product_code와 동일
ASSET_TYPE = "forecast"

# GFS Wave g2sub endpoint
FILTER_ENDPOINT = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfswave.pl"

# 저장할 변수
PARAMS: Dict[str, Dict[str, str]] = {
    "UGRD": {"unit": "m/s", "name_en": "Eastward Current", "stream": "wave"},
    "VGRD": {"unit": "m/s", "name_en": "Northward Current", "stream": "wave"},
    "WIND": {"unit": "m/s", "name_en": "Wind Speed", "stream": "wave"},
    "WDIR": {"unit": "degree", "name_en": "Wind Direction", "stream": "wave"},
    "HTSGW": {"unit": "m", "name_en": "Significant Wave Height", "stream": "wave"},
    "PERPW": {"unit": "s", "name_en": "Peak Wave Period", "stream": "wave"},
    "DIRPW": {"unit": "degree", "name_en": "Peak Wave Direction", "stream": "wave"},
}

# wave 파일 도메인
WAVE_DOMAIN = os.getenv("NOAA_GFS_WAVE_DOMAIN", "global")

DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (gfs-ingest)"}

# --------------------- S3 설정 ---------------------
BUCKET = os.getenv("S3_BUCKET", "optimal-loads")
REGION = os.getenv("AWS_REGION", "ap-northeast-2")

# S3 경로 루트: noaa/gfs/fc/YYYY/MM/DD/HHZ/original/PARAM/
S3_PREFIX_ROOT = f"{SOURCE}/{MODEL}/{TYPE_CODE}"

DELETE_LOCAL_AFTER_UPLOAD = False  # ✅ wrapper 완료 후 일괄 삭제로 변경

# --------------------- Mongo 설정 ---------------------
MONGO_URI = os.getenv("MONGO_URI", "")
MONGO_DB = os.getenv("MONGO_DB", "optimal_loads")

MONGO_ASSETS_COL  = os.getenv("MONGO_COL",         "assets_metadata")
MONGO_DIR_COL     = os.getenv("MONGO_DIR_COL",      "directories")
MONGO_CONTROL_COL = os.getenv("MONGO_CONTROL_COL",  "ingestion_control")
MONGO_RUNS_COL    = os.getenv("MONGO_RUNS_COL",      "ingestion_runs")
MONGO_S100_COL    = os.getenv("MONGO_S100_COL",      "s100assets_metadata")  # ✅ 추가

CONTROL_DOC_ID = os.getenv("CONTROL_DOC_ID", "noaa_gfs_ingestion")

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_ROOT = SCRIPT_DIR / SOURCE / MODEL / TYPE_CODE  # 로컬 저장 루트

ROOT_DIR = f"{SOURCE}/{MODEL}/"  # directories 컬렉션용

# ✅ 에러 로그 최대 개수
MAX_ERRORS = 20

mongo: Optional[MongoClient] = None
assets_col  = None
dir_col     = None
control_col = None
runs_col    = None
s100_col    = None  # ✅ 추가

if MONGO_URI:
    mongo = MongoClient(MONGO_URI)
    assets_col  = mongo[MONGO_DB][MONGO_ASSETS_COL]
    dir_col     = mongo[MONGO_DB][MONGO_DIR_COL]
    control_col = mongo[MONGO_DB][MONGO_CONTROL_COL]
    runs_col    = mongo[MONGO_DB][MONGO_RUNS_COL]
    s100_col    = mongo[MONGO_DB][MONGO_S100_COL]  # ✅ 추가

    try:
        # assets_metadata
        assets_col.create_index([("natural_key", ASCENDING)], unique=True)
        assets_col.create_index([("valid_time_utc", ASCENDING)])
        assets_col.create_index([("run_time_utc", ASCENDING)])
        assets_col.create_index([("valid_key", ASCENDING)])
        assets_col.create_index([("inventory_directory", ASCENDING), ("name", ASCENDING)])

        # directories
        dir_col.create_index([("parent", ASCENDING), ("name", ASCENDING)])
        dir_col.create_index([("last_modified", ASCENDING)])

        # ingestion_runs
        runs_col.create_index([("run_time_utc", ASCENDING)])
        runs_col.create_index([("variable", ASCENDING)])
        runs_col.create_index([("stream", ASCENDING)])
        runs_col.create_index([("status", ASCENDING)])
        runs_col.create_index([("updated_at", ASCENDING)])

        # s100assets_metadata
        s100_col.create_index([("natural_key", ASCENDING)], unique=True)
        s100_col.create_index([
            ("product", ASCENDING),
            ("run_time_utc", ASCENDING),
            ("tile.idx", ASCENDING),
        ])
        s100_col.create_index([("product", ASCENDING), ("run_time_utc", ASCENDING)])
    except Exception:
        pass
else:
    print("⚠️ MONGO_URI 환경변수가 비어있어서 Mongo 기능은 비활성화됩니다.")


# --------------------- 공통 유틸 ---------------------
def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def ensure_dirs(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def build_gfs_steps(max_step: int = 384) -> List[int]:
    """
    ECMWF IFS 방식과 통일:
    - 0~144h: 3시간 간격
    - 150~384h: 6시간 간격
    """
    steps = list(range(0, min(144, max_step) + 1, 3))   # 0~144, 3h
    if max_step >= 150:
        steps += list(range(150, max_step + 1, 6))       # 150~384, 6h
    return sorted(set(steps))


def _cleanup_run_set_dir(run_set_dir: Path) -> None:
    """wave/, s111/, s413/ 하위 파일 전체 삭제."""
    for subdir in ("wave", "s111", "s413"):
        target = run_set_dir / subdir
        if target.exists():
            try:
                shutil.rmtree(target)
                print(f"🧹 삭제 완료: {target}")
            except Exception as e:
                print(f"⚠️ 삭제 실패: {target} — {e}")


# --------------------- 로컬 폴더 구조 (기준 코드 방식) ---------------------
def get_run_set_dir(run_dt: datetime) -> Path:
    """
    기준 코드와 동일:
    /noaa/gfs/fc/YYYY/MM/DD/HHZ/
    """
    yyyy = run_dt.strftime("%Y")
    mm = run_dt.strftime("%m")
    dd = run_dt.strftime("%d")
    hhZ = f"{run_dt:%H}Z"
    return DATA_ROOT / yyyy / mm / dd / hhZ


def get_out_path(run_set_dir: Path, stream: str, param: str, filename: str) -> Path:
    """
    기준 코드와 동일:
    run_set_dir / stream / param / filename
    예: /noaa/gfs/fc/2025/07/01/00Z/wave/UGRD/original_UGRD_20250701_00Z_step000.grib2
    """
    return run_set_dir / stream / param / filename


# --------------------- S3 경로 ---------------------
def build_s3_key(run_dt: datetime, product_code: str, param: str, filename: str) -> str:
    """
    S3 경로: noaa/gfs/fc/YYYY/MM/DD/HHZ/original/PARAM/filename
    """
    yyyy = run_dt.strftime("%Y")
    mm = run_dt.strftime("%m")
    dd = run_dt.strftime("%d")
    hhZ = f"{run_dt:%H}Z"
    return f"{S3_PREFIX_ROOT}/{yyyy}/{mm}/{dd}/{hhZ}/{product_code}/{param}/{filename}"


def upload_to_s3(s3, local_path: Path, s3_key: str) -> None:
    s3.upload_file(
        str(local_path),
        BUCKET,
        s3_key,
        ExtraArgs={"ContentType": "application/x-grib"},
    )


# --------------------- Mongo 키 (기준 코드와 동일) ---------------------
def build_keys(
    *,
    source: str,
    dataset_code: str,
    model: str,
    asset_type: str,
    stream: str,
    param: str,
    run_time: datetime,
    step: int,
    valid_time: datetime,
) -> Tuple[str, str]:
    natural_key = (
        f"{dataset_code}|{source}|{model}|{asset_type}|{stream}|{param}|"
        f"run={iso_z(run_time)}|step={step}"
    )
    valid_key = (
        f"{dataset_code}|{source}|{model}|{asset_type}|{stream}|{param}|"
        f"valid={iso_z(valid_time)}"
    )
    return natural_key, valid_key


# --------------------- directories 유틸 (기준 코드 방식) ---------------------
def _norm_dir(d: str) -> str:
    d = (d or "").strip().lstrip("/")
    if d and not d.endswith("/"):
        d += "/"
    return d


def _split_dir(d: str) -> Tuple[Optional[str], str]:
    d = _norm_dir(d)
    if not d:
        return None, ""
    parts = [p for p in d.strip("/").split("/") if p]
    if not parts:
        return None, ""
    name = parts[-1]
    if len(parts) == 1:
        return None, name
    parent = "/".join(parts[:-1]) + "/"
    return parent, name


def upsert_directories_from_doc(doc: Dict[str, Any]) -> None:
    """
    기준 코드와 동일한 directories 관리 로직
    """
    if dir_col is None:
        return

    inv_dir = _norm_dir(doc.get("inventory_directory", ""))
    
    if not inv_dir or not inv_dir.startswith(ROOT_DIR):
        return

    lm = doc.get("created_at")
    if not isinstance(lm, datetime):
        try:
            lm = parse_utc(str(lm))
        except Exception:
            lm = utc_now()

    now = utc_now()
    cur = inv_dir

    while cur and cur.startswith(ROOT_DIR):
        parent, name = _split_dir(cur)

        # 1) 현재 디렉토리 upsert
        dir_col.update_one(
            {"_id": cur},
            {
                "$setOnInsert": {
                    "dir": cur,
                    "parent": parent,
                    "name": name,
                    "children_dirs": [],
                    "created_at": now,
                },
                "$max": {"last_modified": lm},
                "$set": {"updated_at": now},
            },
            upsert=True,
        )

        # 2) 부모 upsert → children add
        if parent and parent.startswith(ROOT_DIR):
            p_parent, p_name = _split_dir(parent)

            dir_col.update_one(
                {"_id": parent},
                {
                    "$setOnInsert": {
                        "dir": parent,
                        "parent": p_parent,
                        "name": p_name,
                        "children_dirs": [],
                        "created_at": now,
                    },
                    "$max": {"last_modified": lm},
                    "$set": {"updated_at": now},
                },
                upsert=True,
            )

            child_name = name + "/"
            dir_col.update_one(
                {"_id": parent},
                {
                    "$addToSet": {"children_dirs": child_name},
                    "$max": {"last_modified": lm},
                    "$set": {"updated_at": now},
                },
            )

        cur = parent


# --------------------- 메타 doc 빌더 (기준 코드 방식) ---------------------
def build_raw_doc(
    *,
    source: str,
    dataset_code: str,
    model: str,
    resol: str,
    asset_type: str,
    type_code: str,
    stream: str,
    param: str,
    unit: str,
    name_en: str,
    run_time: datetime,
    step: int,
    valid_time: datetime,
    filename: str,
    local_path: Path,
    s3_key: Optional[str],
) -> dict:
    size_bytes = local_path.stat().st_size
    natural_key, valid_key = build_keys(
        source=source,
        dataset_code=dataset_code,
        model=model,
        asset_type=asset_type,
        stream=stream,
        param=param,
        run_time=run_time,
        step=step,
        valid_time=valid_time,
    )

    doc: dict = {
        "source": source,
        "dataset_code": dataset_code,

        "variable": param,
        "name_en": name_en,
        "unit": unit,

        "model": model,
        "type": asset_type,
        "stream": stream,

        "resolution": {"lon_deg": 0.25, "lat_deg": 0.25},

        "run_time_utc": iso_z(run_time),
        "step_hours": step,
        "valid_time_utc": iso_z(valid_time),

        "year": int(valid_time.strftime("%Y")),
        "month": int(valid_time.strftime("%m")),

        "name": filename,
        "format": "grib2",
        "content_type": "application/x-grib",
        "size_bytes": size_bytes,

        "created_at": utc_now(),

        "natural_key": natural_key,
        "valid_key": valid_key,

        # ✅ source_parameters 보강 (기준 코드 스타일)
        "source_parameters": {
            "noaa": {
                "type": type_code,
                "stream": stream,
                "time": f"{run_time:%H}",
                "step": step,
                "param": param,
                "resol": resol,
                "access": "nomads-http-filter",  # 다운로드 방식 명시
                "endpoint": FILTER_ENDPOINT,
                "domain": WAVE_DOMAIN,
                "filter_method": "g2sub",
            }
        },

        # ✅ inventory 구조 (기준 코드와 동일)
        "inventory_directory": f"{source}/{model}/{run_time:%Y/%Y-%m/%Y-%m-%d/%H}Z/{dataset_code}/{param}/",
        "inventory_name": f"{dataset_code}_{param}_{run_time:%Y%m%d_%H}Z_step{step:03}",
    }

    if s3_key:
        doc["s3"] = {"bucket": BUCKET, "region": REGION, "key": s3_key}

    return doc


def upsert_assets(doc: dict) -> None:
    if assets_col is None:
        return
    nk = doc["natural_key"]
    assets_col.update_one({"natural_key": nk}, {"$setOnInsert": doc}, upsert=True)
    upsert_directories_from_doc(doc)


# --------------------- ingestion_control (ECMWF 방식) ---------------------
def ensure_control_doc() -> None:
    """초기 1회 생성 보장(없으면 생성)."""
    if control_col is None:
        return
    now = utc_now()
    control_col.update_one(
        {"_id": CONTROL_DOC_ID},
        {"$setOnInsert": {
            "_id": CONTROL_DOC_ID,
            "enabled": True,
            "running": False,
            "status": "idle",
            "error": None,
            "created_at": now,
            "updated_at": now,

            # ✅ 추가 (운영 안정성)
            "last_started_at": None,
            "last_finished_at": None,
            "last_heartbeat_at": None,
        }},
        upsert=True,
    )


def try_begin_ingestion() -> bool:
    """
    실행 가능하면:
      enabled=true AND running=false 조건을 만족할 때만 running=true로 바꾸고 시작.
    실행 불가면 False.
    """
    if control_col is None:
        return True  # Mongo 비활성화면 제어도 못하니 그냥 진행

    ensure_control_doc()
    # ✅ stale 자동 정리 먼저
    recover_stale_running()
    now = utc_now()

    # 1) enabled 확인
    ctl = control_col.find_one({"_id": CONTROL_DOC_ID}, {"enabled": 1, "running": 1})
    if not ctl:
        return True
    if not ctl.get("enabled", True):
        control_col.update_one(
            {"_id": CONTROL_DOC_ID},
            {"$set": {"status": "paused", "updated_at": now}}
        )
        print("⏸️ ingestion_control.enabled=false → 이번 실행은 스킵합니다.")
        return False

    # 2) running=false일 때만 running=true로 바꾸기(중복 실행 방지)
    updated = control_col.find_one_and_update(
        {"_id": CONTROL_DOC_ID, "enabled": True, "running": False},
        {"$set": {
            "running": True,
            "status": "running",
            "last_started_at": now,
            "last_heartbeat_at": now,
            "updated_at": now,
            "error": None
        }},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        print("⏭️ 이미 수집이 진행 중(running=true) → 이번 실행은 스킵합니다.")
        return False

    return True


def end_ingestion(overall: str, err_msg: Optional[str] = None, summary: Optional[dict] = None) -> None:
    """성공/실패 상관없이 running=false로 되돌리고 종료 상태 기록."""
    if control_col is None:
        return
    now = utc_now()
    set_doc: dict = {
        "running": False,
        "status": "idle" if overall in ("success", "partial") else "error",
        "last_finished_at": now,
        "updated_at": now,
    }
    if err_msg:
        set_doc["error"] = err_msg[:1000]
    else:
        set_doc["error"] = None
    if summary:
        set_doc["last_run_summary"] = summary

    control_col.update_one({"_id": CONTROL_DOC_ID}, {"$set": set_doc})


def recover_stale_running() -> None:
    """
    running=true인데 last_heartbeat_at이 너무 오래됐으면
    비정상 종료로 간주하고 running=false로 정리.
    """
    if control_col is None:
        return

    now = utc_now()
    cutoff = now - timedelta(minutes=STALE_MINUTES)

    q = {
        "_id": CONTROL_DOC_ID,
        "running": True,
        "$or": [
            {"last_heartbeat_at": {"$lt": cutoff}},
            {"last_heartbeat_at": None, "last_started_at": {"$lt": cutoff}},
        ],
    }

    upd = {
        "$set": {
            "running": False,
            "status": "error",
            "error": f"stale reset: no heartbeat for {STALE_MINUTES} minutes",
            "last_finished_at": now,
            "updated_at": now,
        }
    }

    res = control_col.update_one(q, upd)
    if res.modified_count > 0:
        print(f"🧹 stale running detected → forced reset (>{STALE_MINUTES}m)")


def heartbeat(note: Optional[str] = None) -> None:
    """실행 중임을 표시(비정상 종료 감지용)."""
    if control_col is None:
        return
    now = utc_now()
    set_doc = {"last_heartbeat_at": now, "updated_at": now}
    if note:
        set_doc["heartbeat_note"] = note
    control_col.update_one(
        {"_id": CONTROL_DOC_ID, "running": True},
        {"$set": set_doc},
    )


# --------------------- ingestion_runs (ECMWF 방식 완전 적용) ---------------------
def run_doc_id(run_dt: datetime, stream: str, var: str) -> str:
    return f"{SOURCE}|{MODEL}|{TYPE_CODE}|{stream}|{var}|run={iso_z(run_dt)}"


def make_attempt_id(now: datetime) -> str:
    """고유 attempt ID 생성"""
    return now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def is_already_success(run_dt: datetime, stream: str, var: str) -> bool:
    """status가 success면 이미 완료로 판단"""
    if runs_col is None:
        return False
    _id = run_doc_id(run_dt, stream, var)
    doc = runs_col.find_one({"_id": _id}, {"status": 1})
    return bool(doc and doc.get("status") == "success")


def start_var_run_log(
    *,
    run_dt: datetime,
    stream: str,
    var: str,
    dataset_code: str,
    max_step: int,
    steps: list[int],
    trigger: str,
) -> tuple[str, str, datetime]:
    """
    변수별 run 로그 attempt 시작.
    반환: (_id, attempt_id, started_at)
    """
    if runs_col is None:
        now = utc_now()
        return run_doc_id(run_dt, stream, var), make_attempt_id(now), now

    now = utc_now()
    _id = run_doc_id(run_dt, stream, var)
    attempt_id = make_attempt_id(now)

    zero_counters = {"downloaded": 0, "existed": 0, "uploaded": 0, "mongo_written": 0, "failed": 0}

    attempt_doc = {
        "attempt_no": None,  # 나중에 attempts_cnt 기반으로 설정
        "attempt_id": attempt_id,
        "trigger": trigger,
        "status": "running",
        "started_at": now,
        "finished_at": None,
        "duration_sec": None,
        "counters": dict(zero_counters),
        "errors": [],
    }

    # attempts_cnt를 안전하게 증가
    doc_after = runs_col.find_one_and_update(
        {"_id": _id},
        {
            "$setOnInsert": {
                "_id": _id,
                "source": SOURCE,
                "model": MODEL,
                "type_code": TYPE_CODE,
                "dataset_code": dataset_code,
                "stream": stream,
                "variable": var,
                "run_time_utc": iso_z(run_dt),
                "created_at": now,
                "attempts": [],
                "counters_total": {"downloaded": 0, "existed": 0, "uploaded": 0, "mongo_written": 0, "failed": 0},
            },
            "$inc": {"attempts_cnt": 1},
            "$set": {
                "running": True,
                "status": "running",
                "trigger_last": trigger,
                "schedule": {"max_step": max_step, "steps_cnt": len(steps)},
                "started_at_last": now,
                "finished_at_last": None,
                "duration_sec_last": None,
                "counters_last": dict(zero_counters),
                "updated_at": now,
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    
    attempt_no = int((doc_after or {}).get("attempts_cnt") or 1)
    attempt_doc["attempt_no"] = attempt_no

    # attempt push
    runs_col.update_one(
        {"_id": _id},
        {
            "$push": {"attempts": attempt_doc},
            "$set": {"updated_at": now},
        },
    )

    return _id, attempt_id, now


def update_var_run_log(
    _id: str,
    attempt_id: str,
    *,
    counters: Optional[dict] = None,
    add_error: Optional[dict] = None,
) -> None:
    """
    ✅ 진행 중 카운터/에러 업데이트 (ECMWF 방식)
    - counters_last 갱신
    - attempts 내 attempt_id 매칭 원소도 갱신
    """
    if runs_col is None:
        return

    now = utc_now()
    upd: dict = {"$set": {"updated_at": now}}

    array_updates: dict = {}
    if counters is not None:
        upd["$set"]["counters_last"] = counters
        array_updates["attempts.$[a].counters"] = counters

    if add_error is not None:
        upd.setdefault("$push", {})["attempts.$[a].errors"] = {"$each": [add_error], "$slice": -MAX_ERRORS}

    if array_updates:
        upd["$set"].update(array_updates)

    runs_col.update_one(
        {"_id": _id},
        upd,
        array_filters=[{"a.attempt_id": attempt_id}],
    )


def finish_var_run_log(
    _id: str,
    attempt_id: str,
    *,
    started_at: datetime,
    status: str,
    counters: dict,
    errors: list[dict],
) -> None:
    """
    ✅ attempt 종료 + 문서 top-level last_* 갱신 (ECMWF 방식)
    - counters_total은 시도 누적
    """
    if runs_col is None:
        return

    now = utc_now()
    dur = int((now - started_at).total_seconds())

    upd = {
        "$set": {
            "running": False,
            "status": status,
            "updated_at": now,

            "finished_at_last": now,
            "duration_sec_last": dur,
            "counters_last": counters,

            # attempts 내 해당 attempt 마무리
            "attempts.$[a].status": status,
            "attempts.$[a].finished_at": now,
            "attempts.$[a].duration_sec": dur,
            "attempts.$[a].counters": counters,
            "attempts.$[a].errors": errors[-MAX_ERRORS:],
        },
        "$inc": {
            "counters_total.downloaded": counters.get("downloaded", 0),
            "counters_total.existed": counters.get("existed", 0),
            "counters_total.uploaded": counters.get("uploaded", 0),
            "counters_total.mongo_written": counters.get("mongo_written", 0),
            "counters_total.failed": counters.get("failed", 0),
        },
        "$setOnInsert": {
            "created_at": now,
        },
    }

    runs_col.update_one(
        {"_id": _id},
        upd,
        array_filters=[{"a.attempt_id": attempt_id}],
        upsert=True,
    )


# --------------------- NOAA 다운로드 ---------------------
def build_filter_url(run_dt: datetime, step: int, var: str) -> str:
    """
    NOMADS g2sub filter URL 생성
    """
    yyyymmdd = run_dt.strftime("%Y%m%d")
    hh = run_dt.strftime("%H")
    fff = f"{step:03d}"
    file_ = f"gfswave.t{hh}z.{WAVE_DOMAIN}.0p25.f{fff}.grib2"
    dir_ = f"/gfs.{yyyymmdd}/{hh}/wave/gridded"

    params = {
        "file": file_,
        f"var_{var}": "on",
        "lev_surface": "on",
        "subregion": "",
        "leftlon": "0",
        "rightlon": "360",
        "toplat": "90",
        "bottomlat": "-90",
        "dir": dir_,
    }
    req = requests.Request("GET", FILTER_ENDPOINT, params=params).prepare()
    return req.url


def download_var_step(
    *,
    run_dt: datetime,
    step: int,
    var: str,
    out_path: Path,
    timeout_sec: int,
    polite_wait_sec: float,
) -> None:
    """
    NOMADS HTTP로 변수별 GRIB2 다운로드
    """
    ensure_dirs(out_path)
    url = build_filter_url(run_dt, step, var)

    r = requests.get(url, headers=DEFAULT_HEADERS, stream=True, timeout=timeout_sec)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {url} :: {r.text[:120]}")

    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)

    # g2sub netiquette: 루프 요청시 대기 권장
    if polite_wait_sec > 0:
        time.sleep(polite_wait_sec)


# --------------------- main ---------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="NOAA GFS(WAVE) ingest → var별 GRIB2 저장 + S3 + Mongo metadata (ECMWF 수준 상세 로그)"
    )
    ap.add_argument("RUN_UTC", help="런 시각 예: 2026-02-02T06:00:00Z")
    ap.add_argument("--trigger", default="manual")
    ap.add_argument("--timeout_sec", type=int, default=60)
    ap.add_argument("--polite_wait_sec", type=float, default=1.0, help="requests 사이 대기(서버 보호)")
    ap.add_argument("--max_step", type=int, default=384)
    ap.add_argument("--skip_if_success", action="store_true", default=True)
    ap.add_argument("--no_delete_local", action="store_true", help="업로드 후 로컬 파일 삭제하지 않음")
    ap.add_argument("--no_s3",   action="store_true", help="S3 업로드 비활성화")
    ap.add_argument("--no_mongo", action="store_true", help="Mongo 메타 저장 비활성화")
    ap.add_argument("--no_convert", action="store_true", help="변환툴 실행 스킵 (원본 수집만)")  # ✅ 추가

    args = ap.parse_args()
    run_dt = parse_utc(args.RUN_UTC)

    if not MONGO_URI:
        print("⚠️ MONGO_URI 가 비어있습니다. Mongo 기능은 비활성화됩니다.")

    # ✅ Mongo 비활성화 처리
    global assets_col, dir_col, control_col, runs_col, s100_col
    if args.no_mongo:
        assets_col  = None
        dir_col     = None
        control_col = None
        runs_col    = None
        s100_col    = None  # ✅ 추가

    # ✅ ingestion_control로 실행 가능 여부 판정
    if not args.no_mongo:
        can_run = try_begin_ingestion()
        if not can_run:
            return

    # 요약 변수
    overall_err: Optional[str] = None
    overall_status: str = "success"
    summary_vars_total = len(PARAMS)
    summary_vars_success = 0
    summary_vars_failed = 0
    summary_run_time_utc = iso_z(run_dt)

    s3 = None
    if not args.no_s3:
        s3 = boto3.client("s3", region_name=REGION)

    try:
        steps = build_gfs_steps(max_step=args.max_step)
        
        print(f"▶ NOAA RUN : {iso_z(run_dt)}")
        print(f"▶ steps_cnt : {len(steps)} (max_step={args.max_step})")
        print(f"▶ Params    : {', '.join(PARAMS.keys())}")
        print(f"▶ DST S3    : {'OFF' if args.no_s3 else f's3://{BUCKET}/{S3_PREFIX_ROOT}/...'}")
        print(f"▶ Mongo     : {'OFF' if args.no_mongo else ('ON' if (MONGO_URI and assets_col is not None) else 'OFF')}")
        print(f"▶ Source    : NOMADS HTTP (g2sub filter)")
        print("")

        run_set_dir = get_run_set_dir(run_dt)

        # 변수별 실행
        for var, meta in PARAMS.items():
            unit = meta["unit"]
            name_en = meta["name_en"]
            stream = meta.get("stream", "wave")
            
            # ✅ 이미 성공한 변수-run이면 스킵
            if is_already_success(run_dt, stream, var):
                print(f"⏭️ SKIP (already success): {var} ({stream}) run={iso_z(run_dt)}")
                summary_vars_success += 1
                if not args.no_mongo:
                    heartbeat(f"skip var={var} already_success")
                continue

            # ✅ 변수 시작 heartbeat
            if not args.no_mongo:
                heartbeat(f"start var={var} run={iso_z(run_dt)}")

            # ✅ 변수별 로그 시작 (ECMWF 방식)
            log_id, attempt_id, started_at = start_var_run_log(
                run_dt=run_dt,
                stream=stream,
                var=var,
                dataset_code=DATASET_CODE,
                max_step=args.max_step,
                steps=steps,
                trigger=args.trigger,
            )

            counters = {"downloaded": 0, "existed": 0, "uploaded": 0, "mongo_written": 0, "failed": 0}
            errors: list[dict] = []

            # step × var 다운로드 + 업로드 + 메타
            for i, step in enumerate(steps):
                valid_time = run_dt + timedelta(hours=step)
                filename = f"{DATASET_CODE}_{var}_{run_dt:%Y%m%d_%H}Z_step{step:03}.grib2"

                # ✅ 로컬 저장 (기준 코드와 동일: stream 폴더 사용)
                local_path = get_out_path(run_set_dir, stream, var, filename)
                
                # 1) 다운로드
                if local_path.exists() and local_path.stat().st_size > 0:
                    counters["existed"] += 1
                else:
                    try:
                        download_var_step(
                            run_dt=run_dt,
                            step=step,
                            var=var,
                            out_path=local_path,
                            timeout_sec=args.timeout_sec,
                            polite_wait_sec=args.polite_wait_sec,
                        )
                        counters["downloaded"] += 1
                    except Exception as e:
                        counters["failed"] += 1
                        if len(errors) < MAX_ERRORS:
                            errors.append({"phase": "retrieve", "step": step, "message": str(e)[:500]})
                        # ✅ 진행 중 업데이트
                        update_var_run_log(log_id, attempt_id, counters=counters, add_error=errors[-1])
                        continue

                # 2) S3 업로드
                s3_key = None
                if s3 is not None:
                    try:
                        s3_key = build_s3_key(run_dt, DATASET_CODE, var, filename)
                        upload_to_s3(s3, local_path, s3_key)
                        counters["uploaded"] += 1
                    except Exception as e:
                        counters["failed"] += 1
                        if len(errors) < MAX_ERRORS:
                            errors.append({"phase": "s3", "step": step, "message": str(e)[:500]})
                        update_var_run_log(log_id, attempt_id, counters=counters, add_error=errors[-1])
                        continue

                # 3) 메타 생성 + upsert
                doc = build_raw_doc(
                    source=SOURCE,
                    dataset_code=DATASET_CODE,
                    model=MODEL,
                    resol=RESOL,
                    asset_type=ASSET_TYPE,
                    type_code=TYPE_CODE,
                    stream=stream,
                    param=var,
                    unit=unit,
                    name_en=name_en,
                    run_time=run_dt,
                    step=step,
                    valid_time=valid_time,
                    filename=filename,
                    local_path=local_path,
                    s3_key=s3_key,
                )
                
                if assets_col is not None:
                    try:
                        upsert_assets(doc)
                        counters["mongo_written"] += 1
                    except Exception as e:
                        counters["failed"] += 1
                        if len(errors) < MAX_ERRORS:
                            errors.append({"phase": "mongo", "step": step, "message": str(e)[:500]})
                        update_var_run_log(log_id, attempt_id, counters=counters, add_error=errors[-1])
                        continue

                # ✅ 주기적 카운터 업데이트 (10 step마다)
                if (i + 1) % 10 == 0:
                    update_var_run_log(log_id, attempt_id, counters=counters)

                # 4) 로컬 삭제
                if DELETE_LOCAL_AFTER_UPLOAD and s3_key and (not args.no_delete_local):
                    try:
                        local_path.unlink(missing_ok=True)
                    except Exception:
                        pass

            # ✅ 변수별 로그 종료 status 결정 (ECMWF 방식)
            succeeded_any = (counters["downloaded"] + counters["existed"] + counters["uploaded"] + counters["mongo_written"]) > 0
            if counters["failed"] == 0:
                var_status = "success"
            else:
                var_status = "partial" if succeeded_any else "failed"

            finish_var_run_log(
                log_id,
                attempt_id,
                started_at=started_at,
                status=var_status,
                counters=counters,
                errors=errors,
            )

            if not args.no_mongo:
                heartbeat(f"done var={var} status={var_status}")

            # 요약 집계
            if var_status == "success":
                summary_vars_success += 1
            else:
                summary_vars_failed += 1

            print(f"  ✅ VAR Done {var} ({stream}) status={var_status} counters={counters}")
            print("")

        # overall 결정 (원본 수집 기준)
        if summary_vars_failed == 0:
            overall_status = "success"
        elif summary_vars_success > 0:
            overall_status = "partial"
        else:
            overall_status = "failed"

        # ✅ conversion phase (원본 수집 성공/partial일 때만 시도)
        if not args.no_convert and overall_status in ("success", "partial"):
            if not args.no_mongo:
                heartbeat("conversion start")

            all_uploaded_keys: list[str] = []
            conv_failed = False

            for conv_mode in (2, 3):   # 2=S-111, 3=S-413
                product = "s111" if conv_mode == 2 else "s413"
                print(f"\n▶ {product.upper()} 변환 시작")

                ok, keys = run_conversion(
                    mode=conv_mode,
                    run_time_utc=run_dt,
                    run_set_dir=run_set_dir,
                    s3_client=s3,
                    s100_col=s100_col if not args.no_mongo else None,
                )
                all_uploaded_keys.extend(keys)

                if not ok:
                    print(f"❌ {product.upper()} 변환 실패 → 롤백 시작")
                    rollback_s3(s3, all_uploaded_keys)
                    conv_failed = True
                    overall_status = "failed"
                    break

                if not args.no_mongo:
                    heartbeat(f"conversion done mode={conv_mode}")

            if not conv_failed:
                print("\n✅ S-111 / S-413 변환 완료")

        print("✅ done")

    except Exception as e:
        overall_status = "failed"
        overall_err = str(e)
        raise

    finally:
        # ✅ 로컬 파일 일괄 삭제 (GRIB2 + H5)
        if not args.no_delete_local:
            _cleanup_run_set_dir(run_set_dir)

        if not args.no_mongo:
            summary = {
                "run_time_utc": summary_run_time_utc,
                "overall": overall_status,
                "vars_total": summary_vars_total,
                "vars_success": summary_vars_success,
                "vars_failed": summary_vars_failed,
            }
            end_ingestion(overall=overall_status, err_msg=overall_err, summary=summary)


if __name__ == "__main__":
    main()