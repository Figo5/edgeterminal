import json,hashlib,re,statistics,math,copy
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
artifact=nba_model.PROP_PARAMETERS
empirical=copy.deepcopy(artifact)
for config in empirical['markets'].values():config['probability_method']='empirical'
results={}
for market in nba_model.MARKETS:
    cases=[]
    for row in datasets[market]:
        if row['ts']<nba_model_fit.stamp(nba_model_fit.CALIBRATION_END):continue
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
    results[market]=output
    print(market,json.dumps(output),flush=True)
Path('/tmp/nba_confidence_pre_cutoff_metrics.json').write_text(json.dumps(results,indent=2))
