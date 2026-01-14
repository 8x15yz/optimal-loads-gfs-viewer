(2026-01) DEMO V0.4
│
├── services
│   ├── collector
│   │   ├── ecmwf
│   │   │   ├── fetch.py          ← 🔹 “한 run/step/param 수집 함수”
│   │   │   ├── metadata.py       ← 🔹 raw / derived doc builder
│   │   │   ├── storage.py        ← 🔹 S3 / Mongo 저장
│   │   │   └── __init__.py
│   │   └── __init__.py
│   │
│   └── fastapi-inventory
│       └── app
│
├── orchestration
│   ├── airflow
│   │   └── dags
│   │       └── ecmwf_ifs_demo.py  ← ✅ DAG (1분마다, 06Z 고정)
│   │
│   ├── kafka
│   │   ├── producer.py           ← ✅ 알림 전용
│   │   ├── topics.py
│   │   └── schemas.py
│   │
│   └── runners
│       └── ecmwf_task.py          ← 🔥 Airflow가 호출하는 “조립 코드”
│
└── README.md
