"""Share five-minute public result reads across NFL research models and freezes."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import db

TTL = 300


def _url(url):
    parsed=urlsplit(url)
    pairs=parse_qsl(parsed.query,keep_blank_values=True)
    scoreboard=(parsed.hostname=='site.api.espn.com' and
        parsed.path=='/apis/site/v2/sports/football/nfl/scoreboard' and
        len(dict(pairs))==len(pairs) and
        all(k in {'dates','seasontype','week','limit'} and v.isdigit() for k,v in pairs))
    stats=(parsed.hostname=='github.com' and not pairs and re.fullmatch(
        r'/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_\d{4}\.csv',parsed.path))
    if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.fragment or
            parsed.port not in (None,443) or not (scoreboard or stats)):
        raise ValueError('Unsupported NFL result source URL')
    return urlunsplit((parsed.scheme,parsed.netloc,parsed.path,urlencode(sorted(pairs)),''))


def _key(url):
    return 'nfl_result_feed:'+hashlib.sha256(_url(url).encode()).hexdigest()


def read(url,fetcher,*,now):
    """Return exact bytes and their original as-of time; failed reads also cool down."""
    canonical=_url(url);key=_key(canonical)
    cached=db.kv_get(key) or {}
    if 0<=now-cached.get('retrieved_at',-TTL)<TTL:
        if not cached.get('ok'):
            raise ValueError('NFL result source unavailable; awaiting scheduled retry')
        row=db.q1('SELECT payload FROM nfl_prop_grade_source_artifacts WHERE sha256=?',(cached['sha256'],))
        raw=bytes(row['payload']) if row else b''
        if hashlib.sha256(raw).hexdigest()!=cached['sha256']:
            raise ValueError('NFL result cache hash mismatch')
        return raw,cached['retrieved_at']
    try:
        raw=fetcher(url)
        if not isinstance(raw,bytes) or not raw or len(raw)>50*1024*1024:
            raise ValueError('NFL result source is empty or exceeds size limit')
        if 'site.api.espn.com'==urlsplit(canonical).hostname:
            data=json.loads(raw)
            if not isinstance(data,dict) or not isinstance(data.get('events'),list):
                raise ValueError('NFL result scoreboard lacks events')
        else:
            fields=csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))).fieldnames or []
            if not {'game_id','player_id','season','week','season_type','team','opponent_team'}<=set(fields):
                raise ValueError('NFL result stats lack identity columns')
        digest=hashlib.sha256(raw).hexdigest()
        db.ex('INSERT OR IGNORE INTO nfl_prop_grade_source_artifacts VALUES(?,?,?,?)',
              (digest,url,now,raw))
        db.kv_set(key,{'ok':True,'sha256':digest,'retrieved_at':now})
        return raw,now
    except Exception:
        db.kv_set(key,{'ok':False,'retrieved_at':now})
        raise
