import os
import boto3

BUCKET = os.getenv("S3_BUCKET", "optimal-loads")
REGION = os.getenv("AWS_REGION", "ap-northeast-2")
BASE_PREFIX = os.getenv("BASE_PREFIX", "ecmwf/ifs/fc/2025/")

NEEDLE = "/original/pp1d/"

DRY_RUN = False
PRINT_SAMPLE = 20

s3 = boto3.client("s3", region_name=REGION)

def list_pp1d_keys(base_prefix: str):
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=base_prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if NEEDLE in key:
                keys.append(key)
    return keys

def delete_keys(keys):
    # S3는 1000개씩 삭제
    for i in range(0, len(keys), 1000):
        chunk = keys[i:i+1000]
        if DRY_RUN:
            continue
        s3.delete_objects(
            Bucket=BUCKET,
            Delete={"Objects": [{"Key": k} for k in chunk]}
        )

if __name__ == "__main__":
    keys = list_pp1d_keys(BASE_PREFIX)
    print(f"[S3] bucket={BUCKET}")
    print(f"[S3] base_prefix={BASE_PREFIX}")
    print(f"[S3] pp1d objects to delete = {len(keys)}")
    print(f"[S3] DRY_RUN={DRY_RUN}")

    if keys:
        print("\n--- sample keys ---")
        for k in keys[:PRINT_SAMPLE]:
            print(k)
        if len(keys) > PRINT_SAMPLE:
            print("...")

    if not keys:
        raise SystemExit("No keys found. Nothing to delete.")

    delete_keys(keys)
    if DRY_RUN:
        print("\n(DRY_RUN=True, not deleted)")
    else:
        print("\n✅ Deleted.")