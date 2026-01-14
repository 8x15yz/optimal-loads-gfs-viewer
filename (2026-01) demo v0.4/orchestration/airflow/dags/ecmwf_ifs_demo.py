from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

# 🔥 Airflow가 호출할 실제 작업 함수
from orchestration.runners.ecmwf_task import run_ecmwf_ifs_task


# -------------------------------------------------------------------
# DAG 기본 설정
# -------------------------------------------------------------------
DEFAULT_ARGS = {
    "owner": "bluemap",
    "depends_on_past": False,
    "retries": 0,
    "retry_delay": timedelta(minutes=1),
}


with DAG(
    dag_id="ecmwf_ifs_demo_v0_4",
    start_date=datetime(2026, 1, 1, 0, 0),
    schedule_interval="*/1 * * * *",
    catchup=True,   # ⚠️ 중요
    max_active_runs=1,
) as dag:

    # ----------------------------------------------------------------
    # Task: ECMWF IFS 수집 + 저장 + 알림까지
    # ----------------------------------------------------------------
    ecmwf_ifs_task = PythonOperator(
        task_id="run_ecmwf_ifs_collection",
        python_callable=run_ecmwf_ifs_task,
        op_kwargs={
            # 🔽 필요하면 파라미터화 가능
            "dataset_code": "original",
            "model": "ifs",
            "stream": "oper",
            "params": ["10u", "10v"],   # demo용
            "steps": [0],              # demo용
        },
    )

    ecmwf_ifs_task
