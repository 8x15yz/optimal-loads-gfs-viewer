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

API_URL = "http://weather-api.bmap.kr/api/latest"


def download_file(item: dict, save_dir: str) -> tuple[str, bool, str]:
    """단일 파일 다운로드. (s3_key, 성공여부, 메시지) 반환"""
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
    parser.add_argument("--vars", nargs="+", metavar="VAR", help="다운받을 변수 선택 (예: HTSGW WDIR)")
    parser.add_argument("--save-json", metavar="PATH", help="API 응답 JSON 저장 경로")
    parser.add_argument("--from-json", metavar="PATH", help="저장된 JSON 파일에서 다운 (API 호출 생략)")
    parser.add_argument("--out-dir", default="./weather_data", help="다운로드 저장 폴더 (기본: ./weather_data)")
    parser.add_argument("--workers", type=int, default=8, help="동시 다운로드 수 (기본: 8)")
    args = parser.parse_args()

    # 1. JSON 로드
    if args.from_json:
        print(f"[JSON] {args.from_json} 에서 로드")
        with open(args.from_json, "r") as f:
            data = json.load(f)
    else:
        print(f"[API] {API_URL} 요청 중...")
        r = requests.get(API_URL, timeout=30)
        r.raise_for_status()
        data = r.json()
        issued = data["issued"]
        print(f"[API] 응답 완료 (만료: {issued['expires_at']} / {issued['expires_in_seconds']}초 후)")

        if args.save_json:
            with open(args.save_json, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print(f"[JSON] {args.save_json} 저장 완료")

    # 2. 메타 출력
    issued = data["issued"]
    print(f"\n생성 시각 : {issued['generated_at']}")
    print(f"만료 시각 : {issued['expires_at']}")
    print(f"총 파일 수: {data['summary']['total_files']}")
    print(f"포함 변수 : {', '.join(data['summary']['variables_included'])}\n")

    # 3. 다운로드 대상 수집
    target_vars = args.vars if args.vars else list(data["assets"].keys())
    tasks = []  # (step_dict, save_dir)

    for var_name in target_vars:
        if var_name not in data["assets"]:
            print(f"[SKIP] {var_name} — JSON에 없는 변수")
            continue

        var_data = data["assets"][var_name]
        var_dir = os.path.join(args.out_dir, var_name)
        os.makedirs(var_dir, exist_ok=True)

        for step in var_data["steps"]:
            tasks.append((step, var_dir))

        print(f"  {var_name:20s} → {var_data['file_count']}개 파일  ({var_dir})")

    total = len(tasks)
    print(f"\n총 {total}개 파일 다운로드 시작 (동시 {args.workers}개)\n")

    # 4. 병렬 다운로드
    success, failed = 0, 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(download_file, step, save_dir): step["s3_key"]
            for step, save_dir in tasks
        }

        for future in as_completed(futures):
            s3_key, ok, msg = future.result()
            filename = os.path.basename(s3_key)
            if ok:
                success += 1
                print(f"  [OK]   {filename} ({msg})")
            else:
                failed += 1
                print(f"  [FAIL] {filename} — {msg}")

    print(f"\n완료: {success}개 성공 / {failed}개 실패 / 총 {total}개")


if __name__ == "__main__":
    main()
