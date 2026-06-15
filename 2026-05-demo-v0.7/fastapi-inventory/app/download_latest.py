#!/usr/bin/env python3
"""
Download all latest marine weather data from Optimal Loads API.
Usage:
    python download_latest.py                          # 전체 다운
    python download_latest.py --vars HTSGW WDIR        # 변수 선택
    python download_latest.py --save-json latest.json  # JSON 저장 후 다운
    python download_latest.py --from-json latest.json  # 저장된 JSON으로 다운 (오프라인)
"""

import requests
import json
import os
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone


API_URL = "http://weather-api.bmap.kr/api/latest"


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def download_file(item: dict, save_dir: str) -> tuple[str, bool, str]:
    href = item["href"]
    s3_key = item["s3_key"]
    filename = os.path.basename(s3_key)
    save_path = os.path.join(save_dir, filename)

    try:
        r = requests.get(href, stream=True, timeout=60)
        r.raise_for_status()
        with open(save_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
        size_kb = os.path.getsize(save_path) / 1024
        return s3_key, True, f"{size_kb:.0f} KB"
    except Exception as e:
        return s3_key, False, str(e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vars", nargs="+", metavar="VAR")
    parser.add_argument("--save-json", metavar="PATH")
    parser.add_argument("--from-json", metavar="PATH")
    parser.add_argument("--out-dir", default="./weather_data")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    # ── 1. JSON 로드 ─────────────────────────────────────────────────
    if args.from_json:
        print(f"[JSON] {args.from_json} 로드")
        with open(args.from_json, "r") as f:
            data = json.load(f)
    else:
        print(f"[API] {API_URL} 요청 중...")
        r = requests.get(API_URL, timeout=30)
        r.raise_for_status()
        data = r.json()
        issued = data["issued"]
        print(f"[API] 응답 완료  생성: {issued['generated_at']}  만료: {issued['expires_at']}")

        if args.save_json:
            with open(args.save_json, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"[JSON] {args.save_json} 저장 완료")

    # ── 2. 로그 파일 준비 ─────────────────────────────────────────────
    os.makedirs(args.out_dir, exist_ok=True)
    log_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(args.out_dir, f"download_{log_ts}.txt")
    log = open(log_path, "w", encoding="utf-8")

    def wlog(line: str = ""):
        log.write(line + "\n")

    issued = data["issued"]
    wlog(f"생성 시각 : {issued['generated_at']}")
    wlog(f"만료 시각 : {issued['expires_at']}")
    wlog(f"총 파일 수: {data['summary']['total_files']}")
    wlog(f"포함 변수 : {', '.join(data['summary']['variables_included'])}")
    wlog()

    # ── 3. 변수별 요약 (콘솔 + 로그) ────────────────────────────────
    target_vars = args.vars if args.vars else list(data["assets"].keys())
    tasks: list[tuple[dict, str]] = []

    print()
    for var_name in target_vars:
        if var_name not in data["assets"]:
            print(f"  [SKIP] {var_name} — 없는 변수")
            wlog(f"[SKIP] {var_name} — 없는 변수")
            continue

        var_data = data["assets"][var_name]
        var_dir = os.path.join(args.out_dir, var_name)
        os.makedirs(var_dir, exist_ok=True)

        for step in var_data["steps"]:
            tasks.append((step, var_dir))

        line = f"  {var_name:<20s}  {var_data['file_count']:>3}개 파일"
        print(line)
        wlog(line)

    total = len(tasks)
    print(f"\n  총 {total}개 다운로드 시작 (동시 {args.workers}개)")
    wlog()
    wlog(f"총 {total}개 다운로드 시작 (동시 {args.workers}개)")
    wlog("-" * 60)

    # ── 4. 병렬 다운로드 ─────────────────────────────────────────────
    success, failed = 0, 0
    fails: list[tuple[str, str]] = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(download_file, step, save_dir): step["s3_key"]
            for step, save_dir in tasks
        }

        for future in as_completed(futures):
            s3_key, ok, msg = future.result()
            filename = os.path.basename(s3_key)
            done = success + failed + 1

            if ok:
                success += 1
                wlog(f"[OK]   {filename}  ({msg})")
            else:
                failed += 1
                fails.append((filename, msg))
                wlog(f"[FAIL] {filename}  — {msg}")

            print(f"\r  진행: {done:>{len(str(total))}}/{total}  성공 {success}  실패 {failed}  ", end="", flush=True)

    print()  # 진행 줄 마무리

    # ── 5. 실패 목록 콘솔 출력 + 로그 마무리 ────────────────────────
    wlog("-" * 60)
    wlog(f"완료: 성공 {success} / 실패 {failed} / 총 {total}")
    if fails:
        wlog()
        wlog("[실패 목록]")
        for fname, err in fails:
            wlog(f"  {fname}  — {err}")

    log.close()

    print(f"\n  완료: 성공 {success} / 실패 {failed} / 총 {total}")
    if fails:
        print(f"\n  [실패 파일]")
        for fname, err in fails:
            print(f"    {fname}  — {err}")
    print(f"\n  로그: {log_path}")


if __name__ == "__main__":
    main()
