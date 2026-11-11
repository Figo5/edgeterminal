"""NBA selected-policy calibration orchestration, separate from immutable pins.

The candidate retains its exact selector and point forecasts. Earlier actual
lines fit calibration; a separate inner block chooses markets; the final whole
games remain untouched. Workload proposals require full-offer policy evidence.
"""
import copy
import hashlib
import json
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from . import nba_model as baseline, prop_calibration

KIND = 'nba-selected-policy-calibration-v2'
FAMILY = 'nba-props-calibration-v2'
QUALITY_PROTOCOL = 'NBA propsquality2.0'


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def selector_hash(artifact):
    return hashlib.sha256(_json(artifact).encode()).hexdigest()


def validate(artifact):
    from . import nba_learning as learning
    if artifact.get('schema') != 2 or artifact.get('kind') != KIND or artifact.get('ranking_changed') is not False:
        raise ValueError('invalid NBA selected-policy artifact')
    base = artifact.get('base_artifact')
    if base is not None:
        if base.get('schema') != 1:
            raise ValueError('NBA calibration selector must be a frozen schema-one artifact or pinned baseline')
        learning._validate_artifact(base)
    if artifact.get('selector_sha256') != selector_hash(base):
        raise ValueError('NBA calibration selector binding mismatch')
    learning._number(artifact['training_cutoff'])
    if set(artifact.get('calibration_layers', {})) != set(baseline.MARKETS):
        raise ValueError('NBA calibration markets incomplete')
    if not set(artifact.get('changed_markets', [])).issubset(set(baseline.MARKETS)):
        raise ValueError('NBA changed market unknown')
    for market, layers in artifact['calibration_layers'].items():
        if not isinstance(layers, list) or len(layers) > 52:
            raise ValueError('NBA calibration layer count invalid')
        for config in layers:
            prop_calibration.apply_calibrator({'over': .6, 'under': .4, 'push': 0.}, config)
            if config.get('selector_sha256') != artifact['selector_sha256']:
                raise ValueError('NBA calibration layer selector binding mismatch')
    return artifact


def point_project(player_id, market, history, tipoff, line, base):
    from . import nba_learning as learning
    if base is None:
        return baseline.project_prop(player_id, market, history, tipoff, line)
    info = dict(version=base.get('version', 'nba-retained-selector'), fingerprint=selector_hash(base))
    return learning._project(player_id, market, history, tipoff, line, base, info)


def project(player_id, market, history, tipoff, line, artifact, info):
    validate(artifact)
    out = point_project(player_id, market, history, tipoff, line, artifact['base_artifact'])
    if out.get('status') != 'ok':
        return out
    out = dict(out, model_version=info['version'], parameter_fingerprint=info['fingerprint'])
    if line is not None and artifact['calibration_layers'][market]:
        probs = {side: out['probability_' + side] for side in ('over', 'under', 'push')}
        probs = coherent_probabilities(probs)
        layers = artifact['calibration_layers'][market]
        for index, config in enumerate(layers):
            probs = prop_calibration.apply_calibrator(probs, config)
            if index + 1 < len(layers):
                # The next update was fit on the incumbent's serialized live
                # probabilities. Replay that boundary exactly between layers.
                probs = coherent_probabilities({side: round(value, 4) for side, value in probs.items()})
        out.update({'probability_' + side: round(probs[side], 4) for side in probs})
        if artifact['calibration_layers'][market]:
            out.update(probability_method='actual_line_selected_policy_calibration',
                       probability_scope='fixed_raw_top_five_membership')
    return out


def coherent_probabilities(probs):
    """Recover complementarity after four-decimal serialization, preserving push."""
    values = [probs[k] for k in ('over', 'under', 'push')]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in values):
        raise ValueError('invalid NBA probability mass')
    if abs(sum(values) - 1.) > .00011:
        raise ValueError('invalid NBA probability mass')
    total = probs['over'] + probs['under']
    over = (1. - probs['push']) * probs['over'] / total if total else 0.
    return dict(over=over, under=1. - probs['push'] - over, push=probs['push'])


def select(current, quotes, history, tipoff, now, injuries, count):
    from . import nba_learning as learning
    artifact = validate(current['artifact'])
    base = artifact['base_artifact']
    retained = None if base is None else dict(artifact=base, version=base.get('version', 'retained-selector'),
        training_cutoff=base['training_cutoff'], artifact_sha256=selector_hash(base))
    token = learning._SNAPSHOT.set(retained)
    try:
        selected = learning.select_props(quotes, history, tipoff, now, injuries, count)
    finally:
        learning._SNAPSHOT.reset(token)
    # Do not re-sort, change a side, or drop a pick after correcting confidence.
    # This exact raw membership is the population on which calibration fits.
    info = learning._info(current)
    for row in selected['candidates']:
        p = project(row['player_id'], row['market'], history, tipoff, row['line'], artifact, info)
        row.update(raw_policy_probability=row['probability'],
                   probability=p['probability_' + row['side']], probability_push=p['probability_push'],
                   model_version=info['version'], parameter_fingerprint=info['fingerprint'],
                   probability_method=p['probability_method'])
        row['edge'] = round(row['probability'] - learning.american_to_prob(row['price']), 4)
    selected.update(model_version=info['version'], parameter_fingerprint=info['fingerprint'])
    return selected


def _partition(records, outer_games=40):
    dates = defaultdict(list)
    for row in records:
        day = datetime.fromtimestamp(row['kickoff_at'], ZoneInfo('America/New_York')).date().isoformat()
        dates[day].append(row)
    ordered = sorted(dates)
    outer_dates, count = [], set()
    while ordered and (len(count) < outer_games or len(outer_dates) < 7):
        day = ordered.pop()
        outer_dates.append(day)
        count.update(r['game_id'] for r in dates[day])
    if len(ordered) < 2:
        return [], [], [r for d in sorted(outer_dates) for r in dates[d]]
    midpoint = len(ordered) // 2
    return ([r for d in ordered[:midpoint] for r in dates[d]],
            [r for d in ordered[midpoint:] for r in dates[d]],
            [r for d in sorted(outer_dates) for r in dates[d]])


def _paired(record, before, after):
    keys = ('game_id', 'player_id', 'market', 'quote_id', 'line', 'side', 'observed_at',
            'kickoff_at', 'result_observed_at', 'actual')
    return dict({k: record[k] for k in keys}, baseline_prediction=before['raw_projection'],
                candidate_prediction=after['raw_projection'], baseline_probability=conditional_probability(before, record['side']),
                candidate_probability=conditional_probability(after, record['side']))


def conditional_probability(projection, side):
    mass = 1. - projection['probability_push']
    if mass <= 0:
        raise ValueError('NBA decided observation has no predicted decided mass')
    value = projection['probability_' + side] / mass
    if not -.00011 <= value <= 1.00011:
        raise ValueError('NBA conditional side probability invalid')
    return min(1., max(0., value))


def build(now=None):
    from . import nba_learning as learning
    stamp = time.time() if now is None else learning._number(now)
    records, context, _ = learning.validation_records(stamp, 120)
    current = learning._active()
    # Saved-five evidence validates only the selector that actually chose it.
    # Never treat old selections from a different scorer as the new policy.
    current_version = learning.model_info()['version']
    records = [r for r in records if r['saved']['model_version'] == current_version]
    fit, inner, outer = _partition(records)
    if min(len({r['game_id'] for r in group}) for group in (fit, inner)) < 10 \
            or len({r['game_id'] for r in outer}) < 30:
        return dict(status='waiting', sport='NBA', reason='more_separate_actual_line_calibration_selection_and_validation_games',
                    fit_games=len({r['game_id'] for r in fit}), selection_games=len({r['game_id'] for r in inner}),
                    validation_games=len({r['game_id'] for r in outer}))
    fit_cutoff = min(r['observed_at'] for r in inner) - 1
    selection_cutoff = min(r['observed_at'] for r in outer) - 1
    if max(r['result_observed_at'] for r in fit) >= fit_cutoff \
            or max(r['result_observed_at'] for r in inner) >= selection_cutoff:
        return dict(status='waiting', sport='NBA', reason='whole_date_label_boundaries_overlap')
    from . import model_learning_store
    if not model_learning_store.holdout_available('NBA', FAMILY, {r['game_id'] for r in outer}):
        return dict(status='waiting', sport='NBA', reason='fresh_untouched_quality_holdout_required')
    incumbent = current['artifact'] if current else None
    if incumbent and incumbent.get('kind') != KIND and incumbent.get('schema') == 2:
        return dict(status='waiting', sport='NBA', reason='role_selector_updates_require_full_policy_evidence')
    if current and current['training_cutoff'] >= min(r['observed_at'] for r in fit):
        return dict(status='waiting', sport='NBA', reason='incumbent_overlaps_calibration_fit')
    if incumbent and incumbent.get('schema') == 2:
        base = copy.deepcopy(incumbent['base_artifact'])
        layers = copy.deepcopy(incumbent['calibration_layers'])
    else:
        base, layers = copy.deepcopy(incumbent), {m: [] for m in baseline.MARKETS}
    bound = selector_hash(base)
    def before(record):
        return (learning._project(record['player_id'], record['market'], record['history'], record['kickoff_at'],
                 record['line'], incumbent, learning._info(current)) if current else
                baseline.project_prop(record['player_id'], record['market'], record['history'], record['kickoff_at'], record['line']))
    configs = {}
    for market in baseline.MARKETS:
        samples = []
        for row in fit:
            if row['market'] != market:
                continue
            p = before(row)
            coherent = coherent_probabilities({side: p['probability_' + side] for side in ('over', 'under', 'push')})
            samples.append(dict(game_id=row['game_id'], quote_id=row['quote_id'],
                probability_over=coherent['over'], probability_under=coherent['under'],
                probability_push=coherent['push'], actual=row['actual'], line=row['line'],
                result_observed_at=row['result_observed_at']))
        configs[market] = dict(prop_calibration.fit_calibrator(samples, cutoff=fit_cutoff), selector_sha256=bound)
    proposed = dict(schema=2, kind=KIND, evaluation_family=FAMILY, quality_protocol=QUALITY_PROTOCOL, training_cutoff=fit_cutoff,
        base_artifact=base, selector_sha256=bound, ranking_changed=False,
        probability_contract='conditional_side_given_no_push',
        calibration_layers={m: layers[m] + [configs[m]] for m in baseline.MARKETS}, changed_markets=list(baseline.MARKETS))
    paired_inner = []
    info = dict(version='nba-quality-inner', fingerprint='unregistered')
    for row in inner:
        paired_inner.append(_paired(row, before(row), project(row['player_id'], row['market'], row['history'],
            row['kickoff_at'], row['line'], proposed, info)))
    choice = prop_calibration.choose_market_updates(paired_inner, baseline.MARKETS, cutoff=selection_cutoff)
    changed = choice['changed_markets']
    if not changed:
        return dict(status='waiting', sport='NBA', reason='no_market_calibration_improvement_on_inner_games',
                    market_selection=choice)
    point_training, point_sources = _point_training(base, fit_cutoff)
    inherited_calibration = set(point_training.pop('retained_calibration_game_ids', []))
    if incumbent and incumbent.get('schema') == 2:
        previous_training = incumbent.get('training', {})
        for key in ('calibration_game_ids', 'selection_game_ids', 'validation_game_ids'):
            inherited_calibration.update(previous_training.get(key, []))
    proposed.update(changed_markets=changed, market_selection=choice,
        calibration_layers={m: layers[m] + ([configs[m]] if m in changed else []) for m in baseline.MARKETS},
        training=dict(**point_training, calibration_fit_game_ids=sorted({r['game_id'] for r in fit}),
                      calibration_game_ids=sorted(inherited_calibration | {r['game_id'] for r in fit}),
                      calibration_label_max_at=max(r['result_observed_at'] for r in fit),
                      selection_game_ids=choice['selection_game_ids'], validation_game_ids=sorted({r['game_id'] for r in outer})))
    sources = {}
    for item in point_sources:
        sources[(item['id'], 'training')] = dict(item, role='training')
    if incumbent and incumbent.get('schema') == 2:
        for item in incumbent.get('training_source_catalog', []):
            sources[(item['id'], item['role'])] = item
    for group, role in ((fit, 'calibration'), (inner, 'calibration'), (outer, 'validation')):
        for row in group:
            for source in row['sources']:
                item = dict(source, role=role)
                sources[(item['id'], role)] = item
    proposed['training_source_catalog'] = sorted((s for s in sources.values() if s['role'] != 'validation'),
                                                key=lambda s: (s['role'], s['id']))
    names = ('nba_learning.py', 'nba_learning_data.py', 'nba_prop_quality.py', 'nba_prop_role.py', 'nba_prop_features.py', 'nba_model_fit.py',
             'nba_policy_evidence.py', 'nba_store.py', 'nba_game_context.py', 'prop_calibration.py', 'nba_model.py', 'nba_prop_model.py', 'pricing.py',
             'nba_sources.py', 'nba_data.py', 'nba_quotes.py', 'nba_identity.py', 'analytics/nba.py', 'nba_model_manifest.py',
             'nba_prop_opponent.py')
    proposed['code_sha256'] = {'edgeterminal/' + name: hashlib.sha256((learning.ROOT / 'edgeterminal' / name).read_bytes()).hexdigest()
                               for name in names}
    paired = [_paired(r, before(r), project(r['player_id'], r['market'], r['history'], r['kickoff_at'],
        r['line'], proposed, dict(version='nba-quality-outer', fingerprint='unregistered'))) for r in outer]
    evaluation = dict(kind='actual_main_lines', validation_start=min(r['observed_at'] for r in outer),
        validation_end=max(r['result_observed_at'] for r in outer), calibration_cutoff=selection_cutoff,
        baseline_candidate_id=current['candidate_id'] if current else None,
        rows=paired, design='Fixed incumbent top-five membership; earlier actual-line calibration fit and separate market selection; untouched later whole games.',
        exclusions=context['exclusions'])
    proposed['actual_main_line_validation'] = learning._summary(paired)
    version = 'nba-props-quality-' + hashlib.sha256(_json(dict(artifact=proposed, evaluation=evaluation,
        sources=list(sources.values()))).encode()).hexdigest()[:20]
    proposed['version'] = version
    validate(proposed)
    return dict(status='ready', sport='NBA', version=version, training_cutoff=fit_cutoff, artifact=proposed,
                evaluation=evaluation, sources=sorted(sources.values(), key=lambda x: (x['role'], x['id'])),
                summary=proposed['actual_main_line_validation'])


def _point_training(base, cutoff):
    from . import nba_learning_data as evidence, nba_model_fit
    if base is None:
        catalog = baseline.PROP_PARAMETERS['source_catalog']
        ids = [s['artifact_id'] for s in catalog]
        history, sources = evidence.history_from_sources(ids, {}, cutoff)
        records = nba_model_fit.examples(history)['points']
        rows = [r for r in records if r['ts'] < nba_model_fit.stamp(nba_model_fit.FIT_END)]
        calibration = [r for r in records if nba_model_fit.stamp(nba_model_fit.FIT_END) <= r['ts'] < nba_model_fit.stamp(nba_model_fit.CALIBRATION_END)]
        if not rows:
            raise ValueError('pinned NBA point fitting provenance unavailable')
        from . import nba_learning
        return dict(fit_game_ids=sorted({nba_learning._game_id(r['game_id'], r['ts']) for r in rows}),
                    fit_label_max_at=max(r['ts'] + 14400 for r in rows),
                    retained_calibration_game_ids=sorted({nba_learning._game_id(r['game_id'], r['ts']) for r in calibration})), [evidence.descriptor(s, 'training') for s in sources]
    training = base['training']
    for item in base['training_source_catalog']:
        source = evidence._source(item['id'], {}, cutoff)[0]
        if source['sha256'] != item['sha256']:
            raise ValueError('retained NBA source provenance changed')
    return dict(fit_game_ids=training.get('coefficient_game_ids', training['fit_game_ids']),
                development_game_ids=training.get('development_game_ids', []),
                development_label_max_at=training.get('development_label_max_at', training['fit_label_max_at']),
                retained_calibration_game_ids=training.get('calibration_game_ids', []),
                fit_label_max_at=training['fit_label_max_at']), base['training_source_catalog']
