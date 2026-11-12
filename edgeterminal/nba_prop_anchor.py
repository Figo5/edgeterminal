"""Price-anchored NBA prop policy requiring full contemporaneous policy evidence.

The retained scorer's point forecast never changes. Each offered side's
conditional probability becomes one pooled, regularized logistic blend of the
retained scorer's probability and that offer's own price-implied probability.
The five are ranked by that blend in every market; a displayed probability
changes only in markets that earned it on separate earlier games.

The implied probability includes the sportsbook's margin; the fitted intercept
absorbs its average. A higher hit rate from leaning toward favoured prices is
not a betting edge. Everything stays PASS / RESEARCH_ONLY.
"""
import copy
import hashlib
import math
from collections import defaultdict

import numpy as np

from . import nba_model as baseline, prop_calibration
from . import nba_prop_quality as quality

KIND = 'nba-price-anchor-policy-v1'
FAMILY = 'nba-props-price-anchor-policy-v1'
SCHEMA = 'price-anchor-logit-1'
METHOD = 'price_anchored_logit'
REGULARIZATION = 10.
MIN_ROWS, MIN_GAMES = 200, 20
# Saved selections through Nov 12 2026 (Eastern) were inspected before this
# policy was declared, so its untouched validation games must tip off afterwards.
DECLARED_AT = baseline.parse_time('2026-11-13T05:00:00Z')
CODE_NAMES = ('nba_learning.py', 'nba_learning_data.py', 'nba_prop_quality.py', 'nba_prop_role.py',
              'nba_prop_anchor.py', 'nba_prop_features.py', 'nba_model_fit.py', 'nba_policy_evidence.py',
              'nba_store.py', 'nba_game_context.py', 'prop_calibration.py', 'nba_model.py', 'nba_prop_model.py',
              'pricing.py', 'nba_sources.py', 'nba_data.py', 'nba_quotes.py', 'nba_identity.py',
              'analytics/nba.py', 'nba_model_manifest.py', 'nba_prop_opponent.py')


def _implied(price):
    from .pricing import american_to_prob
    value = prop_calibration._number(price)
    if value == 0:
        raise ValueError('price anchor requires a real American price')
    return american_to_prob(value)


def fit_anchor(rows, *, cutoff, min_rows=MIN_ROWS, min_games=MIN_GAMES):
    """Pooled logit of side outcome on model and price logits, shrunk toward the model alone."""
    rows = list(rows)
    catalog = prop_calibration._catalog(rows, cutoff)
    games = {r['game_id'] for r in rows}
    base = dict(schema=SCHEMA, method='identity', intercept=0., model_slope=1., price_slope=0.,
                regularization=REGULARIZATION, fit_rows=len(rows), fit_games=len(games),
                label_max_at=catalog['selection_label_max_at'], cutoff=float(cutoff),
                fit_game_ids=catalog['selection_game_ids'])
    if len(rows) < min_rows or len(games) < min_games:
        return dict(base, reason='insufficient_full_offer_coverage')
    y = np.array([float(bool(r['won'])) for r in rows])
    if len(set(y)) < 2:
        return dict(base, reason='both_decided_outcomes_required')
    x = np.array([[1., prop_calibration._logit(prop_calibration._number(r['model_probability'])),
                   prop_calibration._logit(_implied(r['price']))] for r in rows])
    prior = np.array([0., 1., 0.])
    low, high = np.array([-5., 0., 0.]), np.array([5., 4., 4.])
    coefficients = prior.copy()
    for _ in range(60):
        predicted = 1. / (1. + np.exp(-np.clip(x @ coefficients, -30., 30.)))
        gradient = x.T @ (predicted - y) + REGULARIZATION * (coefficients - prior)
        weights = np.maximum(predicted * (1. - predicted), 1e-9)
        hessian = x.T @ (weights[:, None] * x) + REGULARIZATION * np.eye(3)
        updated = np.clip(coefficients - np.linalg.solve(hessian, gradient), low, high)
        done = float(np.max(np.abs(updated - coefficients))) < 1e-9
        coefficients = updated
        if done:
            break
    return dict(base, method=METHOD, intercept=float(coefficients[0]), model_slope=float(coefficients[1]),
                price_slope=float(coefficients[2]), reason=None)


def recalibrate(config, rows, *, cutoff):
    """Re-level the fitted blend on separate later games; slopes stay frozen."""
    rows = list(rows)
    catalog = prop_calibration._catalog(rows, cutoff)
    offsets = np.array([prop_calibration._logit(apply_anchor(r['model_probability'], r['price'], config)) for r in rows])
    y = np.array([float(bool(r['won'])) for r in rows])
    shift = 0.
    for _ in range(60):
        predicted = 1. / (1. + np.exp(-np.clip(offsets + shift, -30., 30.)))
        step = (float(np.sum(predicted - y)) + REGULARIZATION * shift) / \
               (float(np.sum(np.maximum(predicted * (1. - predicted), 1e-9))) + REGULARIZATION)
        shift -= step
        if abs(step) < 1e-9:
            break
    return dict(config, intercept=float(np.clip(config['intercept'] + shift, -5., 5.)), calibration_shift=shift,
                calibration_rows=len(rows), calibration_game_ids=catalog['selection_game_ids'],
                calibration_label_max_at=catalog['selection_label_max_at'], calibration_cutoff=float(cutoff))


def apply_anchor(model_probability, price, config):
    """Conditional side probability given no push; identity returns the model's own."""
    p = prop_calibration._number(model_probability)
    if not 0 <= p <= 1 or config.get('schema') != SCHEMA or config.get('method') not in ('identity', METHOD):
        raise ValueError('unknown price-anchor contract')
    a, b, c = (prop_calibration._number(config[k]) for k in ('intercept', 'model_slope', 'price_slope'))
    if not -5 <= a <= 5 or not 0 <= b <= 4 or not 0 <= c <= 4:
        raise ValueError('invalid price-anchor coefficients')
    if config['method'] == 'identity':
        if (a, b, c) != (0., 1., 0.):
            raise ValueError('identity price-anchor coefficients changed')
        return p
    return prop_calibration._sigmoid(a + b * prop_calibration._logit(p) + c * prop_calibration._logit(_implied(price)))


def validate(artifact):
    from . import nba_learning
    if artifact.get('schema') != 2 or artifact.get('kind') != KIND or artifact.get('ranking_changed') is not True:
        raise ValueError('invalid NBA price-anchor artifact')
    nba_learning._number(artifact['training_cutoff'])
    retained = artifact.get('retained_artifact')
    if quality.selector_hash(retained) != artifact.get('selector_sha256'):
        raise ValueError('NBA retained price-anchor selector binding mismatch')
    if retained is not None:
        if retained.get('kind') == KIND:
            raise ValueError('NBA price anchor cannot retain another price anchor')
        nba_learning._validate_artifact(retained)
    changed = artifact.get('changed_markets')
    if not isinstance(changed, list) or len(set(changed)) != len(changed) or not set(changed) <= set(baseline.MARKETS):
        raise ValueError('invalid NBA price-anchor market selection')
    apply_anchor(.6, -110., artifact['anchor'])
    return artifact


def _model(player_id, market, history, tipoff, line, retained, info):
    from . import nba_learning
    return (baseline.project_prop(player_id, market, history, tipoff, line) if retained is None else
            nba_learning._project(player_id, market, history, tipoff, line, retained, info))


def project(player_id, market, history, tipoff, line, artifact, info):
    """Point forecast and price-free probabilities are the retained scorer's own."""
    validate(artifact)
    out = _model(player_id, market, history, tipoff, line, artifact['retained_artifact'], info)
    if out.get('status') != 'ok':
        return out
    return dict(out, model_version=info['version'], parameter_fingerprint=info['fingerprint'])


def offer(quote, history, tipoff, artifact, info):
    """Ranking score and displayed probability for one offered side at its own price."""
    raw = project(quote['player_id'], quote['market'], history, tipoff, quote['line'], artifact, info)
    if raw.get('status') != 'ok':
        return raw
    mass = 1. - raw['probability_push']
    score = apply_anchor(quality.conditional_probability(raw, quote['side']), quote['price'], artifact['anchor'])
    anchored = quote['market'] in artifact['changed_markets'] and artifact['anchor']['method'] == METHOD
    # Registered probabilities for changed markets must stay strictly inside zero-one.
    probability = round(mass * min(.999, max(.001, score)), 4) if anchored else raw['probability_' + quote['side']]
    return dict(raw, score=score, probability=probability, anchored=anchored,
                conditional=min(1., max(0., probability / mass)))


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
        scored = offer(row, history, tipoff, artifact, info)
        if scored.get('status') != 'ok':
            raise ValueError('NBA price-anchor scorer lost an otherwise supported market')
        side = scored['probability_' + row['side']]
        row.update(projection=scored['projection'], sd=scored['sd'],
            raw_policy_probability=side, probability=scored['probability'], probability_push=scored['probability_push'],
            policy_score=round(scored['score'], 6), policy_conditional_probability=scored['conditional'],
            probability_method=METHOD if scored['anchored'] else scored.get('probability_method'),
            edge=round(scored['probability'] - _implied(row['price']), 4),
            model_version=info['version'], parameter_fingerprint=info['fingerprint'])
        key = (row['game_id'], row['player_id'], row['market'])
        order = (-row['policy_score'], -nba_learning.american_to_decimal(row['price']), str(row['sportsbook']), row['side'])
        if key not in groups or order < groups[key][0]:
            groups[key] = (order, row)
    candidates = sorted((v[1] for v in groups.values()), key=lambda r: (-r['policy_score'], r['player_id'], r['market']))
    selected, rest, counts = [], [], defaultdict(int)
    for row in candidates:
        if len(selected) < count and counts[row['player_id']] < baseline.MAX_MARKETS_PER_PLAYER:
            selected.append(row); counts[row['player_id']] += 1
        else:
            rest.append(row)
    selected.extend(rest[:max(0, count - len(selected))])
    okay = len(selected) == count
    return dict(status='ok' if okay else 'wait', selections=selected if okay else [], candidates=candidates,
        excluded=excluded, eligible_candidates=len(candidates), required=count, tipoff=baseline.parse_time(tipoff),
        generated_at=baseline.parse_time(now), model_version=info['version'], parameter_fingerprint=info['fingerprint'],
        designation='PASS', readiness='RESEARCH_ONLY')


def _thirds(contexts):
    """Three contiguous whole-Eastern-date blocks, oldest first."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    dates = defaultdict(list)
    for c in contexts:
        dates[datetime.fromtimestamp(c['game']['tipoff_ts'], ZoneInfo('America/New_York')).date().isoformat()].append(c)
    ordered = sorted(dates)
    a, b = len(ordered) // 3, 2 * len(ordered) // 3
    return tuple([c for d in part for c in dates[d]] for part in (ordered[:a], ordered[a:b], ordered[b:]))


def _record(context, quote, before, after):
    from .nba_prop_role import _record as paired
    row = paired(context, quote, before, after)
    row['candidate_probability'] = after['conditional']
    return row


def build(now, contexts=None):
    from . import nba_learning, nba_policy_evidence as evidence, model_learning_store
    from .nba_learning_data import descriptor
    from .nba_prop_role import _actual
    contexts = evidence.ready_contexts(now, 160) if contexts is None else contexts
    current = nba_learning._active()
    info = nba_learning.model_info()
    contexts = [c for c in contexts if c['payload']['selector']['version'] == info['version']
                and c['payload']['selector']['fingerprint'] == info['fingerprint']]
    incomplete = sum(len(c['payload']['incumbent']) != 5 for c in contexts)
    contexts = [c for c in contexts if len(c['payload']['incumbent']) == 5]
    if len(contexts) < 100:
        return dict(status='waiting', sport='NBA', reason='full_same_selector_policy_contexts_required',
                    available_contexts=len(contexts), required_contexts=100, incomplete_pretip_contexts=incomplete)
    pseudo = [dict(kickoff_at=c['game']['tipoff_ts'], game_id=c['game']['game_id'], context=c) for c in contexts]
    first, second, outer_rows = quality._partition(pseudo)
    fit_contexts, calibration_contexts, choice_contexts = _thirds([r['context'] for r in first + second])
    outer = [r['context'] for r in outer_rows]
    if min(len(fit_contexts), len(calibration_contexts), len(choice_contexts)) < 30:
        return dict(status='waiting', sport='NBA', reason='separate_thirty_game_anchor_fit_calibration_and_choice_blocks_required',
                    available_contexts=len(contexts), validation_games=len(outer))
    if min(c['game']['tipoff_ts'] for c in outer) < DECLARED_AT:
        return dict(status='waiting', sport='NBA', reason='price_anchor_validation_must_follow_its_declaration',
                    validation_games=sum(c['game']['tipoff_ts'] >= DECLARED_AT for c in contexts))
    if not model_learning_store.holdout_available('NBA', FAMILY, [c['game']['game_id'] for c in outer]):
        return dict(status='waiting', sport='NBA', reason='fresh_untouched_price_anchor_holdout_required')
    fit_cutoff = min(c['payload']['observed_at'] for c in calibration_contexts) - 1
    calibration_cutoff = min(c['payload']['observed_at'] for c in choice_contexts) - 1
    choice_cutoff = min(c['payload']['observed_at'] for c in outer) - 1
    if max(c['final_source']['received_at'] for c in fit_contexts) >= fit_cutoff \
            or max(c['final_source']['received_at'] for c in calibration_contexts) >= calibration_cutoff \
            or max(c['final_source']['received_at'] for c in choice_contexts) >= choice_cutoff:
        return dict(status='waiting', sport='NBA', reason='price_anchor_label_blocks_overlap')
    retained = copy.deepcopy(current['artifact']) if current else None
    if retained is not None and retained.get('kind') == KIND:
        # ponytail: one anchor layer only; stacking needs a price-aware baseline replay.
        return dict(status='waiting', sport='NBA', reason='price_anchor_incumbent_already_active')
    tip = lambda c: c['game']['tipoff_utc']
    before = lambda c, q: _model(q['player_id'], q['market'], c['history'], tip(c), q['line'], retained, info)

    def samples(block):
        """Every decided offered side with a known final stat, at its own price."""
        rows = []
        for context in block:
            evidence.replay_incumbent(context)
            for q in context['payload']['quotes']:
                actual, raw = _actual(context, q), before(context, q)
                if actual is None or actual == q['line'] or raw['status'] != 'ok':
                    continue
                rows.append(dict(game_id=q['game_id'], quote_id=q['quote_id'], price=q['price'],
                    model_probability=quality.conditional_probability(raw, q['side']),
                    won=(actual > q['line']) == (q['side'] == 'over'),
                    result_observed_at=context['final_source']['received_at']))
        return rows
    anchor = fit_anchor(samples(fit_contexts), cutoff=fit_cutoff)
    if anchor['method'] != METHOD:
        return dict(status='waiting', sport='NBA', reason='price_anchor_' + anchor['reason'])
    anchor = recalibrate(anchor, samples(calibration_contexts), cutoff=calibration_cutoff)
    artifact = dict(schema=2, kind=KIND, evaluation_family=FAMILY, quality_protocol=quality.QUALITY_PROTOCOL,
        training_cutoff=fit_cutoff, retained_artifact=retained, selector_sha256=quality.selector_hash(retained),
        ranking_changed=True, probability_contract='conditional_side_given_no_push', anchor=anchor,
        changed_markets=list(baseline.MARKETS),
        training=dict(fit_game_ids=anchor['fit_game_ids'], fit_label_max_at=anchor['label_max_at']))
    proposal_info = dict(version='nba-price-anchor-unregistered', fingerprint='unregistered')
    after = lambda c, q: offer(q, c['history'], tip(c), artifact, proposal_info)

    paired_inner = []
    for context in choice_contexts:
        evidence.replay_incumbent(context)
        used = set()
        for q in context['payload']['quotes']:
            key = (q['player_id'], q['market'])
            if key in used or _actual(context, q) is None:
                continue
            old, new = before(context, q), after(context, q)
            if old['status'] != 'ok' or new['status'] != 'ok':
                continue
            used.add(key); paired_inner.append(_record(context, q, old, new))
    choice = prop_calibration.choose_market_updates(paired_inner, baseline.MARKETS, cutoff=choice_cutoff)
    if not choice['changed_markets']:
        return dict(status='waiting', sport='NBA', reason='no_price_anchor_market_earned_inner_selection',
                    market_selection=choice)
    artifact.update(changed_markets=choice['changed_markets'], market_selection=choice)
    artifact['training'].update(selection_game_ids=choice['selection_game_ids'],
        calibration_game_ids=sorted({c['game']['game_id'] for c in calibration_contexts}),
        calibration_label_max_at=max(c['final_source']['received_at'] for c in calibration_contexts),
        validation_game_ids=sorted(c['game']['game_id'] for c in outer))

    paired, policy_groups, sources = [], [], {}
    for collection, role in ((fit_contexts, 'training'), (calibration_contexts, 'calibration'),
                             (choice_contexts, 'calibration'), (outer, 'validation')):
        for context in collection:
            for source in context['sources'] + [context['final_source']]:
                sources[(source['id'], role)] = descriptor(source, role)
    for context in outer:
        frozen = evidence.replay_incumbent(context)
        quotes = context['payload']['quotes']
        selected = select(dict(artifact=artifact, **proposal_info, training_cutoff=fit_cutoff), quotes,
            context['history'], tip(context), context['as_of'],
            [dict(player_id=r['player_id'], status=r.get('availability', 'unknown')) for r in context['roster']], 5)
        if selected['status'] != 'ok':
            return dict(status='waiting', sport='NBA', reason='common_price_anchor_validation_incomplete')
        group = dict(game_id=context['game']['game_id'], context_id=context['context_id'],
                     universe_quote_ids=[q['quote_id'] for q in quotes], baseline=[], candidate=[])
        for label, rows in (('baseline', frozen), ('candidate', selected['selections'])):
            for chosen in rows:
                q = (next(q for q in quotes if q['quote_id'] == chosen['quote_id']) if label == 'baseline' else
                     next(q for q in quotes if all(q[k] == chosen[k] for k in ('player_id', 'market', 'side', 'line', 'sportsbook'))))
                try:
                    row = _record(context, q, before(context, q), after(context, q))
                except ValueError:
                    return dict(status='waiting', sport='NBA', reason='common_price_anchor_validation_participation_unknown')
                group[label].append(row)
                if label == 'baseline':
                    paired.append(row)
        policy_groups.append(group)
    artifact['code_sha256'] = {'edgeterminal/' + n: hashlib.sha256((nba_learning.ROOT / 'edgeterminal' / n).read_bytes()).hexdigest()
                               for n in CODE_NAMES}
    evaluation = dict(kind='actual_main_lines', validation_start=min(c['payload']['observed_at'] for c in outer),
        validation_end=max(c['final_source']['received_at'] for c in outer), calibration_cutoff=choice_cutoff,
        baseline_candidate_id=current['candidate_id'] if current else None, rows=paired,
        selection_policy=dict(kind='common_full_offer_universe', games=policy_groups),
        design='Earliest whole dates fit one pooled price anchor on every offered side; later dates re-level it; later dates choose markets; untouched final dates score both complete policies.')
    artifact['actual_main_line_validation'] = nba_learning._summary(paired)
    version = 'nba-props-anchor-' + hashlib.sha256(quality._json(dict(artifact=artifact, evaluation=evaluation,
        sources=list(sources.values()))).encode()).hexdigest()[:20]
    artifact['version'] = version
    return dict(status='ready', sport='NBA', version=version, training_cutoff=fit_cutoff, artifact=artifact,
        evaluation=evaluation, sources=list(sources.values()), summary=artifact['actual_main_line_validation'])
