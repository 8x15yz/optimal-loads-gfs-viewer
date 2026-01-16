import os
from datetime import datetime, timezone
from pathlib import Path

import boto3
from ecmwf.opendata import Client
from pymongo import MongoClient

from services.collector.ecmwf import (
    fetch_ecmwf_grib,
    build_raw_doc,
    upload_to_s3,
    upsert_mongo,
)
from services.collector.ecmwf.directories import upsert_directories

UTC = timezone.utc


# ----------------------------------------------------------------
# Test: 1분마다 실행 확인용 태스크
# ----------------------------------------------------------------
def test_task_function(**context):
    execution_date = context["execution_date"]
    print(f"🕒 Execution date: {execution_date}")

def run_ecmwf_ifs_task(
    dataset_code: str,
    model: str,
    stream: str,
    params: list[str],
    **context,
):
    execution_date = context["execution_date"]

    BASE_RUN_TIME = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    STEP_INTERVAL = 3
    MAX_STEP = 360

    delta_minutes = int(
        (execution_date - BASE_RUN_TIME).total_seconds() // 60
    )
    step = delta_minutes * STEP_INTERVAL

    if step > MAX_STEP:
        print("⏭ DEMO finished")
        return

    run_time = BASE_RUN_TIME

    # clients
    ecmwf = Client(source="aws", model=model, resol="0p25")
    s3 = boto3.client("s3")
    mongo = MongoClient(os.environ["MONGO_URI"])
    collection = mongo["optimal_loads"]["assets_metadata"]
    dir_collection = mongo["optimal_loads"]["directories"]

    for param in params:
        local_path = Path(
            f"/tmp/{dataset_code}_{param}_"
            f"{run_time:%Y%m%d_%HZ}_step{step:03d}.grib2"
        )

        fetch_ecmwf_grib(
            client=ecmwf,
            run_time=run_time,
            step=step,
            param=param,
            stream=stream,
            target_path=local_path,
        )

        doc = build_raw_doc(
            source="ecmwf",
            dataset_code=dataset_code,
            model=model,
            asset_type="forecast",
            stream=stream,
            param=param,
            run_time=run_time,
            step=step,
            valid_time=run_time + timedelta(hours=step),
            local_path=local_path,
        )

        upload_to_s3(...)
        upsert_mongo(collection, doc)
        upsert_directories(dir_collection, doc["inventory_directory"])

        print(f"✅ {run_time:%Y-%m-%d %HZ} / {param} / step {step:03d}")

