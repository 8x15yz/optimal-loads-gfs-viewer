# services/collector/ecmwf/storage.py

from pathlib import Path
from typing import Optional
import boto3
from pymongo.collection import Collection


def upload_to_s3(
    *,
    s3_client,
    bucket: str,
    local_path: Path,
    s3_key: str,
) -> None:
    s3_client.upload_file(
        str(local_path),
        bucket,
        s3_key,
        ExtraArgs={"ContentType": "application/x-grib"},
    )


def upsert_mongo(
    *,
    collection,
    doc: dict,
) -> None:
    if collection is None:
        print("⚠️ Mongo collection is None")
        return

    result = collection.update_one(
        {"natural_key": doc["natural_key"]},
        {"$setOnInsert": doc},
        upsert=True,
    )

    print(
        "🧾 mongo upsert:",
        "matched=", result.matched_count,
        "upserted_id=", result.upserted_id,
    )
