"""Synthetic power check for the fixed game/date validation gates.

This experiment reads no research data and cannot train, fetch, register or
activate models. It estimates sensitivity under stated assumptions, rather
than estimating the performance of an EdgeTerminal model.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics

import numpy as np


def simulate(*, games, trials=2000, seed=20261030, baseline=.76, candidate=.64, truth=.55):
    rng=np.random.default_rng(seed)
    accepted=0
    expected=(baseline-truth)**2-(candidate-truth)**2
    for _ in range(trials):
        days=math.ceil(games/5)
        date_truth=np.clip(truth+rng.normal(0.,.08,days),.05,.95)
        game_truth=np.clip(date_truth[np.arange(games)//5]+rng.normal(0.,.10,games),.01,.99)
        y=(rng.random((games,5))<game_truth[:,None]).astype(float)
        effects=np.mean((candidate-y)**2-(baseline-y)**2,axis=1)
        date_effects=[float(np.mean(effects[i:i+5])) for i in range(0,games,5)]
        game_upper=float(np.mean(effects))+1.96*float(np.std(effects,ddof=1))/math.sqrt(games)
        date_upper=statistics.mean(date_effects)+1.96*statistics.stdev(date_effects)/math.sqrt(days)
        accepted+=games>=30 and games*5>=100 and days>=7 and float(np.mean(effects))<=-.002 \
            and game_upper<0 and date_upper<0
    return dict(games=games,dates=math.ceil(games/5),trials=trials,seed=seed,
                baseline_probability=baseline,candidate_probability=candidate,true_mean=truth,
                theoretical_brier_gain=expected,pass_fraction=accepted/trials,
                note='Synthetic probability-gate sensitivity only; all source, market and policy gates remain required.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trials',type=int,default=2000)
    args=parser.parse_args()
    if not 2<=args.trials<=100000:parser.error('trials must be between 2 and 100000')
    scenarios={'large_calibration_gain':(.76,.64,.55),
               'modest_calibration_gain':(.70,.65,.55),
               'null':(.64,.64,.55),'regression':(.64,.76,.55)}
    print(json.dumps({name:[simulate(games=n,trials=args.trials,baseline=b,candidate=c,truth=t)
                           for n in (40,80,160)] for name,(b,c,t) in scenarios.items()},indent=2))
