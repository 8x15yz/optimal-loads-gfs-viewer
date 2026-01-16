FROM apache/airflow:2.8.1-python3.11

# --------------------------------------------------
# system deps
# --------------------------------------------------
USER root
RUN apt-get update \
 && apt-get install -y gcc \
 && apt-get clean

# --------------------------------------------------
# airflow env
# --------------------------------------------------
ENV AIRFLOW_HOME=/opt/airflow
ENV PYTHONPATH="/opt/airflow:/opt/airflow/orchestration"

USER airflow

# --------------------------------------------------
# python deps (airflow-specific)
# --------------------------------------------------
# COPY <src> <dest>
# <src> docker build를 실행한 디렉토리(= build context) 기준의 경로
# <dest> 컨테이너 내부 경로
COPY 2026-01-demo-v0.4/orchestration/airflow/requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

# --------------------------------------------------
# copy orchestration code
# --------------------------------------------------
USER root
COPY 2026-01-demo-v0.4/orchestration /opt/airflow/orchestration
COPY 2026-01-demo-v0.4/orchestration/airflow/dags /opt/airflow/dags

RUN chown -R airflow: /opt/airflow

USER airflow
