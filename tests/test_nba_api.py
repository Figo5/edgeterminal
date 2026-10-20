import pytest
from fastapi.testclient import TestClient

from edgeterminal import db, nba_ops, nba_sources, visit_refresh
from edgeterminal.api import app
from edgeterminal.config import settings


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    if db._conn is not None:
        db._conn.close();db._conn=None
    monkeypatch.setattr(settings,'database_url',f'sqlite:///{tmp_path / "nba-api.db"}')
    db.init();nba_ops.ensure_schema()
    monkeypatch.setattr(nba_sources,'fetch',lambda *a,**kw:pytest.fail('GET fetched a provider'))
    yield
    db._conn.close();db._conn=None


def test_nba_reads_are_local_validated_and_cannot_trigger_network(isolated):
    client=TestClient(app)
    assert client.get('/api/product/nba?date=2026-10-20').json()['sport']=='NBA'
    assert client.get('/api/product/nba?date=2026-40-99').status_code==422
    assert client.get('/api/product/nba/nfl-2026-other').status_code==404
    assert client.get('/api/product/nba/performance').json()['props']['pending']==0
    assert client.get('/api/product/nba/status').headers['Cache-Control']=='no-store'


def test_nba_check_uses_same_worker_throttle_and_origin_boundary(isolated,monkeypatch):
    calls=[]
    monkeypatch.setattr(visit_refresh,'check',lambda: calls.append(1) or {'ok':True,'status':'cached'})
    client=TestClient(app)
    assert client.post('/api/product/nba/check',headers={'Origin':'https://attacker.example'}).status_code==403
    assert client.post('/api/product/nba/check').json()['ok']
    assert len(calls)==1


def test_hosted_nba_preview_hides_future_props_and_private_refresh(isolated,monkeypatch):
    monkeypatch.setattr(settings,'hosted_mode',True)
    monkeypatch.setattr(settings,'public_url','https://edge.example')
    monkeypatch.setattr(nba_ops,'board',lambda day=None:{'games':[{'id':'nba-2026-401909090',
        'kickoff_utc':'2099-10-20T23:00Z','top_props':[{'player':'private player'}]}]})
    client=TestClient(app,base_url='https://edge.example')
    response=client.get('/api/product/nba')
    assert response.status_code==200 and response.json()['games'][0]['top_props']==[]
    assert 'private player' not in response.text
    assert client.post('/api/product/nba/check').status_code==403
    assert client.post('/api/research/refresh',headers={'Origin':settings.public_url}).status_code==403
