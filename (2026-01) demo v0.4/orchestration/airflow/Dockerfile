FROM apache/airflow:2.8.1-python3.11

# -------------------------------
# System deps (root)
# -------------------------------
USER root

RUN apt-get update && apt-get install -y \
    gcc \
    && apt-get clean

# -------------------------------
# Python deps (airflow user)
# -------------------------------
USER airflow

COPY orchestration/airflow/requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

# -------------------------------
# Project code (root)
# -------------------------------
USER root

COPY orchestration /opt/airflow/orchestration
COPY services /opt/airflow/services
COPY orchestration/airflow/dags /opt/airflow/dags

# -------------------------------
# Runtime
# -------------------------------
ENV PYTHONPATH=/opt/airflow

USER airflow
