#!/usr/bin/env python3
"""Retrospective multi-season score evaluation, never a model activation."""
from datetime import datetime,timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
import sys

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from edgeterminal import nba_spread_data as data,nba_spread_learning as learning,nba_spread_model as model

HERE=Path(__file__).resolve().parent
ARCHIVE=ROOT/'data/research/nba_spread/bootstrap-2026-10-29'
stamp=lambda v:datetime.fromisoformat(v.replace('Z','+00:00')).timestamp()
OBSERVATION_CUTOFF=stamp('2026-10-30T00:00:00Z')
FIT_CUTOFF=stamp('2025-10-01T00:00:00Z')
ASSESSMENT_END=stamp('2026-10-01T00:00:00Z')


def run():
    manifest=json.loads((ARCHIVE/'manifest.json').read_text());catalog=[];history=[]
    assert manifest['requests']==60 and not manifest['errors']
    for i,item in enumerate(manifest['records']):
        source=dict(item,id=-(i+1),body=(ARCHIVE/item['file']).read_bytes())
        history.extend(data.source_history(source,cutoff=OBSERVATION_CUTOFF))
        catalog.append({k:item[k] for k in ('url','received_at','sha256','team','year')})
    con=sqlite3.connect((ROOT/'data/edgeterminal.db').as_uri()+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
    sources=con.execute("SELECT * FROM nba_source_artifacts WHERE id IN (SELECT MAX(id) FROM nba_source_artifacts "
                        "WHERE url LIKE '%/schedule?season=2026&seasontype=2' AND received_at<? AND http_status=200 GROUP BY url)",
                        (OBSERVATION_CUTOFF,)).fetchall()
    assert len(sources)==30
    for source in sources:
        source=dict(source);history.extend(data.source_history(source,cutoff=OBSERVATION_CUTOFF))
        catalog.append({k:source[k] for k in ('id','url','received_at','sha256')})
    con.close()
    unique={}
    for row in history:
        key=(row['team'],row['game_id']);signature=lambda r:tuple(r.get(k) for k in ('played_at','points','opponent_points','opponent','home'))
        if key in unique:assert signature(row)==signature(unique[key])
        unique[key]=row
    history=list(unique.values())
    print(f'Verified {len(history)} owner-only historical rows / {len(catalog)} schedules.',flush=True)
    artifact=learning.fit_candidate(history,FIT_CUTOFF)
    print('Fit complete with two historical seasons; computing held-year forecasts.',flush=True)
    examples=learning.examples(history,ASSESSMENT_END)
    rows=[]
    for example in examples:
        if example['ts']<FIT_CUTOFF:continue
        value=example['baseline_margin']+artifact['intercept']+sum(c*(v-m)/s for c,v,m,s in
            zip(artifact['coefficients'],example['values'],artifact['means'],artifact['scales']))
        candidate=round(value,2)
        row=dict(example,candidate_margin=candidate,
            baseline_probability=round(model.baseline.norm_cdf(example['baseline_margin']/model.baseline.SD_MARGIN),4),
            candidate_probability=round(model.baseline.norm_cdf(candidate/artifact['sd_margin']),4))
        rows.append(row)
    def metrics(sample):
        out=dict(n=len(sample))
        for label in ('baseline','candidate'):
            errors=[r[label+'_margin']-r['actual_margin'] for r in sample]
            out[label]=dict(margin_mae=statistics.mean(abs(e) for e in errors),
                margin_rmse=math.sqrt(statistics.mean(e*e for e in errors)),
                winner_brier=statistics.mean((r[label+'_probability']-(r['actual_margin']>0))**2 for r in sample))
        return out
    slices={'entire_2025_26':rows,'early_season_oct_dec':[r for r in rows if r['ts']<stamp('2026-01-01T00:00:00Z')],
            'january_february':[r for r in rows if stamp('2026-01-01T00:00:00Z')<=r['ts']<stamp('2026-03-01T00:00:00Z')],
            'march_april_previously_examined':[r for r in rows if r['ts']>=stamp('2026-03-01T00:00:00Z')]}
    result=dict(scope='Retrospective score reconstruction only; no historical ATS or activation claim',
        source_catalog=catalog,owner_rows=len(history),sources=len(catalog),artifact=artifact,
        metrics={name:metrics(sample) for name,sample in slices.items()},assessment_forecasts=rows,
        protocol_sha256=hashlib.sha256((HERE/'protocol.md').read_bytes()).hexdigest(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (HERE/'evaluation.json').write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    print(json.dumps(result['metrics'],indent=2),flush=True)


if __name__=='__main__':run()
