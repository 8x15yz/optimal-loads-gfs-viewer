#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ecmwf_gap_fill.py
"""
ECMWF IFS 회차/변수/스텝 단위 누락 점검 및 채우기.

판단 기준은 ingestion_runs.status 가 아니라 "실제 데이터"임.
  - S3 원본 파일  : ecmwf/ifs/fc/YYYY/MM/DD/HHZ/original/<var>/original_<var>_..._stepNNN.grib2
  - Mongo 메타    : assets_metadata (dataset_code=original, type=forecast)
  - 파생 메타     : assets_metadata (dataset_code=computed, wind_speed_10m / wind_dir_10m)
  - all-steps     : assets_metadata (dataset_code=original-all-steps) 의 step_count

스텝 판정
  - S3 파일 없음 / Mongo 문서 없음 / Mongo 문서에 s3.key 없음  → 누락
  - S3 파일 크기가 같은 변수 중앙값의 50% 미만, 또는 10KB 미만   → 손상 의심
  - S3 크기와 Mongo size_bytes 불일치                            → 손상 의심
  → 누락·손상 스텝만 원천(기본 aws 미러)에서 다시 받아 S3 업로드 + Mongo 갱신.

ingest 스크립트와 차이
  - ingest 는 status=success 가 아닌 변수를 처음 스텝부터 전부 다시 받음
    (업로드 후 로컬 파일을 지우기 때문에 existed 로 건너뛰지 못함).
  - 이 스크립트는 빠진 스텝만 받음.
  - 원천 클라이언트 재시도 횟수/간격을 줄여 503 에서 오래 묶이지 않게 함.

사용 (2026-05-demo-v0.7 디렉터리에서, .env_runtime 로드 후)
  점검만:   python ecmwf_gap_fill.py --start_utc 2026-10-16T00:00:00Z --end_utc 2026-10-20T18:00:00Z
  채우기:   python ecmwf_gap_fill.py --run 2026-10-18T12:00:00Z --fill --rebuild_all_steps
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import re
import shutil
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent


def _load_module(name: str, filename: str):
    path = HERE / filename
    if not path.exists():
        raise SystemExit(f"❌ {path} 가 없음 (이 스크립트는 get_variables/emcwf/ 안에 두어야 함)")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ingest 모듈의 설정/키 규칙/메타 빌더/로그 함수를 그대로 재사용
ing = _load_module("ecmwf_ifs_ingest_for_gapfill", "ecmwf_ifs_ingest.py")

STEP_RE = re.compile(r"_step(\d{3})\.grib2$")
DERIVED = list(ing.DERIVED_VARS.keys())  # wind_speed_10m, wind_dir_10m
DS_ORIG = "original"
DS_ALL = "original-all-steps"
DS_DERIVED = "computed"


# --------------------------------------------------------------------------- 유틸
def compress_steps(missing: List[int], expected: List[int]) -> str:
    """expected 순서 기준으로 연속 구간을 a-b 로 묶음."""
    if not missing:
        return ""
    idx = {s: i for i, s in enumerate(expected)}
    ms = sorted(missing, key=lambda s: idx.get(s, 10**6))
    out, start, prev = [], ms[0], ms[0]
    for s in ms[1:]:
        if idx.get(s, -9) == idx.get(prev, -99) + 1:
            prev = s
            continue
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = s
    out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ",".join(out)


def expected_steps(run_dt: datetime, max_long: int, max_short: int) -> List[int]:
    max_step = max_long if run_dt.hour in (0, 12) else max_short
    return ing.build_ifs_steps(run_dt.hour, max_step)


def run_list(args) -> List[datetime]:
    if args.run:
        return [ing.parse_utc(args.run)]
    start, end = ing.parse_utc(args.start_utc), ing.parse_utc(args.end_utc)
    out, t = [], start
    while t <= end:
        out.append(t)
        t += timedelta(hours=6)
    return out


def s3_prefix(run_dt: datetime, param: str) -> str:
    return f"{ing.S3_PREFIX_ROOT}/{run_dt:%Y}/{run_dt:%m}/{run_dt:%d}/{run_dt:%H}Z/{DS_ORIG}/{param}/"


def is_valid_grib(path: Path) -> bool:
    try:
        size = path.stat().st_size
        if size < 100:
            return False
        with open(path, "rb") as f:
            head = f.read(4)
            f.seek(-4, 2)
            tail = f.read(4)
        return head == b"GRIB" and tail == b"7777"
    except Exception:
        return False


# --------------------------------------------------------------------------- 점검
def scan_var(s3, run_dt: datetime, param: str, exp: List[int]) -> dict:
    run_iso = ing.iso_z(run_dt)
    stream = ing.PARAMS[param]["stream"]

    # S3
    s3_files: Dict[int, int] = {}
    pag = s3.get_paginator("list_objects_v2")
    for page in pag.paginate(Bucket=ing.BUCKET, Prefix=s3_prefix(run_dt, param)):
        for obj in page.get("Contents", []) or []:
            m = STEP_RE.search(obj["Key"])
            if m:
                s3_files[int(m.group(1))] = int(obj["Size"])

    # Mongo 원본 메타
    mongo_docs: Dict[int, dict] = {}
    for d in ing.assets_col.find(
        {"source": ing.SOURCE, "model": ing.MODEL, "dataset_code": DS_ORIG,
         "type": ing.ASSET_TYPE, "variable": param, "run_time_utc": run_iso},
        {"step_hours": 1, "size_bytes": 1, "s3.key": 1},
    ):
        mongo_docs[int(d.get("step_hours", -1))] = d

    # all-steps
    all_doc = ing.assets_col.find_one(
        {"source": ing.SOURCE, "model": ing.MODEL, "dataset_code": DS_ALL,
         "variable": param, "run_time_utc": run_iso},
        {"step_count": 1, "steps_included": 1, "s3.key": 1},
        sort=[("created_at", -1)],
    )

    # ingestion_runs
    rdoc = ing.runs_col.find_one({"_id": ing.run_doc_id(run_dt, stream, param)}, {"status": 1})

    return {
        "param": param, "stream": stream, "expected": exp,
        "s3": s3_files, "mongo": mongo_docs, "all_doc": all_doc,
        "runs_status": (rdoc or {}).get("status", "-"),
    }


def judge_var(info: dict, median: Optional[float], ratio: float, min_bytes: int) -> dict:
    exp = info["expected"]
    missing, suspect = [], []
    for st in exp:
        size = info["s3"].get(st)
        doc = info["mongo"].get(st)
        if size is None or doc is None or not (doc.get("s3") or {}).get("key"):
            missing.append(st)
            continue
        floor = min_bytes
        if median:
            floor = max(floor, int(median * ratio))
        msize = doc.get("size_bytes")
        if size < floor or (isinstance(msize, int) and msize != size):
            suspect.append(st)
    info["missing"] = missing
    info["suspect"] = suspect
    info["todo"] = sorted(set(missing) | set(suspect))
    info["ok_cnt"] = len(exp) - len(info["todo"])
    return info


def scan_derived(run_dt: datetime) -> Dict[str, set]:
    run_iso = ing.iso_z(run_dt)
    out = {}
    for dv in DERIVED:
        out[dv] = {
            int(d["step_hours"])
            for d in ing.assets_col.find(
                {"source": ing.SOURCE, "model": ing.MODEL, "dataset_code": DS_DERIVED,
                 "variable": dv, "run_time_utc": run_iso},
                {"step_hours": 1},
            )
        }
    return out


def scan(s3, runs: List[datetime], params: List[str], args) -> Dict[datetime, dict]:
    raw: Dict[datetime, Dict[str, dict]] = {}
    sizes: Dict[str, List[int]] = {p: [] for p in params}
    for run_dt in runs:
        exp = expected_steps(run_dt, args.max_step_long, args.max_step_short)
        raw[run_dt] = {}
        for p in params:
            info = scan_var(s3, run_dt, p, exp)
            raw[run_dt][p] = info
            sizes[p].extend(info["s3"].values())

    # 변수별 크기 중앙값 (표본 5개 미만이면 최소 크기 기준만 사용)
    medians = {p: (statistics.median(v) if len(v) >= 5 else None) for p, v in sizes.items()}

    # all-steps 를 만드는 파이프라인인지: 범위 안에 all-steps 문서가 하나라도 있으면 그렇다고 봄
    any_all_steps = {p: any(raw[r][p]["all_doc"] for r in runs) for p in params}

    result: Dict[datetime, dict] = {}
    for run_dt in runs:
        vars_ = {p: judge_var(raw[run_dt][p], medians[p], args.size_ratio, args.min_bytes) for p in params}
        for p, info in vars_.items():
            ad = info["all_doc"]
            n_exp = len(info["expected"])
            if ad:
                info["all_state"] = str(ad.get("step_count", "?"))
                info["all_rebuild"] = ad.get("step_count") != n_exp
            else:
                info["all_state"] = "없음"
                info["all_rebuild"] = any_all_steps[p]

        der_missing = 0
        if "10u" in vars_ and "10v" in vars_:
            der = scan_derived(run_dt)
            ok_wind = set(vars_["10u"]["expected"]) - set(vars_["10u"]["todo"]) - set(vars_["10v"]["todo"])
            der_missing = sum(len(ok_wind - der[dv]) for dv in DERIVED)

        result[run_dt] = {"vars": vars_, "derived_missing": der_missing, "medians": medians}
    return result


def run_needs_work(r: dict) -> bool:
    if r["derived_missing"]:
        return True
    for info in r["vars"].values():
        if info["todo"] or info["all_rebuild"] or info["runs_status"] != "success":
            return True
    return False


def print_report(result: Dict[datetime, dict]) -> None:
    tot_exp = tot_ok = 0
    print("=" * 96)
    print("ECMWF 누락 점검  (ok/기대 스텝 | 누락 | 손상의심 | ingestion_runs | all-steps)")
    print("=" * 96)
    for run_dt, r in result.items():
        flag = "⚠️ " if run_needs_work(r) else "✅"
        print(f"{flag} RUN {ing.iso_z(run_dt)}")
        for p, i in r["vars"].items():
            n = len(i["expected"])
            tot_exp += n
            tot_ok += i["ok_cnt"]
            miss = compress_steps(i["missing"], i["expected"]) or "-"
            sus = compress_steps(i["suspect"], i["expected"]) or "-"
            all_s = i["all_state"] + (" (재생성)" if i["all_rebuild"] else "")
            print(f"   {p:<4} {i['ok_cnt']:>3}/{n:<3} 누락:{len(i['missing']):>3} [{miss}]"
                  f"  손상:{len(i['suspect'])} [{sus}]  runs={i['runs_status']}  all-steps={all_s}")
        if r["derived_missing"]:
            print(f"   파생(wind) 메타 누락: {r['derived_missing']}건")
    print("-" * 96)
    bad = [ing.iso_z(k) for k, v in result.items() if run_needs_work(v)]
    print(f"합계: 스텝 {tot_ok}/{tot_exp}  |  처리 필요 회차 {len(bad)}/{len(result)}")
    for b in bad:
        print(f"   - {b}")
    print("=" * 96)


# --------------------------------------------------------------------------- 채우기
def make_client(source: str, max_retries: int, retry_after: int):
    from ecmwf.opendata import Client
    kw = {"source": source, "model": ing.MODEL, "resol": ing.RESOL}
    sig = inspect.signature(Client.__init__).parameters
    if "maximum_retries" in sig:
        kw["maximum_retries"] = max_retries
    if "retry_after" in sig:
        kw["retry_after"] = retry_after
    return Client(**kw)


def fetch_step(client, run_dt: datetime, stream: str, param: str, step: int, target: Path) -> Optional[str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        client.retrieve(
            date=run_dt.date(), type=ing.TYPE_CODE, stream=stream,
            time=f"{run_dt:%H}", step=step, param=param, target=str(target),
        )
    except Exception as e:
        target.unlink(missing_ok=True)
        return f"retrieve: {str(e)[:300]}"
    if not is_valid_grib(target):
        target.unlink(missing_ok=True)
        return "invalid grib (GRIB/7777 헤더·꼬리 불일치)"
    return None


def fill_var(client, s3, run_dt: datetime, info: dict, work: Path, args) -> dict:
    param, stream = info["param"], info["stream"]
    meta = ing.PARAMS[param]
    product_code = meta.get("product_code", ing.PRODUCT_CODE)
    exp = info["expected"]
    max_step = max(exp)

    counters = {"downloaded": 0, "existed": 0, "uploaded": 0, "mongo_written": 0, "failed": 0}
    errors: List[dict] = []
    notes = {"derived_planned_written": 0, "gap_filled_steps": 0}
    log_id, attempt_id, started_at = ing.start_var_run_log(
        run_dt=run_dt, stream=stream, var=param, dataset_code=product_code,
        max_step=max_step, steps=exp, trigger="gap_fill",
    )

    todo = info["todo"]
    print(f"  ▶ {param} ({stream}) 채우기 {len(todo)}스텝: {compress_steps(todo, exp)}")
    consecutive_fail = 0
    for step in todo:
        valid_time = run_dt + timedelta(hours=step)
        filename = f"{ing.PRODUCT_CODE}_{param}_{run_dt:%Y%m%d_%H}Z_step{step:03}.grib2"
        local = work / f"{run_dt:%Y%m%d%H}" / param / filename

        err = fetch_step(client, run_dt, stream, param, step, local)
        if err:
            counters["failed"] += 1
            consecutive_fail += 1
            errors.append({"phase": "retrieve", "step": step, "message": err})
            print(f"    ❌ step {step:03}: {err}")
            if consecutive_fail >= args.max_consecutive_fail:
                print(f"    ⏹️ 연속 {consecutive_fail}회 실패 → {param} 중단 (나머지는 다음 실행에서)")
                break
            time.sleep(args.sleep)
            continue
        consecutive_fail = 0
        counters["downloaded"] += 1

        s3_key = ing.build_s3_key(run_dt, product_code, param, filename)
        try:
            ing.upload_to_s3(s3, local, s3_key)
            counters["uploaded"] += 1
        except Exception as e:
            counters["failed"] += 1
            errors.append({"phase": "s3", "step": step, "message": str(e)[:300]})
            print(f"    ❌ step {step:03}: s3 {e}")
            local.unlink(missing_ok=True)
            continue

        try:
            doc = ing.build_raw_doc(
                source=ing.SOURCE, dataset_code=ing.PRODUCT_CODE, model=ing.MODEL, resol=ing.RESOL,
                asset_type=ing.ASSET_TYPE, type_code=ing.TYPE_CODE, stream=stream, param=param,
                unit=meta["unit"], name_en=meta["name_en"], run_time=run_dt, step=step,
                valid_time=valid_time, filename=filename, local_path=local, s3_key=s3_key,
            )
            doc["gap_filled_at"] = ing.utc_now()
            # 손상 스텝은 기존 문서의 size/s3 를 새 파일 기준으로 덮어써야 하므로 $set
            ing.assets_col.update_one({"natural_key": doc["natural_key"]}, {"$set": doc}, upsert=True)
            ing.upsert_directories_from_doc(doc)
            counters["mongo_written"] += 1
            notes["gap_filled_steps"] += 1
        except Exception as e:
            counters["failed"] += 1
            errors.append({"phase": "mongo", "step": step, "message": str(e)[:300]})
            print(f"    ❌ step {step:03}: mongo {e}")

        local.unlink(missing_ok=True)
        time.sleep(args.sleep)

    # 실제 데이터로 다시 판정해서 status 기록
    re_info = judge_var(scan_var(s3, run_dt, param, exp), info.get("_median"), args.size_ratio, args.min_bytes)
    status = "success" if not re_info["todo"] else ("partial" if re_info["ok_cnt"] > 0 else "failed")
    ing.finish_var_run_log(
        log_id, attempt_id, started_at=started_at, status=status,
        counters=counters, errors=errors, notes=notes,
    )
    print(f"  ◀ {param} status={status} ok={re_info['ok_cnt']}/{len(exp)} counters={counters}")
    re_info["all_state"], re_info["all_rebuild"] = info["all_state"], info["all_rebuild"]
    # 스텝을 새로 채웠으면 기존 all-steps 는 내용이 바뀌었으므로 재생성 대상
    if notes["gap_filled_steps"] and (info["all_doc"] or info["all_rebuild"]):
        re_info["all_rebuild"] = True
    return re_info


def mark_success_only(run_dt: datetime, info: dict) -> None:
    """데이터는 다 있는데 ingestion_runs 가 success 가 아닌 경우 상태만 맞춤."""
    param, stream = info["param"], info["stream"]
    meta = ing.PARAMS[param]
    exp = info["expected"]
    log_id, attempt_id, started_at = ing.start_var_run_log(
        run_dt=run_dt, stream=stream, var=param, dataset_code=meta.get("product_code", ing.PRODUCT_CODE),
        max_step=max(exp), steps=exp, trigger="gap_fill",
    )
    zero = {"downloaded": 0, "existed": len(exp), "uploaded": 0, "mongo_written": 0, "failed": 0}
    ing.finish_var_run_log(
        log_id, attempt_id, started_at=started_at, status="success",
        counters=zero, errors=[], notes={"derived_planned_written": 0, "verified_by_gap_fill": 1},
    )
    print(f"  ✔ {param}: 데이터 {len(exp)}/{len(exp)} 확인 → ingestion_runs status {info['runs_status']} → success")


def fill_derived(run_dt: datetime, vars_: Dict[str, dict]) -> int:
    if "10u" not in vars_ or "10v" not in vars_:
        return 0
    der = scan_derived(run_dt)
    ok_wind = set(vars_["10u"]["expected"]) - set(vars_["10u"]["todo"]) - set(vars_["10v"]["todo"])
    written = 0
    for dv, dmeta in ing.DERIVED_VARS.items():
        for step in sorted(ok_wind - der[dv]):
            doc = ing.build_derived_doc(
                source=ing.SOURCE, dataset_code=DS_DERIVED, model=ing.MODEL, resol=ing.RESOL,
                type_code=ing.TYPE_CODE, stream="oper", derived_var=dv, derived_meta=dmeta,
                run_time=run_dt, step=step, valid_time=run_dt + timedelta(hours=step),
            )
            ing.upsert_assets(doc)
            written += 1
    if written:
        print(f"  ✔ 파생(wind) planned 메타 {written}건 추가")
    return written


_ALLM = None


def rebuild_all_steps(s3, run_dt: datetime, info: dict, work: Path) -> None:
    global _ALLM
    if _ALLM is None:
        _ALLM = _load_module("ecmwf_all_steps_for_gapfill", "ecmwf_all_steps.py")
    allm = _ALLM
    param = info["param"]
    old_key = ((info.get("all_doc") or {}).get("s3") or {}).get("key")
    wdir = work / "all_steps"
    try:
        ok = allm.process_run(s3, run_dt, param, wdir, skip_existing=False)
    finally:
        shutil.rmtree(wdir, ignore_errors=True)
    print(f"  {'✔' if ok else '❌'} all-steps 재생성 {param}: {'성공' if ok else '실패'}")
    if ok and old_key:
        new_doc = ing.assets_col.find_one(
            {"source": ing.SOURCE, "model": ing.MODEL, "dataset_code": DS_ALL,
             "variable": param, "run_time_utc": ing.iso_z(run_dt)}, {"s3.key": 1})
        new_key = ((new_doc or {}).get("s3") or {}).get("key")
        if new_key and new_key != old_key:
            print(f"    ℹ️ 이전 all-steps 파일은 S3 에 남아 있음(참조 없음): s3://{ing.BUCKET}/{old_key}")


def fill_run(s3, run_dt: datetime, r: dict, args) -> bool:
    print(f"\n▶▶ FILL {ing.iso_z(run_dt)}  (source={args.source})")
    work = Path(args.work_dir)
    client = None
    vars_ = r["vars"]
    for p, info in list(vars_.items()):
        info["_median"] = r["medians"].get(p)
        if info["todo"]:
            if client is None:
                client = make_client(args.source, args.max_retries, args.retry_after)
            vars_[p] = fill_var(client, s3, run_dt, info, work, args)
            vars_[p]["all_doc"] = info["all_doc"]
        elif info["runs_status"] != "success":
            mark_success_only(run_dt, info)

    fill_derived(run_dt, vars_)

    if args.rebuild_all_steps:
        for p, info in vars_.items():
            if info["all_rebuild"] and not info["todo"]:
                rebuild_all_steps(s3, run_dt, info, work)
            elif info["all_rebuild"]:
                print(f"  ⏭️ all-steps {p}: 원본 스텝이 아직 모자라 재생성 보류")

    shutil.rmtree(work / f"{run_dt:%Y%m%d%H}", ignore_errors=True)
    complete = all(not i["todo"] for i in vars_.values())
    print(f"◀◀ FILL {ing.iso_z(run_dt)} → {'완료' if complete else '미완(다음 실행에서 이어서)'}")
    return complete


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="ECMWF IFS 누락 점검/채우기 (S3 + Mongo 실데이터 기준)")
    ap.add_argument("--run", help="단일 회차 예: 2026-10-18T12:00:00Z")
    ap.add_argument("--start_utc", default="2026-10-16T00:00:00Z")
    ap.add_argument("--end_utc", default="2026-10-20T18:00:00Z")
    ap.add_argument("--params", default=",".join(ing.PARAMS.keys()))
    ap.add_argument("--max_step_long", type=int, default=360)
    ap.add_argument("--max_step_short", type=int, default=144)

    ap.add_argument("--fill", action="store_true", help="누락/손상 스텝을 실제로 받아서 채움 (없으면 점검만)")
    ap.add_argument("--rebuild_all_steps", action="store_true", help="스텝 수가 모자란 all-steps 파일 재생성")
    ap.add_argument("--source", default="aws", help="원천: aws(기본) / azure / google / ecmwf")
    ap.add_argument("--max_retries", type=int, default=8, help="스텝 1개당 원천 재시도 횟수 (ingest 기본 500)")
    ap.add_argument("--retry_after", type=int, default=30, help="재시도 간격(초) (ingest 기본 120)")
    ap.add_argument("--max_consecutive_fail", type=int, default=3, help="변수별 연속 실패 시 그 변수 중단")
    ap.add_argument("--sleep", type=float, default=0.5, help="스텝 간 대기(초)")
    ap.add_argument("--size_ratio", type=float, default=0.5, help="중앙값 대비 이 비율 미만이면 손상 의심")
    ap.add_argument("--min_bytes", type=int, default=10_000, help="이 크기 미만이면 손상 의심")
    ap.add_argument("--work_dir", default=str(HERE / "_gap_fill_work"))
    ap.add_argument("--runs_out", help="처리가 필요한 회차 목록을 이 파일에 한 줄씩 기록")
    args = ap.parse_args()

    if ing.assets_col is None or ing.runs_col is None:
        raise SystemExit("❌ MONGO_URI 가 비어 있음 (.env_runtime 로드 확인)")

    params = [p.strip() for p in args.params.split(",") if p.strip()]
    for p in params:
        if p not in ing.PARAMS:
            raise SystemExit(f"❌ 알 수 없는 변수: {p}")

    import boto3
    s3 = boto3.client("s3", region_name=ing.REGION)
    runs = run_list(args)

    result = scan(s3, runs, params, args)
    print_report(result)

    todo_runs = [r for r, v in result.items() if run_needs_work(v)]
    if args.runs_out:
        Path(args.runs_out).write_text("".join(f"{ing.iso_z(r)}\n" for r in todo_runs))
        print(f"📝 처리 필요 회차 {len(todo_runs)}개 → {args.runs_out}")

    if not args.fill:
        return

    all_done = True
    for run_dt in todo_runs:
        if not fill_run(s3, run_dt, result[run_dt], args):
            all_done = False
    sys.exit(0 if all_done else 3)


if __name__ == "__main__":
    main()
