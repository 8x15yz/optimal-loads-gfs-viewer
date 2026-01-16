# services/collector/ecmwf/fetch.py

from pathlib import Path
from datetime import datetime
from ecmwf.opendata import Client


def fetch_ecmwf_grib(
    *,
    client: Client,
    run_time: datetime,
    step: int,
    param: str,
    stream: str,
    target_path: Path,
    type_code: str = "fc",
) -> Path:
    """
    ECMWF Open Data에서 단일 GRIB 파일 다운로드

    - run_time: 2025-01-10 06:00 UTC
    - step: forecast step (hours)
    - param: 10u, 10v, swh ...
    - stream: oper, wave ...
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)

    client.retrieve(
        date=run_time.date(),
        type=type_code,
        stream=stream,
        time=f"{run_time:%H}",
        step=step,
        param=param,
        target=str(target_path),
    )

    return target_path
