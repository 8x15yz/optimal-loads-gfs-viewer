FROM apache/airflow:2.8.1-python3.11

USER root
RUN apt-get update && apt-get install -y gcc && apt-get clean

ENV AIRFLOW_HOME=/opt/airflow
ENV PYTHONPATH="/opt/airflow:/opt/airflow/services:/opt/airflow/orchestration"

USER airflow

COPY orchestration/airflow/requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt

USER root
COPY orchestration /opt/airflow/orchestration
COPY services /opt/airflow/services
COPY orchestration/airflow/dags /opt/airflow/dags

RUN chown -R airflow: /opt/airflow

USER airflow
