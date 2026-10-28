import json,hashlib,re,statistics,math,copy
import numpy as np
from pathlib import Path
from collections import defaultdict
from scipy.stats import nbinom
from edgeterminal import db,nba_model,nba_prop_model,nba_sources,nba_model_fit
hist=[]
for meta in nba_model.PROP_PARAMETERS['source_catalog']:
    s=db.q1('SELECT * FROM nba_source_artifacts WHERE id=?',(meta['artifact_id'],))
    body=s['body'].encode() if isinstance(s['body'],str) else s['body']
    assert hashlib.sha256(body).hexdigest()==meta['sha256']
    pid=re.search(r'/athletes/(\d+)/',s['url']).group(1)
    hist.extend(nba_sources.player_history(json.loads(body),pid,s['sha256']))
datasets=nba_model_fit.examples(hist)
artifact=copy.deepcopy(nba_model.PROP_PARAMETERS)
for config in artifact['markets'].values():config['probability_method']='parametric'
empirical=copy.deepcopy(artifact)
for config in empirical['markets'].values():config['probability_method']='empirical'
results={}
for market in nba_model.COUNT_MARKETS:
    cases=[];crps_active=[];crps_candidate=[];ll_active=[];ll_candidate=[];mae_active=[];mae_candidate=[]
    for row in datasets[market]:
        if not nba_model_fit.stamp('2026-02-15T00:00:00Z')<=row['ts']<nba_model_fit.stamp(nba_model_fit.CALIBRATION_END):continue
        mu,mins=nba_prop_model.predict(row['rows'],row['ts'],market,artifact)
        f,_=nba_prop_model.features(row['rows'],row['ts'])
        sd=max(statistics.stdev(r['stat'] for r in row['rows']),nba_model.PROP_SD_FLOOR[market])
        line=math.floor(f['baseline'])+.5
        p=nba_prop_model.probabilities(market,mu,sd,line,artifact)[0]
        pe=nba_prop_model.probabilities(market,mu,sd,line,empirical)[0]
        actual=row['target']>line
        pg=None
        if market in nba_model.COUNT_MARKETS:
            S=sum(r['stat'] for r in row['rows']);E=sum(r['minutes'] for r in row['rows'])
            pg=float(nbinom.sf(math.floor(line),S+.5,E/(E+mins)))
            mae_active.append(abs(mu-row['target']))
            mae_candidate.append(abs((S+.5)*mins/E-row['target']))
            maximum={'rebounds':40,'assists':30,'threes':20}[market]
            thresholds=np.arange(maximum)
            truth=(row['target']<=thresholds).astype(float)
            pa=np.array([nba_prop_model.probabilities(market,mu,sd,float(k)+.5,artifact)[0] for k in thresholds])
            pc=nbinom.sf(thresholds,S+.5,E/(E+mins))
            crps_active.append(float(np.sum((1-pa-truth)**2)))
            crps_candidate.append(float(np.sum((1-pc-truth)**2)))
            ll_active.append(-math.log(max(1e-15,p if actual else 1-p)))
            ll_candidate.append(-math.log(max(1e-15,pg if actual else 1-pg)))
        cases.append(dict(mu=mu,line=line,target=row['target'],prior_n=len(row['rows']),prior_total=sum(r['stat'] for r in row['rows']),p=p,empirical=pe,gamma_poisson=pg,y=actual))
    def score(xs,key):
        return {'n':len(xs),'brier':statistics.mean((r[key]-r['y'])**2 for r in xs) if xs else None,
                'mean_selected_confidence':statistics.mean(max(r[key],1-r[key]) for r in xs) if xs else None,
                'selected_win_rate':statistics.mean((r[key]>=.5)==r['y'] for r in xs) if xs else None}
    subset=[r for r in cases if r['mu']<1e-6]
    near=[r for r in cases if r['mu']<=.25]
    extreme=[r for r in cases if max(r['p'],1-r['p'])>=.95]
    output={'active':score(cases,'p'),'all_empirical':score(cases,'empirical'),
            'zero_mu':{'active':score(subset,'p'),'empirical':score(subset,'empirical'),
                'following_nonzero':sum(r['target']>0 for r in subset)},
            'near_zero_mu':{'active':score(near,'p'),'empirical':score(near,'empirical')},
            'extreme_confidence':score(extreme,'p')}
    if market in nba_model.COUNT_MARKETS:
        output['gamma_poisson']=score(cases,'gamma_poisson');output['zero_mu']['gamma_poisson']=score(subset,'gamma_poisson');output['near_zero_mu']['gamma_poisson']=score(near,'gamma_poisson')
    output['mae']={'parametric_active_mean':statistics.mean(mae_active),'gamma_poisson_mean':statistics.mean(mae_candidate)}
    output['crps']={'active':statistics.mean(crps_active),'gamma_poisson':statistics.mean(crps_candidate)}
    output['synthetic_logloss']={'active':statistics.mean(ll_active),'gamma_poisson':statistics.mean(ll_candidate)}
    results[market]=output
    print(market,json.dumps(output),flush=True)
Path('/tmp/nba_confidence_february_metrics.json').write_text(json.dumps(results,indent=2))
