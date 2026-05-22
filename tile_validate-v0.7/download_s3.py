# Usage:
#   python download_s3.py s413_260521_06z
#   python download_s3.py s111_260521_06z
#
# target format: {site}_{YYMMDD}_{HHz}

import os
import argparse
import boto3
from pathlib import Path

BUCKET = "optimal-loads"
DEFAULT_TARGET = "s413_260521_06z"


def parse_target(target: str) -> tuple[str, Path]:
    parts = target.split("_")
    if len(parts) != 3:
        raise ValueError("target must look like s413_260521_06z")

    site, yymmdd, run = parts
    if len(yymmdd) != 6 or not yymmdd.isdigit():
        raise ValueError("date part must be YYMMDD, for example 260521")

    if len(run) != 3 or not run[:2].isdigit() or run[2].lower() != "z":
        raise ValueError("run part must look like 06z")

    year = f"20{yymmdd[:2]}"
    month = yymmdd[2:4]
    day = yymmdd[4:6]
    prefix = f"{site}/{year}/{month}/{day}/{run.upper()}/"
    local_dir = Path(f"./downloaded_{target.lower()}")

    return prefix, local_dir


def download_all(bucket: str, prefix: str, local_dir: Path) -> None:
    s3 = boto3.client(
        "s3",
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-2"),
    )

    paginator = s3.get_paginator("list_objects_v2")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

    downloaded = 0
    for page in pages:
        for obj in page.get("Contents", []):
            key = obj["Key"]
            relative = key[len(prefix):]  # prefix 이후 상대 경로
            dest = local_dir / relative

            dest.parent.mkdir(parents=True, exist_ok=True)
            print(f"Downloading: {key} -> {dest}")
            s3.download_file(bucket, key, str(dest))
            downloaded += 1

    print(f"\nDone. {downloaded} file(s) downloaded to '{local_dir.resolve()}'")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download all files from an optimal-loads S3 prefix."
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=DEFAULT_TARGET,
        help="Target like s413_260521_06z. Defaults to %(default)s.",
    )
    args = parser.parse_args()

    PREFIX, LOCAL_DIR = parse_target(args.target)
    download_all(BUCKET, PREFIX, LOCAL_DIR)
