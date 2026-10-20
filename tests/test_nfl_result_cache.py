"""Offline result feeds are shared without inventing newer source receipts."""
import pytest
import httpx

from edgeterminal import db, nfl_result_cache
from test_research_refresh import isolated

URL='https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=2026&seasontype=2&week=7&limit=100'


def test_models_share_one_public_read_until_five_minute_expiry(isolated):
    calls=[]
    def fetch(url):
        calls.append(url)
        return b'{"events":[]}'
    assert nfl_result_cache.read(URL,fetch,now=1000)==(b'{"events":[]}',1000)
    assert nfl_result_cache.read(URL,fetch,now=1299)==(b'{"events":[]}',1000)
    nfl_result_cache.read(URL,fetch,now=1300)
    assert len(calls)==2


def test_failed_result_source_retries_after_five_minutes(isolated):
    calls=[]
    def fail(url):
        calls.append(url)
        raise ValueError('source offline')
    with pytest.raises(ValueError,match='offline'):
        nfl_result_cache.read(URL,fail,now=1000)
    with pytest.raises(ValueError,match='scheduled retry'):
        nfl_result_cache.read(URL,fail,now=1299)
    with pytest.raises(ValueError,match='offline'):
        nfl_result_cache.read(URL,fail,now=1300)
    assert len(calls)==2


def test_result_cache_refuses_untrusted_urls_and_corrupt_source_bytes(isolated):
    with pytest.raises(ValueError,match='Unsupported'):
        nfl_result_cache.read('https://evil.example/results',lambda u:pytest.fail('untrusted request'),now=1000)
    nfl_result_cache.read(URL,lambda u:b'{"events":[]}',now=1000)
    key=nfl_result_cache._key(URL)
    cached=db.kv_get(key);cached['sha256']='a'*64;db.kv_set(key,cached)
    with pytest.raises(ValueError,match='hash mismatch'):
        nfl_result_cache.read(URL,lambda u:pytest.fail('corrupt cache bypassed'),now=1001)


def test_official_participation_sources_refresh_at_five_minutes(isolated,monkeypatch):
    from edgeterminal import nfl_official_participation as official
    calls=[]
    def response(request):
        calls.append(str(request.url))
        return httpx.Response(200,content=b'verified public source page')
    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        monkeypatch.setattr(official,'client',lambda:http)
        url='https://www.nfl.com/games/fixture'
        official.fetch(url,1000)
        official.fetch(url,1299)
        assert len(calls)==1
        official.fetch(url,1300)
        assert len(calls)==2
