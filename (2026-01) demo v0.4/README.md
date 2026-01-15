(2026-01) DEMO V0.4
│
├── services
│   ├── collector
│   │   ├── ecmwf
│   │   │   ├── fetch.py        
│   │   │   ├── metadata.py     
│   │   │   ├── storage.py      
│   │   │   ├── directories.py     
│   │   │   └── __init__.py
│   │   └── __init__.py
│   │
│   └── fastapi-inventory
│       └── app
│
├── orchestration
│   ├── airflow
│   │   └── dags
│   │       └── ecmwf_ifs_demo.py 
│   │
│   ├── kafka
│   │   ├── producer.py 
│   │   ├── topics.py
│   │   └── schemas.py
│   │
│   └── runners
│       └── ecmwf_task.py 
│
└── README.md
