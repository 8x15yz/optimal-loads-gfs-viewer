# services/collector/ecmwf/metadata.py

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

UTC = timezone.utc


def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_raw_doc(
    *,
    source: str,
    dataset_code: str,
    model: str,
    asset_type: str,
    stream: str,
    param: str,
    unit: str,
    name_en: str,
    run_time: datetime,
    step: int,
    valid_time: datetime,
    local_path: Path,
    s3_key: Optional[str],
) -> Dict:
    size_bytes = local_path.stat().st_size

    natural_key = (
        f"{dataset_code}|{source}|{model}|{asset_type}|{stream}|{param}"
        f"|run={iso_z(run_time)}|step={step}"
    )
    valid_key = (
        f"{dataset_code}|{source}|{model}|{asset_type}|{stream}|{param}"
        f"|valid={iso_z(valid_time)}"
    )

    doc = {
        "source": source,
        "dataset_code": dataset_code,
        "variable": param,
        "name_en": name_en,
        "unit": unit,
        "model": model,
        "type": asset_type,
        "stream": stream,
        "run_time_utc": iso_z(run_time),
        "step_hours": step,
        "valid_time_utc": iso_z(valid_time),
        "size_bytes": size_bytes,
        "created_at": datetime.now(UTC),
        "natural_key": natural_key,
        "valid_key": valid_key,
        "inventory_directory": (
            f"{source}/{model}/"
            f"{run_time:%Y/%Y-%m/%Y-%m-%d/%H}Z/"
            f"{dataset_code}/{param}/"
        ),
    }

    if s3_key:
        doc["s3"] = {"key": s3_key}

    return doc
