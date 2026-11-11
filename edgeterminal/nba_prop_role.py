"""Workload-aware NBA proposals requiring full contemporaneous policy evidence."""
import copy
import hashlib
import statistics
from collections import defaultdict

from . import nba_model as baseline, nba_prop_features as workload, nba_prop_model, prop_calibration
from . import nba_prop_quality as quality, nba_prop_opponent as opponent

KIND = 'nba-role-policy-v2'
FAMILY = 'nba-props-role-policy-v2'
# Rate variant: same full-offer policy gates, but the production rate chooses a
# longer history window and an opponent-allowance strength instead of recent form.
RATE_KIND = 'nba-rate-policy-v1'
RATE_FAMILY = 'nba-props-rate-policy-v1'
KINDS = (KIND, RATE_KIND)
FAMILIES = {KIND: FAMILY, RATE_KIND: RATE_FAMILY}
RATE_WINDOWS = (15, 25, 40)
# The Nov 4-10 2026 selections were inspected before this variant was declared,
# so its untouched validation games must tip off on or after Nov 12 (Eastern).
RATE_DECLARED_AT = baseline.parse_time('2026-11-12T05:00:00Z')


def validate(artifact):
    from . import nba_learning
    if artifact.get('schema') != 2 or artifact.get('kind') not in KINDS or artifact.get('ranking_changed') is not True:
        raise ValueError('invalid NBA role-policy artifact')
    nba_learning._number(artifact['training_cutoff'])
    if quality.selector_hash(artifact.get('retained_artifact')) != artifact.get('selector_sha256'):
        raise ValueError('NBA retained role-selector binding mismatch')
    retained = artifact.get('retained_artifact')
    if retained is not None:
        nba_learning._validate_artifact(retained)
    if set(artifact.get('role_markets', [])) != set(artifact.get('changed_markets', [])) \
            or not set(artifact['role_markets']).issubset(set(baseline.MARKETS)):
        raise ValueError('invalid NBA role market selection')
    fitted = artifact['role_model']
    if set(fitted.get('markets', {})) != set(baseline.MARKETS):
        raise ValueError('NBA role production market catalog incomplete')
    minute = fitted['minutes']
    if minute.get('feature_names') != list(workload.MINUTE_FEATURES) or len(minute['minute_residuals']) < 30:
        raise ValueError('invalid NBA role minute model')
    for key in ('intercept',):
        nba_learning._number(minute['minutes_model'][key])
    if len(minute['minutes_model']['coefficients']) != len(workload.MINUTE_FEATURES):
        raise ValueError('invalid NBA role minute coefficient count')
    for v in minute['minutes_model']['coefficients'] + minute['minute_residuals']:
        nba_learning._number(v)
    if minute['minute_residuals'] != sorted(minute['minute_residuals']):
        raise ValueError('invalid NBA role minute residual order')
    for market in baseline.MARKETS:
        config = fitted['markets'][market]
        if config['recent_weight'] not in nba_learning.RECENT_WEIGHTS or len(config['standardized_residuals']) < 30:
            raise ValueError('invalid NBA role production parameters')
        rate = (config.get('window'), config.get('opponent_strength'))
        if rate != (None, None) if artifact['kind'] == KIND else (
                rate[0] not in RATE_WINDOWS or rate[1] not in opponent.STRENGTHS or config['recent_weight'] != 0.):
            raise ValueError('invalid NBA rate window or opponent strength')
        for v in config['standardized_residuals']:
            nba_learning._number(v)
        if config['standardized_residuals'] != sorted(config['standardized_residuals']):
            raise ValueError('invalid NBA role production residual order')
        prop_calibration.apply_calibrator({'over': .5, 'under': .5, 'push': 0.}, artifact['calibrators'][market])
    return artifact


def _rows(history, player_id, market, tip, window):
    """Up to `window` eligible rows, by repeating the frozen 15-row reader further back."""
    rows, conflicts = baseline._player_rows(history, str(player_id), market, tip)
    while rows and len(rows) < window:
        earlier, more = baseline._player_rows(history, str(player_id), market,
                                              rows[0]['ts'] - .001 + baseline.FEATURE_LAG_S)
        earlier = [r for r in earlier if r['ts'] < rows[0]['ts']]
        if not earlier:
            break
        rows, conflicts = earlier + rows, max(conflicts, more)
    return rows[-window:], conflicts


def _factor(history, player_id, market, config):
    """Opponent multiplier from the game's archived context row; unknown context changes nothing."""
    marker = next((r for r in history or [] if isinstance(r, dict) and r.get('kind') == opponent.CONTEXT_KIND), None)
    team = marker['teams'].get(str(player_id)) if marker else None
    ratio = marker['ratios'].get(team, {}).get(market) if team else None
    return opponent.adjust(1., ratio, config.get('opponent_strength', 0.))


def _prediction(rows, tipoff, market, fitted, uncertainty=True, factor=1.):
    """Workload and volatility use the latest 15 rows; a rate variant may pool a longer window."""
    from . import nba_learning
    recent = rows[-baseline.PROP_WINDOW:]
    dist = (workload.minute_distribution(recent, tipoff, fitted['minutes']) if uncertainty else
            [(workload.predict_minutes(recent, tipoff, fitted['minutes']['minutes_model']), 1.)])
    config = fitted['markets'][market]
    events, exposure, _ = nba_learning._production(rows, tipoff, config['recent_weight'])
    exposure /= factor
    shape = events + .5 if market in baseline.COUNT_MARKETS else events
    minutes = sum(m * w for m, w in dist)
    mean = shape * minutes / exposure
    sd = max(statistics.stdev(r['stat'] for r in recent), baseline.PROP_SD_FLOOR[market])
    return mean, minutes, sd, shape, exposure, dist


def _point(player_id, market, history, tipoff, line, artifact, info, calibrate=True):
    from . import nba_learning
    if market not in artifact['role_markets']:
        retained = artifact['retained_artifact']
        return (nba_learning._project(player_id, market, history, tipoff, line, retained, info)
                if retained is not None else baseline.project_prop(player_id, market, history, tipoff, line))
    tip = baseline.parse_time(tipoff)
    config = artifact['role_model']['markets'][market]
    rows, conflicts = _rows(history, player_id, market, tip, config.get('window', baseline.PROP_WINDOW))
    if len(rows) < baseline.MIN_PROP_GAMES:
        return dict(status='unavailable', reason='insufficient_games')
    mean, minutes, sd, shape, exposure, dist = _prediction(rows, tip, market, artifact['role_model'],
                                                           factor=_factor(history, player_id, market, config))
    minutes_sd = statistics.stdev(r['minutes'] for r in rows[-baseline.PROP_WINDOW:])
    out = dict(status='ok', player_id=str(player_id), market=market, tipoff=tip, n=len(rows),
        raw_projection=mean, projection=round(mean, 2), raw_sd=sd, sd=round(sd, 2),
        projected_minutes=round(minutes, 1), minutes_sd=round(minutes_sd, 2),
        minutes_flag='volatile' if minutes_sd / minutes > baseline.MINUTES_CV_FLAG else 'stable',
        point_method='role_uncertain_opportunity', probability_method='role_minutes_predictive_mixture',
        calibration_n=artifact['role_model']['markets'][market]['calibration_n'], input_conflicts=conflicts,
        model_version=info['version'], parameter_fingerprint=info['fingerprint'],
        probability_over=None, probability_under=None, probability_push=None,
        readiness='RESEARCH_ONLY', designation='PASS',
        limits=['Observed minutes proxy role; no assumed lineup or injury impact',
                'Player-rate and future-minutes uncertainty are distinct for count markets'])
    if line is not None:
        if market in baseline.COUNT_MARKETS:
            mass = [nba_learning.gamma_poisson_probabilities(shape, exposure, m, line) for m, _ in dist]
            probs = dict(zip(('over', 'under', 'push'), [sum(w * p[i] for p, (_, w) in zip(mass, dist)) for i in range(3)]))
        else:
            values = nba_prop_model.probabilities(market, mean, sd, line,
                {'markets': {market: artifact['role_model']['markets'][market]}})
            probs = dict(zip(('over', 'under', 'push'), values))
        if calibrate:
            probs = prop_calibration.apply_calibrator(probs, artifact['calibrators'][market])
        out.update({'probability_' + k: round(v, 4) for k, v in probs.items()})
        out['line'] = line
    return out


def project(player_id, market, history, tipoff, line, artifact, info):
    validate(artifact)
    return _point(player_id, market, history, tipoff, line, artifact, info)


def raw_project(player_id, market, history, tipoff, line, artifact, info):
    from . import nba_learning
    if artifact is None:
        return baseline.project_prop(player_id, market, history, tipoff, line)
    if artifact.get('kind') == quality.KIND:
        return quality.point_project(player_id, market, history, tipoff, line, artifact['base_artifact'])
    if artifact.get('kind') == KIND:
        if market in artifact['role_markets']:
            return _point(player_id, market, history, tipoff, line, artifact, info, False)
        return raw_project(player_id, market, history, tipoff, line, artifact['retained_artifact'], info)
    return nba_learning._project(player_id, market, history, tipoff, line, artifact, info)


def select(current, quotes, history, tipoff, now, injuries, count):
    from . import nba_learning
    artifact = validate(current['artifact'])
    info = nba_learning._info(current)
    groups, excluded = {}, []
    for quote in quotes:
        guard = baseline.select_props([quote], history, tipoff, now, injuries, count=1)
        excluded.extend(guard['excluded'])
        if not guard['candidates']:
            continue
        row = dict(guard['candidates'][0])
        raw = raw_project(row['player_id'], row['market'], history, tipoff, row['line'], artifact, info)
        if raw['status'] != 'ok':
            raise ValueError('NBA role scorer lost an otherwise supported market')
        p = raw['probability_' + row['side']]
        row.update(projection=raw['projection'], sd=raw['sd'], projected_minutes=raw['projected_minutes'],
            probability=p, probability_push=raw['probability_push'], model_version=info['version'],
            parameter_fingerprint=info['fingerprint'])
        row['flags'] = [flag for flag in row['flags'] if flag != 'minutes_volatile']
        if raw['minutes_flag'] == 'volatile':
            row['flags'].append('minutes_volatile')
        key = (row['game_id'], row['player_id'], row['market'])
        order = (-p, -nba_learning.american_to_decimal(row['price']), str(row['sportsbook']), row['side'])
        if key not in groups or order < groups[key][0]:
            groups[key] = (order, row)
    candidates = sorted([v[1] for v in groups.values()], key=lambda r: (-r['probability'], r['player_id'], r['market']))
    selected, rest, counts = [], [], defaultdict(int)
    for row in candidates:
        if len(selected) < count and counts[row['player_id']] < baseline.MAX_MARKETS_PER_PLAYER:
            selected.append(row); counts[row['player_id']] += 1
        else:
            rest.append(row)
    selected.extend(rest[:max(0, count - len(selected))])
    for row in candidates:
        p = project(row['player_id'], row['market'], history, tipoff, row['line'], artifact, info)
        row.update(raw_policy_probability=row['probability'], probability=p['probability_' + row['side']],
                   probability_push=p['probability_push'], probability_method=p['probability_method'],
                   point_method=p['point_method'], calibration_n=p['calibration_n'],
                   edge=round(p['probability_' + row['side']] - nba_learning.american_to_prob(row['price']), 4))
    okay = len(selected) == count
    return dict(status='ok' if okay else 'wait', selections=selected if okay else [], candidates=candidates,
        excluded=excluded, eligible_candidates=len(candidates), required=count, tipoff=baseline.parse_time(tipoff),
        generated_at=baseline.parse_time(now), model_version=info['version'], parameter_fingerprint=info['fingerprint'],
        designation='PASS', readiness='RESEARCH_ONLY')


def fit_role(datasets, cutoff):
    from . import nba_learning
    fit, dev, cal = nba_learning.split_windows(datasets)
    minutes = workload.fit_minutes(fit['points'], cal['points'])
    fitted = dict(minutes=minutes, markets={})
    for market in baseline.MARKETS:
        if min(len(dev[market]), len(cal[market])) < 30:
            raise ValueError('insufficient NBA role development/calibration rows')
        scores = []
        for weight in nba_learning.RECENT_WEIGHTS:
            fitted['markets'][market] = dict(recent_weight=weight)
            error = statistics.mean(abs(r['target'] - _prediction(r['rows'], r['ts'], market, fitted, False)[0]) for r in dev[market])
            scores.append((error, weight))
        chosen = min(scores)[1]
        fitted['markets'][market] = dict(recent_weight=chosen)
        residuals = sorted((r['target'] - _prediction(r['rows'], r['ts'], market, fitted)[0]) /
                           _prediction(r['rows'], r['ts'], market, fitted)[2] for r in cal[market])
        fitted['markets'][market].update(standardized_residuals=residuals, probability_method='empirical',
            calibration_n=len(residuals), development=[dict(mae=e, weight=w) for e, w in scores])
    return fitted, _training(fit, dev, cal)


def _training(fit, dev, cal):
    return dict(fit_game_ids=sorted({r['game_id'] for r in fit['points']}),
        development_game_ids=sorted({r['game_id'] for r in dev['points']}),
        calibration_game_ids=sorted({r['game_id'] for r in cal['points']}),
        fit_label_max_at=max(r['ts'] + 14400 for r in fit['points'] + dev['points']),
        development_label_max_at=max(r['ts'] + 14400 for r in dev['points']),
        calibration_label_max_at=max(r['ts'] + 14400 for r in cal['points']))


def fit_rate(history, cutoff, excluded_games=()):
    """Choose each market's rate window and opponent strength by development MAE only."""
    from . import nba_learning
    datasets = nba_learning.examples(history, cutoff, excluded_games=excluded_games, window=max(RATE_WINDOWS))
    table = opponent.build(history)
    fit, dev, cal = nba_learning.split_windows(datasets)
    latest = lambda rows: [dict(r, rows=r['rows'][-baseline.PROP_WINDOW:]) for r in rows]
    fitted = dict(minutes=workload.fit_minutes(latest(fit['points']), latest(cal['points'])), markets={})

    def predict(row, market, uncertainty=True):
        config = fitted['markets'][market]
        other = (opponent.opposing(table, row['game_id'], row['team'])
                 or opponent.opposing(table, row['game_id'].rsplit('-', 1)[-1], row['team']))
        ratio = opponent.ratio(table, other, market, row['ts']) if other else None
        return _prediction(row['rows'][-config['window']:], row['ts'], market, fitted, uncertainty,
                           opponent.adjust(1., ratio, config['opponent_strength']))

    for market in baseline.MARKETS:
        if min(len(dev[market]), len(cal[market])) < 30:
            raise ValueError('insufficient NBA rate development/calibration rows')
        scores = []
        for window in RATE_WINDOWS:
            for strength in opponent.STRENGTHS:
                fitted['markets'][market] = dict(recent_weight=0., window=window, opponent_strength=strength)
                error = statistics.mean(abs(r['target'] - predict(r, market, False)[0]) for r in dev[market])
                scores.append((error, window, strength))
        _, window, strength = min(scores)  # ties keep the shorter window and weaker adjustment
        fitted['markets'][market] = dict(recent_weight=0., window=window, opponent_strength=strength)
        residuals = sorted((r['target'] - p[0]) / p[2] for r, p in ((r, predict(r, market)) for r in cal[market]))
        fitted['markets'][market].update(standardized_residuals=residuals, probability_method='empirical',
            calibration_n=len(residuals),
            development=[dict(mae=e, window=w, opponent_strength=o) for e, w, o in scores])
    return fitted, _training(fit, dev, cal)


def _actual(context, quote):
    player = context['players'].get(str(quote['player_id']))
    if not player or player['status'] != 'played' or not player.get('stats'):
        return None
    stats = player['stats']
    return sum(stats[k] for k in ('points', 'rebounds', 'assists')) if quote['market'] == 'pra' else stats.get(quote['market'])


def _record(context, quote, before, after):
    actual = _actual(context, quote)
    if actual is None:
        raise ValueError('NBA common policy cohort has unknown selected participation/stat')
    return dict(game_id=context['game']['game_id'], player_id=str(quote['player_id']), market=quote['market'],
        quote_id=quote['quote_id'], line=quote['line'], side=quote['side'], observed_at=quote['observed_at'],
        kickoff_at=context['game']['tipoff_ts'], result_observed_at=context['final_source']['received_at'], actual=actual,
        policy_context_id=context['context_id'], result_source_artifact_id=context['final_source']['id'],
        baseline_prediction=before['raw_projection'], candidate_prediction=after['raw_projection'],
        baseline_probability=quality.conditional_probability(before, quote['side']),
        candidate_probability=quality.conditional_probability(after, quote['side']))


def build(now, kind=KIND, contexts=None):
    from . import nba_learning, nba_policy_evidence as evidence, model_learning_store
    contexts = evidence.ready_contexts(now, 160) if contexts is None else contexts
    current = nba_learning._active()
    info = nba_learning.model_info()
    contexts = [c for c in contexts if c['payload']['selector']['version'] == info['version']
                and c['payload']['selector']['fingerprint'] == info['fingerprint']]
    incomplete = sum(len(c['payload']['incumbent']) != 5 for c in contexts)
    # Eligibility was known before tipoff. Retain incomplete captures in the
    # archive, but do not reinterpret them as an incumbent five after results.
    contexts = [c for c in contexts if len(c['payload']['incumbent']) == 5]
    if len(contexts) < 100:
        return dict(status='waiting', sport='NBA', reason='full_same_selector_policy_contexts_required',
                    available_contexts=len(contexts), required_contexts=100,
                    incomplete_pretip_contexts=incomplete)
    pseudo = [dict(kickoff_at=c['game']['tipoff_ts'], game_id=c['game']['game_id'], context=c) for c in contexts]
    inner_a, inner_b, outer_rows = quality._partition(pseudo)
    if min(len(inner_a), len(inner_b)) < 30:
        return dict(status='waiting', sport='NBA', reason='separate_thirty_game_role_choice_and_calibration_blocks_required')
    choice_contexts, calibration_contexts, outer = ([r['context'] for r in group] for group in (inner_a, inner_b, outer_rows))
    if kind == RATE_KIND and min(c['game']['tipoff_ts'] for c in outer) < RATE_DECLARED_AT:
        return dict(status='waiting', sport='NBA', reason='rate_policy_validation_must_follow_its_declaration',
                    validation_games=sum(c['game']['tipoff_ts'] >= RATE_DECLARED_AT for c in contexts))
    if not model_learning_store.holdout_available('NBA', FAMILIES[kind], [c['game']['game_id'] for c in outer]):
        return dict(status='waiting', sport='NBA', reason='fresh_untouched_role_policy_holdout_required')
    cutoff = min(c['payload']['observed_at'] for c in choice_contexts) - 1
    choice_cutoff = min(c['payload']['observed_at'] for c in calibration_contexts) - 1
    calibration_cutoff = min(c['payload']['observed_at'] for c in outer) - 1
    if max(c['final_source']['received_at'] for c in choice_contexts) >= choice_cutoff \
            or max(c['final_source']['received_at'] for c in calibration_contexts) >= calibration_cutoff:
        return dict(status='waiting', sport='NBA', reason='role_policy_label_blocks_overlap')
    history, training_sources = nba_learning.training_history(cutoff)
    excluded = {c['game']['game_id'] for c in contexts}
    try:
        fitted, training = (fit_role(nba_learning.examples(history, cutoff, excluded_games=excluded), cutoff)
                            if kind == KIND else fit_rate(history, cutoff, excluded))
    except ValueError as exc:
        return dict(status='waiting', sport='NBA', reason=str(exc))
    retained = copy.deepcopy(current['artifact']) if current else None
    identity = prop_calibration.fit_calibrator([], cutoff=calibration_cutoff)
    artifact = dict(schema=2, kind=kind, evaluation_family=FAMILIES[kind], quality_protocol=quality.QUALITY_PROTOCOL,
        training_cutoff=cutoff, retained_artifact=retained, selector_sha256=quality.selector_hash(retained),
        ranking_changed=True, probability_contract='conditional_side_given_no_push',
        role_model=fitted, role_markets=list(baseline.MARKETS), changed_markets=list(baseline.MARKETS),
        calibrators={m: identity for m in baseline.MARKETS}, training=training)
    proposal_info = dict(version='nba-role-unregistered', fingerprint='unregistered')
    paired_inner = []
    for context in choice_contexts:
        evidence.replay_incumbent(context)
        used = set()
        for q in context['payload']['quotes']:
            key = (q['player_id'], q['market'])
            if key in used or _actual(context, q) is None:
                continue
            before = (nba_learning._project(q['player_id'], q['market'], context['history'], context['game']['tipoff_utc'],
                      q['line'], retained, info) if retained is not None else
                      baseline.project_prop(q['player_id'], q['market'], context['history'], context['game']['tipoff_utc'], q['line']))
            after = project(q['player_id'], q['market'], context['history'], context['game']['tipoff_utc'], q['line'], artifact, proposal_info)
            if before['status'] != 'ok' or after['status'] != 'ok':
                continue
            used.add(key); paired_inner.append(_record(context, q, before, after))
    choice = prop_calibration.choose_market_updates(paired_inner, baseline.MARKETS, cutoff=choice_cutoff)
    if not choice['changed_markets']:
        return dict(status='waiting', sport='NBA', reason='no_role_market_earned_inner_selection', market_selection=choice)
    artifact.update(role_markets=choice['changed_markets'], changed_markets=choice['changed_markets'], market_selection=choice)
    samples = defaultdict(list)
    for context in calibration_contexts:
        evidence.replay_incumbent(context)
        selected = select(dict(artifact=artifact, **proposal_info, training_cutoff=cutoff), context['payload']['quotes'],
            context['history'], context['game']['tipoff_utc'], context['as_of'],
            [dict(player_id=r['player_id'], status=r.get('availability', 'unknown')) for r in context['roster']], 5)
        if selected['status'] != 'ok':
            return dict(status='waiting', sport='NBA', reason='common_role_policy_calibration_cohort_incomplete')
        for row in selected['selections']:
            q = next(q for q in context['payload']['quotes'] if q['player_id'] == row['player_id'] and q['market'] == row['market']
                     and q['side'] == row['side'] and q['line'] == row['line'] and q['sportsbook'] == row['sportsbook'])
            actual = _actual(context, q)
            if actual is None:
                return dict(status='waiting', sport='NBA', reason='common_role_policy_calibration_participation_unknown')
            p = _point(q['player_id'], q['market'], context['history'], context['game']['tipoff_utc'], q['line'], artifact, proposal_info, False)
            probs = quality.coherent_probabilities({s: p['probability_' + s] for s in ('over', 'under', 'push')})
            samples[q['market']].append(dict(game_id=q['game_id'], quote_id=q['quote_id'], actual=actual, line=q['line'],
                result_observed_at=context['final_source']['received_at'], **{'probability_' + s: v for s, v in probs.items()}))
    artifact['calibrators'] = {m: prop_calibration.fit_calibrator(samples[m], cutoff=calibration_cutoff) for m in baseline.MARKETS}
    artifact['training']['calibration_game_ids'] += sorted({c['game']['game_id'] for c in calibration_contexts})
    artifact['training']['calibration_label_max_at'] = max(c['final_source']['received_at'] for c in calibration_contexts)
    artifact['training']['validation_game_ids'] = sorted(c['game']['game_id'] for c in outer)
    paired, policy_groups, sources = [], [], {}
    for source in training_sources:
        sources[(source['id'], 'training')] = source
    for collection, role in ((choice_contexts, 'calibration'), (calibration_contexts, 'calibration'), (outer, 'validation')):
        for context in collection:
            from .nba_learning_data import descriptor
            for source in context['sources'] + [context['final_source']]:
                sources[(source['id'], role)] = descriptor(source, role)
    for context in outer:
        frozen = evidence.replay_incumbent(context)
        selected = select(dict(artifact=artifact, **proposal_info, training_cutoff=cutoff), context['payload']['quotes'],
            context['history'], context['game']['tipoff_utc'], context['as_of'],
            [dict(player_id=r['player_id'], status=r.get('availability', 'unknown')) for r in context['roster']], 5)
        if selected['status'] != 'ok':
            return dict(status='waiting', sport='NBA', reason='common_role_policy_validation_incomplete')
        group = dict(game_id=context['game']['game_id'], context_id=context['context_id'],
                     universe_quote_ids=[q['quote_id'] for q in context['payload']['quotes']], baseline=[], candidate=[])
        # The ordinary paired gate scores the incumbent's exact saved members;
        # the separate policy gate also scores the candidate's complete five.
        for label, selected_rows in (('baseline', frozen), ('candidate', selected['selections'])):
            for selected_row in selected_rows:
                q = (next(q for q in context['payload']['quotes'] if q['quote_id'] == selected_row['quote_id']) if label == 'baseline' else
                     next(q for q in context['payload']['quotes'] if all(q[k] == selected_row[k] for k in ('player_id','market','side','line','sportsbook'))))
                before = (nba_learning._project(q['player_id'], q['market'], context['history'], context['game']['tipoff_utc'], q['line'], retained, info)
                          if retained is not None else baseline.project_prop(q['player_id'], q['market'], context['history'], context['game']['tipoff_utc'], q['line']))
                after = project(q['player_id'], q['market'], context['history'], context['game']['tipoff_utc'], q['line'], artifact, proposal_info)
                try:
                    row = _record(context, q, before, after)
                except ValueError:
                    return dict(status='waiting', sport='NBA', reason='common_role_policy_validation_participation_unknown')
                group[label].append(row)
                if label == 'baseline':
                    paired.append(row)
        policy_groups.append(group)
    names = ('nba_learning.py','nba_learning_data.py','nba_prop_quality.py','nba_prop_role.py','nba_prop_features.py','nba_model_fit.py',
             'nba_policy_evidence.py','nba_store.py','nba_game_context.py','prop_calibration.py','nba_model.py','nba_prop_model.py','pricing.py',
             'nba_sources.py','nba_data.py','nba_quotes.py','nba_identity.py','analytics/nba.py','nba_model_manifest.py',
             'nba_prop_opponent.py')
    artifact['code_sha256'] = {'edgeterminal/' + n: hashlib.sha256((nba_learning.ROOT / 'edgeterminal' / n).read_bytes()).hexdigest() for n in names}
    evaluation = dict(kind='actual_main_lines', validation_start=min(c['payload']['observed_at'] for c in outer),
        validation_end=max(c['final_source']['received_at'] for c in outer), calibration_cutoff=calibration_cutoff,
        baseline_candidate_id=current['candidate_id'] if current else None, rows=paired,
        selection_policy=dict(kind='common_full_offer_universe', games=policy_groups),
        design='Thirty earlier whole games choose role markets; thirty later calibrate selected-policy probabilities; untouched later dates score both complete policies.')
    artifact['actual_main_line_validation'] = nba_learning._summary(paired)
    version = ('nba-props-role-' if kind == KIND else 'nba-props-rate-') + hashlib.sha256(quality._json(dict(artifact=artifact,evaluation=evaluation,sources=list(sources.values()))).encode()).hexdigest()[:20]
    artifact['version'] = version
    return dict(status='ready', sport='NBA', version=version, training_cutoff=cutoff, artifact=artifact,
        evaluation=evaluation, sources=list(sources.values()), summary=artifact['actual_main_line_validation'])
