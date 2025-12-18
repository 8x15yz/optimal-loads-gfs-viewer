from datetime import datetime, timedelta, timezone
from ecmwf.opendata import Client
import os
import sys
import time
import argparse
import json
from pathlib import Path

# --------------------- 사용자 설정 ---------------------
SOURCE = "ecmwf"
MODEL  = "ifs"
RESOL  = "0p25"

PRODUCT_CODE = "original"
TYPE_CODE = "fc"  # forecast

# ✅ 변수별로 stream 지정
PARAMS = {
    "swh":  {"unit": "m",   "name_en": "Significant height of combined wind waves and swell", "stream": "wave"},
    "pp1d": {"unit": "s",   "name_en": "Peak wave period", "stream": "wave"},
    "mwp":  {"unit": "s",   "name_en": "Mean wave period", "stream": "wave"},  # period=seconds
    "10u":  {"unit": "m/s", "name_en": "10 metre U wind component", "stream": "oper"},
    "10v":  {"unit": "m/s", "name_en": "10 metre V wind component", "stream": "oper"},
}

DEFAULT_MAX_STEP = 360
UTC = timezone.utc

SCRIPT_DIR = Path(__file__).resolve().parent

# ✅ 저장 루트(원하면 바꿔도 됨)
DATA_ROOT = SCRIPT_DIR / "ecmwf" / MODEL / TYPE_CODE


# --------------------- 공통 유틸 ---------------------
def parse_utc(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)

def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

def iso_z_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------- step 스케줄 생성 (IFS 규칙) ---------------------
def build_ifs_steps(run_hour: int, max_step: int) -> list[int]:
    """
    IFS Open Data 규칙:
    - 00Z/12Z: 0~144 by 3, 150~360 by 6
    - 06Z/18Z: 0~144 by 3
    """
    steps: list[int] = []

    if run_hour in (0, 12):
        s1_end = min(144, max_step)
        steps.extend(range(0, s1_end + 1, 3))

        if max_step >= 150:
            s2_end = min(360, max_step)
            steps.extend(range(150, s2_end + 1, 6))

    elif run_hour in (6, 18):
        s1_end = min(144, max_step)
        steps.extend(range(0, s1_end + 1, 3))

    else:
        steps.extend(range(0, max_step + 1, 3))

    return sorted(set(steps))


# --------------------- ✅ 폴더 구조 생성 ---------------------
def get_run_set_dir(run_dt: datetime) -> Path:
    """
    추천안 A(런 중심) 구조:

    ecmwf/ifs/fc/YYYY/MM/DD/00Z/
      oper/10u/
      oper/10v/
      wave/swh/
      wave/pp1d/
      wave/mwp/
      metadata_log.jsonl

    """
    yyyy = run_dt.strftime("%Y")
    mm   = run_dt.strftime("%m")
    dd   = run_dt.strftime("%d")
    hhZ  = f"{run_dt:%H}Z"
    return DATA_ROOT / yyyy / mm / dd / hhZ


def get_out_path(run_set_dir: Path, stream: str, param: str, filename: str) -> Path:
    # run_set_dir/oper/10u/<file>
    return run_set_dir / stream / param / filename


def ensure_dirs(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


# --------------------- 메타데이터 파일 기록 ---------------------
def append_metadata_to_jsonl(
    metadata_log_path: Path,
    source: str,
    dataset_code: str,
    model: str,
    resol: str,
    type_code: str,
    run_time: datetime,
    step: int,
    stream: str,
    param: str,
    unit: str,
    name_en: str,
    valid_time: datetime,
    filename: str,
    filepath: Path,
):
    size_bytes = filepath.stat().st_size
    doc = {
        "source": source,
        "dataset_code": dataset_code,
        "model": model,
        "resol": resol,
        "type": type_code,
        "stream": stream,
        "run_time_utc": iso_z(run_time),
        "step_hours": step,
        "variable": param,
        "unit": unit,
        "name_en": name_en,
        "valid_time_utc": iso_z(valid_time),
        "name": filename,
        "local_path": str(filepath),
        "size_bytes": size_bytes,
        "created_at": iso_z_now(),
        "natural_key": f"{source}|{dataset_code}|{model}|{resol}|{type_code}|{stream}|{param}|run={iso_z(run_time)}|step={step}",
    }

    with open(metadata_log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(doc, ensure_ascii=False) + "\n")

    print(f"🧾 metadata logged → {metadata_log_path} | {doc['natural_key']}")


def main():
    ap = argparse.ArgumentParser(description="ECMWF IFS → run-based folder structure + GRIB2 + metadata jsonl")
    ap.add_argument("RUN_UTC", help="런 시각 예: 2025-12-16T00:00:00Z (15일치면 00Z/12Z 권장)")
    ap.add_argument("--max_step", type=int, default=DEFAULT_MAX_STEP, help="예측 step 최대 (기본: 360)")
    ap.add_argument("--sleep", type=float, default=0.2, help="요청 간 sleep(초)")
    args = ap.parse_args()

    run_dt = parse_utc(args.RUN_UTC)
    steps = build_ifs_steps(run_dt.hour, args.max_step)

    # ✅ 런 단위 폴더(=예측 세트) 생성
    run_set_dir = get_run_set_dir(run_dt)
    ensure_dirs(run_set_dir)

    # ✅ 메타데이터 로그도 런 폴더에 두기
    metadata_log_path = run_set_dir / "metadata_log.jsonl"

    print(f"▶ ECMWF RUN : {run_dt.isoformat()}")
    print(f"▶ run_hour  : {run_dt.hour}Z")
    print(f"▶ max_step  : {args.max_step}")
    print(f"▶ steps_cnt : {len(steps)}")
    print(f"▶ steps     : {steps[:25]}{' ...' if len(steps) > 25 else ''}")
    print("▶ Params    :", ", ".join(PARAMS.keys()))
    print(f"▶ Run set   : {run_set_dir}")
    print(f"▶ Meta log  : {metadata_log_path}")
    print("")

    client = Client(source="ecmwf", model=MODEL, resol=RESOL)

    downloaded = existed = meta_written = failed = 0

    for param, meta in PARAMS.items():
        unit = meta["unit"]
        name_en = meta["name_en"]
        stream = meta.get("stream", "oper")

        for step in steps:
            valid_time = run_dt + timedelta(hours=step)

            # 파일명은 기존 그대로 유지
            out = f"{PRODUCT_CODE}_{param}_{run_dt:%Y%m%d_%H}Z_step{step:03}.grib2"
            out_path = get_out_path(run_set_dir, stream, param, out)

            # 폴더 생성 (stream/param)
            ensure_dirs(out_path.parent)

            if out_path.exists():
                print(f"⏭️ exists locally: {out_path.relative_to(run_set_dir)}")
                existed += 1
            else:
                print(f"⏬ retrieve param={param} stream={stream} step={step} → {out_path.relative_to(run_set_dir)}")
                try:
                    client.retrieve(
                        date=run_dt.date(),     # ✅ 과거 런 고정
                        type=TYPE_CODE,         # fc
                        stream=stream,          # oper or wave
                        time=run_dt.hour,
                        step=step,
                        param=param,
                        target=str(out_path),
                    )
                    downloaded += 1
                except Exception as e:
                    print(f"  ❌ retrieve failed: {e}")
                    failed += 1
                    continue

            try:
                append_metadata_to_jsonl(
                    metadata_log_path=metadata_log_path,
                    source=SOURCE,
                    dataset_code=PRODUCT_CODE,
                    model=MODEL,
                    resol=RESOL,
                    type_code=TYPE_CODE,
                    run_time=run_dt,
                    step=step,
                    stream=stream,
                    param=param,
                    unit=unit,
                    name_en=name_en,
                    valid_time=valid_time,
                    filename=out,
                    filepath=out_path,
                )
                meta_written += 1
            except Exception as e:
                print(f"  ❌ metadata log failed: {e}")
                failed += 1

            time.sleep(args.sleep)

    print("")
    print(f"✅ Done. downloaded={downloaded}, existed={existed}, meta_written={meta_written}, failed={failed}")
    if failed > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
