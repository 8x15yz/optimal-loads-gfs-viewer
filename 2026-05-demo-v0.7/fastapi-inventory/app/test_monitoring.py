"""Run: python -m pytest tests/test_monitoring.py (test-only dependencies in README)."""
import asyncio
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mongomock
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# The supplied ZIP is the contents of app/, also support testing it as a flat directory.
root = Path(__file__).resolve().parent
if 'app' not in sys.modules:
    spec = importlib.util.spec_from_file_location('app', root / '__init__.py', submodule_search_locations=[str(root)])
    package = importlib.util.module_from_spec(spec)
    sys.modules['app'] = package
    spec.loader.exec_module(package)

from app import monitoring, request_logging


class Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    async def to_list(self, length):
        return self.rows[:length]


class Collection:
    def __init__(self):
        self.raw = mongomock.MongoClient(tz_aware=True).db.request_logs
        self.fail_writes = False
        self.fail_queries = False

    async def create_index(self, *args, **kwargs):
        return self.raw.create_index(*args, **kwargs)

    async def index_information(self):
        return self.raw.index_information()

    async def drop_index(self, name):
        return self.raw.drop_index(name)

    async def insert_many(self, documents, **kwargs):
        if self.fail_writes:
            raise RuntimeError('simulated database outage')
        return self.raw.insert_many(documents, **kwargs)

    def aggregate(self, pipeline, **kwargs):
        if self.fail_queries:
            raise RuntimeError('simulated database outage')
        return Cursor(self.raw.aggregate(pipeline))


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv('MONITORING_USERNAME', 'operator')
    monkeypatch.setenv('MONITORING_PASSWORD', 'test-password')
    collection = Collection()

    async def get_collection():
        return collection

    monkeypatch.setattr(request_logging, 'get_request_logs_collection', get_collection)
    monkeypatch.setattr(monitoring, 'get_request_logs_collection', get_collection)
    monkeypatch.setattr(monitoring, 'templates', Jinja2Templates(directory=str(root / 'templates')))
    app = FastAPI(lifespan=request_logging.request_logging_lifespan)
    app.add_middleware(request_logging.RequestLogMiddleware)
    app.include_router(monitoring.router)
    app.mount('/guide', StaticFiles(directory=str(root / 'templates/static/guide')))

    @app.get('/api/griddata')
    async def success(n: int = 1):
        return {'ok': True, 'n': n}

    @app.get('/crash')
    async def crash():
        raise RuntimeError('do not save exception secrets')

    @app.get('/stream')
    async def stream():
        async def chunks():
            yield b'abc'
            yield b'defg'
        return StreamingResponse(chunks())

    @app.post('/post')
    async def post():
        return {'ok': True}

    return app, collection


def drain(client, app):
    client.portal.call(app.state.request_log_writer.queue.join)


def test_statuses_ip_redaction_streaming_and_get_only(setup):
    app, collection = setup
    with TestClient(app, raise_server_exceptions=False, client=('203.0.113.42', 1234)) as client:
        res = client.get('/api/griddata?variable=WIND&token=secret&api_key=other-secret', headers={
            'X-Forwarded-For': '1.2.3.4', 'X-Real-IP': '5.6.7.8',
            'Authorization': 'Bearer not-stored', 'Cookie': 'private=not-stored',
        })
        assert res.status_code == 200
        assert client.get('/not-found').status_code == 404
        assert client.get('/api/griddata?n=wrong').status_code == 422
        assert client.get('/crash').status_code == 500
        assert client.get('/stream').content == b'abcdefg'
        assert client.post('/post').status_code == 200
        drain(client, app)
        docs = list(collection.raw.find())
        assert len(docs) == 5
        assert sorted(x['status_code'] for x in docs) == [200, 200, 404, 422, 500]
        first = collection.raw.find_one({'request_id': res.headers['x-request-id']})
        assert first['client_ip'] == '203.0.113.42'
        assert first['query'] == [
            {'name': 'variable', 'value': 'WIND'},
            {'name': 'token', 'value': '[REDACTED]'},
            {'name': 'api_key', 'value': '[REDACTED]'},
        ]
        assert 'not-stored' not in str(docs) and 'other-secret' not in str(docs)
        assert first['category'] == 'data_api'
        assert 'expires_at' not in first
        assert all(x['duration_ms'] >= 0 for x in docs)
        stream = collection.raw.find_one({'path': '/stream'})
        assert stream['response_bytes'] == 7 and stream['response_complete']
        crash = collection.raw.find_one({'path': '/crash'})
        assert crash['error_type'] == 'RuntimeError' and not crash['response_complete']
        assert 'request_expiry' not in collection.raw.index_information()


def test_client_ip_behind_trusted_docker_gateway_proxy(setup):
    """Uvicorn이 172.17.0.1(Docker 브리지 게이트웨이)만 신뢰할 때의 IP 기록 검증."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app, collection = setup
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts='172.17.0.1')

    def logged_ip(client_addr, xff):
        with TestClient(wrapped, client=client_addr) as client:
            res = client.get('/api/griddata', headers={'X-Forwarded-For': xff})
            drain(client, app)
        return collection.raw.find_one({'request_id': res.headers['x-request-id']})['client_ip']

    # Nginx가 $remote_addr로 덮어쓴 값 → 실제 접속자 IP 기록
    assert logged_ip(('172.17.0.1', 50000), '198.51.100.7') == '198.51.100.7'
    # 클라이언트가 위조 헤더를 보내고 Nginx가 뒤에 덧붙인 경우에도 오른쪽(실제) 값 사용
    assert logged_ip(('172.17.0.1', 50000), '1.2.3.4, 198.51.100.7') == '198.51.100.7'
    # 신뢰하지 않는 주소에서 직접 들어온 요청의 헤더는 무시
    assert logged_ip(('203.0.113.9', 50000), '1.2.3.4') == '203.0.113.9'


def test_authentication_and_empty_state(setup, monkeypatch):
    app, _ = setup
    with TestClient(app) as client:
        assert client.get('/monitoring').status_code == 401
        assert client.get('/api/monitoring/data').status_code == 401
        assert client.get('/api/monitoring/data', auth=('operator', 'wrong')).status_code == 401
        html = client.get('/monitoring', auth=('operator', 'test-password'))
        assert html.status_code == 200 and 'API Monitoring' in html.text
        r = client.get('/api/monitoring/data?category=data_api', auth=('operator', 'test-password'))
        assert r.status_code == 200, r.text
        assert r.headers['cache-control'] == 'no-store'
        data = r.json()
        assert data['overview']['total'] == 0
        assert data['overview']['p95_ms'] is None
        assert sum(x['count'] for x in data['timeline']) == 0
        monkeypatch.delenv('MONITORING_PASSWORD')
        assert client.get('/monitoring').status_code == 503


def test_static_docs_and_redirects_are_logged(setup):
    app, collection = setup
    with TestClient(app) as client:
        assert client.get('/guide/index_en.html').status_code == 200
        assert client.get('/docs').status_code == 200
        assert client.get('/api/griddata/', follow_redirects=False).status_code == 307
        drain(client, app)
        static = collection.raw.find_one({'path':'/guide/index_en.html'})
        assert static['category'] == 'static' and static['endpoint'] == '/guide/{path}'
        assert collection.raw.find_one({'path':'/docs'})['category'] == 'page'
        redirect = collection.raw.find_one({'status_code':307})
        assert redirect['endpoint'] == '/api/griddata'


def test_aggregates_filters_pagination_and_timezone(setup):
    app, collection = setup
    now = datetime.now(timezone.utc)
    collection.raw.insert_many([{
        '_id': str(i), 'request_id': str(i), 'started_at': now-timedelta(minutes=i+1),
        'path': '/api/griddata' if i < 10 else '/api/latest',
        'endpoint': '/api/griddata' if i < 10 else '/api/latest',
        'category': 'data_api', 'client_ip': '203.0.113.1' if i < 10 else '203.0.113.2',
        'status_code': 200 if i < 18 else (422 if i == 18 else 503),
        'duration_ms': (i+1)*100, 'response_complete': True,
    } for i in range(20)])
    with TestClient(app) as client:
        auth=('operator', 'test-password')
        r=client.get('/api/monitoring/data?category=data_api', auth=auth)
        assert r.status_code == 200, r.text
        data=r.json()
        assert data['overview'] == {'total':20,'client_errors':1,'server_errors':1,'incomplete':0,'avg_ms':1050.0,'max_ms':2000,'p95_ms':1900}
        assert sum(x['count'] for x in data['timeline']) == 20
        assert sum(x['errors'] for x in data['timeline']) == 2
        assert len(data['endpoints']) == len(data['clients']) == 2
        assert data['requests'][0]['started_at'].endswith(('Z', '+00:00'))
        assert len(data['requests']) == 20
        assert client.get('/api/monitoring/data?category=data_api&page=2', auth=auth).json()['requests'] == []
        assert client.get('/api/monitoring/data?category=data_api&client_ip=203.0.113.2&status=5xx',auth=auth).json()['overview']['total']==1
        assert client.get('/api/monitoring/data?category=data_api&endpoint=griddata',auth=auth).json()['overview']['total']==10
        assert client.get('/api/monitoring/data?category=data_api&endpoint=.*',auth=auth).json()['overview']['total']==0
        assert client.get('/api/monitoring/data?hours=9999',auth=auth).status_code==422


def test_database_failure_does_not_break_original_response(setup, caplog):
    app, collection = setup
    collection.fail_writes = True
    with TestClient(app) as client:
        assert client.get('/api/griddata').status_code == 200
        drain(client, app)
        assert app.state.request_log_writer.fallback_count == 1
        assert 'REQUEST_LOG_FALLBACK' in caplog.text
        collection.fail_queries = True
        assert client.get('/api/monitoring/data',auth=('operator','test-password')).status_code == 503


def test_queue_bound_and_parameter_limit(caplog):
    writer = request_logging.RequestLogWriter()
    writer.queue = asyncio.Queue(maxsize=1)
    doc={'started_at':datetime.now(timezone.utc)}
    writer.submit(dict(doc)); writer.submit(dict(doc))
    assert writer.queue.qsize() == 1 and writer.fallback_count == 1
    assert 'REQUEST_LOG_FALLBACK' in caplog.text
    params, truncated = request_logging.safe_query(b'variable=' + b'a'*400 + b'&password=secret')
    assert truncated and len(params[0]['value'])==256 and params[1]['value']=='[REDACTED]'
