"""Price-anchored NFL prop policy over complete frozen offer universes.

Point forecasts and every market's scorer configuration are retained exactly.
One pooled, regularized logit blends the retained scorer's conditional side
probability with that quote's own price-implied probability (the shared fit in
nba_prop_anchor). Ranking uses the blend in every market; a displayed
probability changes only in markets that earned it on a separate earlier week.

Whole weeks before W-2 fit the blend, W-2 re-levels its intercept, W-1 chooses
markets, and three untouched weeks from W score both complete policies through
the shared registry gate. A higher hit rate from leaning toward favoured prices
is not a betting edge.
"""
from copy import deepcopy
from pathlib import Path
import time

from . import nba_prop_anchor as anchor, prop_calibration

FAMILY = 'nfl-props-price-anchor-v1'
# Every graded selection through the Week 10 Thursday game was inspected before
# this policy was declared, so its untouched validation starts with Week 11.
FIRST_VALIDATION_WEEK = (2026, 11)
MIN_FIT_GAMES = 20


def apply(row, artifact):
    """Rank every market by the blend; display it only where it was chosen."""
    config = (artifact or {}).get('price_anchor')
    mass = 1. - row['probability_push']
    if not config or mass <= 0:
        return row
    side, other = row['side'], 'under' if row['side'] == 'over' else 'over'
    conditional = min(1., row['probability_' + side] / mass)
    value = round(mass * min(.999, max(.001, anchor.apply_anchor(conditional, row['price'], config))), 6)
    values = {side: value, other: round(mass - value, 6)}
    row.update({'policy_probability_' + k: v for k, v in values.items()})
    if row['market'] in artifact['changed_markets']:
        row.update({'probability_' + k: v for k, v in values.items()})
    return row


def _samples(groups, incumbent):
    """Every decided eligible quote at its own price, scored by the retained model."""
    from . import nfl_learning as learning
    rows = []
    for group in groups:
        for row in learning._score(group['rows'], incumbent):
            if row['actual'] == row['line']:
                continue
            rows.append(dict(game_id=row['game_id'], quote_id=row['quote_id'], price=row['price'],
                model_probability=learning._conditional_side(row, row['side']),
                won=(row['actual'] > row['line']) == (row['side'] == 'over'),
                result_observed_at=row['result_observed_at']))
    return rows


def build(now=None):
    from . import nfl_learning as learning, model_learning_store as store
    from . import nfl_prop_model as baseline, nfl_prop_lab as lab, nfl_prop_roles as roles
    from . import nfl_top_five, nfl_prop_availability
    stamp = time.time() if now is None else now
    waiting = lambda reason, **extra: dict(status='waiting', reason=reason, protocol=FAMILY, **extra)
    records, weeks, schedule = learning._quality_window(stamp)
    if len(weeks) < learning.ROLE_PARAMETERS['validation_weeks']:
        return waiting('Three completed NFL weeks with immutable quote universes are required.')
    if min(weeks) < FIRST_VALIDATION_WEEK:
        return waiting('NFL price-anchor validation must follow its declaration; waiting for Weeks 11-13.')
    if not store.holdout_available('NFL', FAMILY, [r['game_id'] for r in records]):
        return waiting('This NFL outer cohort was already evaluated; waiting for a wholly fresh window.')
    season, first_week = min(weeks)
    first = lambda week: min(learning._kickoff(g) for g in schedule.values()
                             if (int(g['season']), int(g['week'])) == week)
    start = first(min(weeks))
    active = learning._validated_active()
    if active and (active['training_cutoff'] >= start or
                   active['artifact'].get('calibration_cutoff', active['training_cutoff']) >= start):
        return waiting('Current NFL baseline needs a new untouched whole-week validation window.')
    incumbent = active['artifact'] if active else None
    if incumbent and incumbent.get('price_anchor'):
        # ponytail: one anchor layer only; refit instead of stacking when the first one ages.
        return waiting('A price anchor is already active.')
    calibration_week, selection_week = (season, first_week - 2), (season, first_week - 1)
    training_cutoff, probability_cutoff, calibration_cutoff = first(calibration_week) - 1., first(selection_week) - 1., start - 1.
    sources, coverage, blocks = [], {}, dict(fit=[], calibration=[], selection=[])
    for record in learning.db.q('SELECT * FROM nfl_top_five_selections ORDER BY selected_at,game_id'):
        parts = record['game_id'].split('-')
        week = int(parts[1]), int(parts[3])
        if week >= min(weeks) or week[0] != season:
            continue
        name, limit = (('fit', training_cutoff) if week < calibration_week else
                       ('calibration', probability_cutoff) if week == calibration_week else ('selection', calibration_cutoff))
        group, proof, reason = learning._graded_universe(dict(record, season=week[0], week=week[1]), limit)
        if reason:
            coverage[record['game_id']] = reason
            continue
        blocks[name].append(group)
        sources.extend(dict(s, role='training' if name == 'fit' else 'calibration') for s in proof)
    counts = {name: len(groups) for name, groups in blocks.items()}
    if counts['fit'] < MIN_FIT_GAMES or not counts['calibration'] or not counts['selection']:
        return waiting('Earlier NFL price-anchor fit, re-level and market-choice weeks are incomplete.',
                       waiting_kind='coverage_wait', quote_coverage=counts, excluded_games=coverage)
    catalog = learning._catalog(stamp)
    learning._attach_features(blocks['fit'] + blocks['calibration'] + blocks['selection'], catalog, sources)
    fitted = anchor.fit_anchor(_samples(blocks['fit'], incumbent), cutoff=training_cutoff)
    if fitted['method'] != anchor.METHOD:
        return waiting('NFL price anchor: ' + fitted['reason'], waiting_kind='coverage_wait', quote_coverage=counts)
    fitted = anchor.recalibrate(fitted, _samples(blocks['calibration'], incumbent), cutoff=probability_cutoff)
    configs = {m: deepcopy(incumbent['configurations'][m] if incumbent else learning._baseline_config()) for m in lab.MARKETS}
    artifact = dict(schema=learning.ROLE_SCHEMA, evaluation_family=FAMILY, parameters=learning.ROLE_PARAMETERS,
        probability_contract='conditional_side_given_no_push', features=list(roles.FEATURES),
        configurations=configs, price_anchor=fitted, changed_markets=list(lab.MARKETS), ranking_changed=True,
        fit_boundary_week=list(calibration_week), training_cutoff=training_cutoff,
        calibration_cutoff=calibration_cutoff, probability_calibration_cutoff=probability_cutoff,
        validation_description=f'2026 weeks {first_week}-{max(w[1] for w in weeks)}; full frozen offers; untouched',
        selector_sha256=learning._digest(Path(nfl_top_five.__file__).read_bytes()))
    units = learning._quote_units(blocks['selection'])
    before = {r['quote_id']: r for r in learning._score(units, incumbent)}
    inner = [learning._paired(row, before[row['quote_id']], row) for row in learning._score(units, artifact)]
    selection = prop_calibration.choose_market_updates(inner, lab.MARKETS, cutoff=calibration_cutoff)
    if not selection['changed_markets']:
        return waiting('No NFL market passed the separate inner price-anchor improvement requirements.',
                       market_selection=selection, quote_coverage=counts, excluded_games=coverage)
    artifact.update(changed_markets=selection['changed_markets'], market_selection=selection)
    artifact['parameter_fingerprint'] = learning._digest(dict(parameters=learning.ROLE_PARAMETERS,
        configurations=configs, price_anchor=fitted, changed_markets=artifact['changed_markets']))
    # Only now, with the blend and market list sealed, read outer labels.
    outer = []
    for record in records:
        group, proof, reason = learning._graded_universe(record, stamp)
        if reason:
            coverage[record['game_id']] = reason
        else:
            outer.append(group); sources.extend(proof)
    if len(outer) < 30 or sum(len(g['rows']) for g in outer) < 100:
        return waiting('Thirty wholly resolved common NFL offer universes are required.',
                       covered_games=len(outer), excluded_games=coverage)
    learning._attach_features(outer, catalog, sources)
    baseline_id = active['candidate_id'] if active else None
    policies = [dict(learning._policy(group, incumbent, artifact), baseline_candidate_id=baseline_id) for group in outer]
    label_max = lambda groups: max(r['result_observed_at'] for g in groups for r in g['rows'])
    artifact['training'] = dict(fit_game_ids=sorted(g['game_id'] for g in blocks['fit']),
        calibration_game_ids=sorted(g['game_id'] for g in blocks['calibration']),
        fit_label_max_at=label_max(blocks['fit']), calibration_label_max_at=label_max(blocks['calibration']))
    modules = (baseline, lab, roles, prop_calibration, nfl_top_five, nfl_prop_availability, learning, anchor)
    artifact['code_sha256'] = {str(Path(m.__file__).relative_to(lab.ROOT)): learning._digest(Path(m.__file__).read_bytes())
                               for m in modules}
    artifact['code_sha256']['edgeterminal/nfl_prop_anchor.py'] = learning._digest(Path(__file__).read_bytes())
    evaluation = dict(kind='actual_main_lines', validation_start=start,
        validation_end=max(r['result_observed_at'] for g in outer for r in g['rows']),
        calibration_cutoff=calibration_cutoff, baseline_candidate_id=baseline_id,
        rows=[r for p in policies for r in p['candidate']],
        selection_policy=dict(kind='common_full_offer_universe', games=policies),
        coverage=dict(included_games=len(outer), excluded_games=coverage))
    unique = {(s['kind'], s.get('path'), s['sha256'], s['role']): s for s in sources}
    sources = sorted(unique.values(), key=lambda s: (s['role'], s['kind'], s['sha256']))
    revision = learning._digest(dict(artifact=artifact, evaluation=evaluation, sources=sources))[:16]
    version = f'nfl-anchor-1.2026w{max(w[1] for w in weeks):02d}-{revision}'
    artifact['version'] = version
    return dict(status='ready', sport='NFL', version=version, training_cutoff=training_cutoff,
                artifact=artifact, evaluation=evaluation, sources=sources)
