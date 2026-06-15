#!/usr/bin/env python3
"""
Download all latest marine weather data from Optimal Loads API.
Usage:
    python download_latest.py                          # download everything
    python download_latest.py --vars HTSGW WDIR        # select variables
    python download_latest.py --save-json latest.json  # save JSON, then download
    python download_latest.py --from-json latest.json  # download from saved JSON (offline)
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

    # ── 1. Load JSON ─────────────────────────────────────────────────
    if args.from_json:
        print(f"[JSON] Loading {args.from_json}")
        with open(args.from_json, "r") as f:
            data = json.load(f)
    else:
        print(f"[API] Requesting {API_URL} ...")
        r = requests.get(API_URL, timeout=30)
        r.raise_for_status()
        data = r.json()
        issued = data["issued"]
        print(f"[API] Response received  generated: {issued['generated_at']}  expires: {issued['expires_at']}")

        if args.save_json:
            with open(args.save_json, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"[JSON] Saved to {args.save_json}")

    # ── 2. Prepare log file ──────────────────────────────────────────
    os.makedirs(args.out_dir, exist_ok=True)
    log_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(args.out_dir, f"download_{log_ts}.txt")
    log = open(log_path, "w", encoding="utf-8")

    def wlog(line: str = ""):
        log.write(line + "\n")

    issued = data["issued"]
    wlog(f"Generated at : {issued['generated_at']}")
    wlog(f"Expires at   : {issued['expires_at']}")
    wlog(f"Total files  : {data['summary']['total_files']}")
    wlog(f"Variables    : {', '.join(data['summary']['variables_included'])}")
    wlog()

    # ── 3. Per-variable summary (console + log) ──────────────────────
    target_vars = args.vars if args.vars else list(data["assets"].keys())
    tasks: list[tuple[dict, str]] = []

    print()
    for var_name in target_vars:
        if var_name not in data["assets"]:
            print(f"  [SKIP] {var_name} — unknown variable")
            wlog(f"[SKIP] {var_name} — unknown variable")
            continue

        var_data = data["assets"][var_name]
        var_dir = os.path.join(args.out_dir, var_name)
        os.makedirs(var_dir, exist_ok=True)

        for step in var_data["steps"]:
            tasks.append((step, var_dir))

        line = f"  {var_name:<20s}  {var_data['file_count']:>3} files"
        print(line)
        wlog(line)

    total = len(tasks)
    print(f"\n  Starting {total} downloads ({args.workers} concurrent)")
    wlog()
    wlog(f"Starting {total} downloads ({args.workers} concurrent)")
    wlog("-" * 60)

    # ── 4. Parallel download ─────────────────────────────────────────
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

            print(f"\r  Progress: {done:>{len(str(total))}}/{total}  ok {success}  failed {failed}  ", end="", flush=True)

    print()  # finish the progress line

    # ── 5. Print failure list to console + finalize log ──────────────
    wlog("-" * 60)
    wlog(f"Done: ok {success} / failed {failed} / total {total}")
    if fails:
        wlog()
        wlog("[Failure list]")
        for fname, err in fails:
            wlog(f"  {fname}  — {err}")

    log.close()

    print(f"\n  Done: ok {success} / failed {failed} / total {total}")
    if fails:
        print(f"\n  [Failed files]")
        for fname, err in fails:
            print(f"    {fname}  — {err}")
    print(f"\n  Log: {log_path}")


if __name__ == "__main__":
    main()