"""Pure NBA totals transforms, chronological development and score composition.

Callers verify archived source bytes before using this module. This second
boundary checks finite values, identities and clocks; it never reads a file,
database or provider. Explicit reconstructed contexts retain actual receipt
times and cannot enter serving composition.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import statistics
from zoneinfo import ZoneInfo

FEATURE_NAMES = ('baseline_total', 'season_score_total', 'recent_score_total',
                 'pace_efficiency_total', 'opponent_context_total', 'pace_coverage')
INCUMBENT_VERSION = 'nba-total-incumbent-1'
KIND = 'nba-totals-season-pace-v1'
FEATURE_LAG_S = 4 * 3600
MIN_TEAM_GAMES = 10
TEAM_WINDOW = 20
SHRINKAGES = (5., 10., 20.)
RECENT_WEIGHTS = (0., .25, .5)
METHODS = ('incumbent', 'intercept', 'season', 'pace')
DEFAULT_SD_TOTAL = 16.5
ET = ZoneInfo('America/New_York')
NBA_TEAMS = frozenset(('ATL', 'BOS', 'BKN', 'CHA', 'CHI', 'CLE', 'DAL', 'DEN', 'DET', 'GSW',
                       'HOU', 'IND', 'LAC', 'LAL', 'MEM', 'MIA', 'MIL', 'MIN', 'NOP', 'NYK',
                       'OKC', 'ORL', 'PHI', 'PHX', 'POR', 'SAC', 'SAS', 'TOR', 'UTA', 'WAS'))
PREPROCESSING = 'two-seasons-20-rows-four-hour-lag-current-shrink-recent-v1'
PROBABILITY = 'normal-integer-continuity-conditional-nonpush-v1'
MODEL_BASE = 'pre_totals_spread_projection_v1'
IDENTITY_FIELDS = ('schema', 'kind', 'feature_names', 'training_cutoff', 'params', 'intercept',
                   'sd_total', 'phase_dates', 'phase_rows', 'phase_receipt_max', 'phase_asof_min',
                   'preprocessing', 'probability', 'family', 'model_base')


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('NBA totals value must be a finite number')
    return float(value)


def _time(value):
    if isinstance(value, bool):
        raise ValueError('Invalid NBA totals time')
    if isinstance(value, datetime):
        value = (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as exc:
            raise ValueError('Invalid NBA totals time') from exc
        value = (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    stamp = _finite(value)
    try:
        datetime.fromtimestamp(stamp, timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError('Invalid NBA totals time range') from exc
    return stamp


def _season(stamp):
    when = datetime.fromtimestamp(stamp, timezone.utc)
    return when.year if when.month >= 7 else when.year - 1


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def artifact_sha256(artifact):
    return hashlib.sha256(_json(artifact).encode()).hexdigest()


def _version(artifact):
    sealed = {k: artifact[k] for k in IDENTITY_FIELDS}
    sealed.update({k: artifact[k] for k in ('code_sha256', 'training_sources_sha256',
                                           'evaluation_protocol_sha256') if k in artifact})
    return 'nba-total-v1-' + artifact_sha256(sealed)[:20]


def seal_version(artifact):
    """Return a new version after the offline learner attaches envelope seals."""
    sealed = deepcopy(artifact)
    sealed['version'] = _version(sealed)
    return validate_artifact(sealed)


def _pace(row):
    raw, duration = row.get('raw_possessions'), row.get('duration_minutes')
    if raw is None or duration is None:
        return None
    return _finite(raw) * 48. / _finite(duration)


def _prepare(rows, cutoff, received_by, season):
    grouped = defaultdict(dict)
    games = defaultdict(dict)
    seen = {}
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError('Invalid NBA totals input row')
        team, opponent = raw.get('team'), raw.get('opponent')
        gid = raw.get('game_id')
        if (team not in NBA_TEAMS or opponent not in NBA_TEAMS or team == opponent
                or isinstance(gid, bool) or not isinstance(gid, (str, int))
                or not str(gid).strip() or (isinstance(gid, int) and gid <= 0)):
            raise ValueError('Invalid NBA totals team/game identity')
        stamp = _time(raw.get('played_at'))
        if stamp >= cutoff:
            raise ValueError('NBA totals score is later than its feature cutoff')
        row_season = _season(stamp)
        declared = raw.get('season_start_year', row_season)
        if isinstance(declared, bool) or declared != row_season:
            raise ValueError('NBA totals season identity conflict')
        points, allowed = _finite(raw.get('points')), _finite(raw.get('opponent_points'))
        if any(not 1 <= x <= 250 or x != int(x) for x in (points, allowed)):
            raise ValueError('Invalid NBA totals final score')
        if raw.get('received_at') is not None and _time(raw['received_at']) > received_by:
            raise ValueError('NBA totals source receipt is later than availability cutoff')
        possessions, duration = raw.get('raw_possessions'), raw.get('duration_minutes')
        if possessions is not None and not 20 <= _finite(possessions) <= 250:
            raise ValueError('Invalid NBA totals raw possessions')
        if duration is not None:
            minutes = _finite(duration)
            if not 48 <= minutes <= 98 or (minutes - 48) % 5 != 0:
                raise ValueError('Invalid NBA totals verified duration')
        if raw.get('pace48') is not None:
            pace = _finite(raw['pace48'])
            normalized = _pace(raw)
            if normalized is None or not math.isclose(pace, normalized, rel_tol=1e-9, abs_tol=1e-8):
                raise ValueError('NBA totals normalized pace conflict')
        # Exact duplicates are harmless. Conflicting optional evidence must fail
        # too: otherwise row order could silently select a duration or receipt.
        key = str(gid)
        signature = (opponent, stamp, points, allowed, possessions, duration,
                     raw.get('pace48'), raw.get('received_at'), raw.get('home'))
        existing = seen.get((team, key))
        if existing is not None and existing != signature:
            raise ValueError('NBA totals duplicate identity conflict')
        seen[(team, key)] = signature
        other = games[key].get(opponent)
        if other and (other['opponent'] != team or _time(other['played_at']) != stamp
                      or other['points'] != allowed or other['opponent_points'] != points):
            raise ValueError('NBA totals mirrored game score conflict')
        if games[key] and team not in games[key] and opponent not in games[key]:
            raise ValueError('NBA totals game/team identity conflict')
        games[key][team] = raw
        if row_season in (season - 1, season):
            grouped[team][key] = (signature, dict(raw))
    return {team: sorted((r[1] for r in by_id.values()),
                         key=lambda r: (_time(r['played_at']), str(r['game_id'])))
            for team, by_id in grouped.items()}


def _rates(rows, season):
    current = [r for r in rows if _season(_time(r['played_at'])) == season]
    prior = [r for r in rows if _season(_time(r['played_at'])) == season - 1]

    def score(items):
        return dict(count=len(items), offense=statistics.mean(r['points'] for r in items),
                    defense=statistics.mean(r['opponent_points'] for r in items)) if items else None

    def possession(items):
        items = [r for r in items if _pace(r) is not None]
        return dict(count=len(items), pace=statistics.mean(_pace(r) for r in items),
                    offense_ppp=statistics.mean(r['points'] / r['raw_possessions'] for r in items),
                    defense_ppp=statistics.mean(r['opponent_points'] / r['raw_possessions'] for r in items)) if items else None

    pooled = possession(rows)
    return dict(current=score(current), prior=score(prior), recent=score(rows[-5:]),
                pace_current=possession(current), pace_prior=possession(prior),
                pace_recent=possession(rows[-5:]), pace=pooled['pace'] if pooled else None,
                offense_ppp=pooled['offense_ppp'] if pooled else None,
                defense_ppp=pooled['defense_ppp'] if pooled else None,
                count=len(rows), pace_count=pooled['count'] if pooled else 0)


def _blend(rates, key, shrinkage, recent_weight, *, possession=False):
    prefix = 'pace_' if possession else ''
    current, prior, recent = (rates[prefix + suffix] for suffix in ('current', 'prior', 'recent'))
    if not current and not prior:
        return None
    if not prior:
        value = current[key]
    elif not current:
        value = prior[key]
    else:
        weight = current['count'] / (current['count'] + shrinkage)
        value = weight * current[key] + (1 - weight) * prior[key]
    if recent:
        value = (1 - recent_weight) * value + recent_weight * recent[key]
    return _finite(value)


def _totals(context, shrinkage=10., recent_weight=.25):
    home, away = (context['team_rates'][context[side]] for side in ('home', 'away'))
    scoring = sum(_blend(rate, key, shrinkage, recent_weight)
                  for rate in (home, away) for key in ('offense', 'defense')) / 2.
    recent = sum(rate['recent'][key] for rate in (home, away) for key in ('offense', 'defense')) / 2.
    pace_total = scoring
    adjustment = 0.
    if context['coverage']['pace_supported']:
        pace = sum(_blend(rate, 'pace', shrinkage, recent_weight, possession=True)
                   for rate in (home, away)) / 2.
        efficiency = sum(_blend(rate, key, shrinkage, recent_weight, possession=True)
                         for rate in (home, away) for key in ('offense_ppp', 'defense_ppp')) / 2.
        pace_total = pace * efficiency
        if context['coverage']['opponent_supported']:
            league = sum(rate[key] for rate in (home, away) for key in ('offense_ppp', 'defense_ppp')) / 4.
            opposition = context['opponent_rates']
            adjustment = pace * (4 * league - sum(opposition[team][key]
                for team in (context['home'], context['away']) for key in ('offense_ppp', 'defense_ppp'))) / 2.
    return scoring, recent, pace_total, adjustment


def features(home, away, rows, tipoff, *, as_of, baseline_total, reconstruction_cutoff=None):
    tip, stamp, baseline_total = _time(tipoff), _time(as_of), _finite(baseline_total)
    if home not in NBA_TEAMS or away not in NBA_TEAMS or home == away:
        raise ValueError('Invalid NBA totals matchup identity')
    if stamp >= tip or not 50 <= baseline_total <= 500:
        raise ValueError('Invalid NBA totals forecast time or incumbent total')
    received_by = stamp if reconstruction_cutoff is None else _time(reconstruction_cutoff)
    if received_by < stamp:
        raise ValueError('NBA totals reconstruction cutoff predates as_of')
    cutoff = min(stamp, tip - FEATURE_LAG_S)
    season = _season(tip)
    grouped = _prepare(rows, cutoff, received_by, season)
    windows = {team: grouped.get(team, [])[-TEAM_WINDOW:] for team in (home, away)}
    coverage = {}
    for side, team in (('home', home), ('away', away)):
        selected = windows[team]
        current = sum(_season(_time(r['played_at'])) == season for r in selected)
        coverage[side] = dict(rows=len(selected), current_season_rows=current,
                              prior_season_rows=len(selected) - current,
                              current_weight=(current / (current + 10.) if len(selected) > current
                                              else float(bool(current))),
                              pace_rows=sum(_pace(r) is not None for r in selected))
    selected = { (r['team'], str(r['game_id'])): r for values in windows.values() for r in values }
    context = dict(schema=1, status='ok', home=home, away=away, tipoff=tip, as_of=stamp,
                   feature_cutoff=cutoff, baseline_total=baseline_total, coverage=coverage,
                   selected_rows=list(selected.values()), reconstructed=reconstruction_cutoff is not None,
                   reconstruction_cutoff=reconstruction_cutoff,
                   availability='reconstructed_at_cutoff' if reconstruction_cutoff is not None else 'received_by_asof')
    coverage['pace_supported'] = all(coverage[side]['pace_rows'] >= max(5, coverage[side]['rows'] / 2)
                                     for side in ('home', 'away'))
    coverage['opponent_supported'] = False
    coverage['opponent_feature_rows'] = 0
    if any(len(values) < MIN_TEAM_GAMES for values in windows.values()):
        context.update(status='unavailable', fallback='insufficient_score_history', values=[], team_rates={})
        return context
    context['team_rates'] = {team: _rates(values, season) for team, values in windows.items()}
    opponent_rates = {}
    for team, values in windows.items():
        estimates = []
        for row in values:
            prior = [r for r in grouped.get(row['opponent'], [])
                     if _time(r['played_at']) <= _time(row['played_at']) - FEATURE_LAG_S
                     and _pace(r) is not None][-TEAM_WINDOW:]
            if len(prior) >= 5:
                estimates.append((statistics.mean(r['points'] / r['raw_possessions'] for r in prior),
                                  statistics.mean(r['opponent_points'] / r['raw_possessions'] for r in prior)))
                selected.update({(r['team'], str(r['game_id'])): r for r in prior})
        opponent_rates[team] = dict(rows=len(estimates),
            offense_ppp=statistics.mean(v[0] for v in estimates) if estimates else None,
            defense_ppp=statistics.mean(v[1] for v in estimates) if estimates else None)
    context['opponent_rates'] = opponent_rates
    coverage['opponent_supported'] = all(r['rows'] >= 5 for r in opponent_rates.values())
    coverage['opponent_feature_rows'] = len(selected) - sum(len(v) for v in windows.values())
    context['selected_rows'] = list(selected.values())
    score, recent, pace, opponent = _totals(context)
    context['values'] = [baseline_total, score, recent, pace, opponent, float(coverage['pace_supported'])]
    return context


def fit_candidate(examples, training_cutoff, *, min_fit=660, min_development=120, min_calibration=120):
    """Select a finite family on earlier dates, then fit only calibration sigma.

    Examples contain game_id/ts/as_of/label_received_at/actual_total/baseline_total
    and a features() context. Optional explicit phases must be supplied for every
    row and may only be fit/development/calibration. Outer labels never belong
    here. Default floors require 900 target games over at least two seasons.
    """
    cutoff = _time(training_cutoff)
    minimums = dict(fit=min_fit, development=min_development, calibration=min_calibration)
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 2 for v in minimums.values()):
        raise ValueError('Invalid NBA totals phase minimum')
    rows = [dict(row) for row in examples]
    if any(r.get('phase') not in (None, 'fit', 'development', 'calibration') for r in rows):
        raise ValueError('NBA totals outer/unknown phase is forbidden in fitting')
    explicit = any(r.get('phase') is not None for r in rows)
    if explicit and not all(r.get('phase') is not None for r in rows):
        raise ValueError('NBA totals explicit phase must cover every example')
    identities = set()
    for row in rows:
        row['ts'], row['as_of'] = _time(row['ts']), _time(row['as_of'])
        row['label_received_at'] = _time(row['label_received_at'])
        gid = str(row['game_id'])
        if not gid or gid in identities:
            raise ValueError('NBA totals duplicate fitting game identity')
        identities.add(gid)
        if row['as_of'] >= row['ts'] or not row['ts'] <= row['label_received_at'] <= cutoff:
            raise ValueError('NBA totals fitting label receipt/cutoff conflict')
        row['day'] = datetime.fromtimestamp(row['ts'], ET).date().isoformat()
        if not 2 <= _finite(row['actual_total']) <= 500:
            raise ValueError('Invalid NBA totals training label')
        _finite(row['baseline_total'])
        ctx = row['context']
        if any(str(r.get('game_id')) == gid for r in ctx.get('selected_rows', [])):
            raise ValueError('NBA totals target game is present in its training context')
        if ctx.get('status') != 'ok' or ctx.get('as_of') != row['as_of'] or ctx.get('tipoff') != row['ts']:
            raise ValueError('NBA totals training context identity conflict')
        if ctx.get('baseline_total') != row['baseline_total']:
            raise ValueError('NBA totals incumbent/context conflict')
        if ctx.get('reconstructed') and _time(ctx['reconstruction_cutoff']) > cutoff:
            raise ValueError('NBA totals reconstruction receipt exceeds training cutoff')
        _totals(ctx)
    rows.sort(key=lambda r: (r['ts'], str(r['game_id'])))
    if len({_season(r['ts']) for r in rows}) < 2:
        raise ValueError('NBA totals fitting needs multiple seasons')
    groups = defaultdict(list)
    for row in rows:
        groups[row['day']].append(row)
    phases = dict(fit=[], development=[], calibration=[])
    if explicit:
        for group in groups.values():
            names = {r['phase'] for r in group}
            if len(names) != 1:
                raise ValueError('NBA totals date overlaps phases')
            phases[names.pop()].extend(group)
    else:
        remaining = sorted(groups)
        for name in ('calibration', 'development'):
            while remaining and len(phases[name]) < minimums[name]:
                phases[name][0:0] = groups[remaining.pop()]
        phases['fit'] = [r for day in remaining for r in groups[day]]
    if any(len(phases[name]) < minimums[name] for name in phases):
        raise ValueError('Insufficient NBA totals fit/development/calibration history')
    for earlier, later in (('fit', 'development'), ('development', 'calibration')):
        if (max(r['day'] for r in phases[earlier]) >= min(r['day'] for r in phases[later])
                or max(r['label_received_at'] for r in phases[earlier]) >= min(r['as_of'] for r in phases[later])):
            raise ValueError('NBA totals phase date/label receipt chronology conflict')

    candidates = [dict(method='incumbent', shrinkage=10., recent_weight=.25),
                  dict(method='intercept', shrinkage=10., recent_weight=.25)]
    candidates += [dict(method=method, shrinkage=shrink, recent_weight=recent)
                   for method in ('season', 'pace') for shrink in SHRINKAGES for recent in RECENT_WEIGHTS]
    scored = []
    for index, params in enumerate(candidates):
        if params['method'] == 'pace' and not any(
                r['context']['coverage']['pace_supported'] and r['context']['coverage']['opponent_supported']
                for r in phases['fit']):
            continue
        intercept = 0. if params['method'] == 'incumbent' else statistics.mean(
            r['actual_total'] - _candidate_mean(params, r['context'], r['baseline_total'])[0]
            for r in phases['fit'])
        errors = [abs(r['actual_total'] - (_candidate_mean(params, r['context'], r['baseline_total'])[0] + intercept))
                  for r in phases['development']]
        scored.append((statistics.mean(errors), index, params, intercept))
    _, _, params, intercept = min(scored, key=lambda value: (value[0], value[1]))
    residuals = [r['actual_total'] - (_candidate_mean(params, r['context'], r['baseline_total'])[0] + intercept)
                 for r in phases['calibration']]
    artifact = dict(schema=1, kind=KIND, feature_names=list(FEATURE_NAMES), training_cutoff=cutoff,
                    params=params, intercept=intercept,
                    sd_total=max(1., min(100., math.sqrt(statistics.mean(x * x for x in residuals)))),
                    phase_dates={name: sorted({r['day'] for r in group}) for name, group in phases.items()},
                    phase_rows={name: len(group) for name, group in phases.items()},
                    phase_receipt_max={name: max(r['label_received_at'] for r in group) for name, group in phases.items()},
                    phase_asof_min={name: min(r['as_of'] for r in group) for name, group in phases.items()},
                    preprocessing=PREPROCESSING, probability=PROBABILITY,
                    family='incumbent-intercept-season-pace-20-fixed-candidates-v1', model_base=MODEL_BASE)
    artifact['version'] = _version(artifact)
    return validate_artifact(artifact)


def validate_artifact(artifact):
    try:
        if (not isinstance(artifact, dict) or (artifact.get('schema'), artifact.get('kind')) != (1, KIND)
                or artifact.get('feature_names') != list(FEATURE_NAMES)
                or artifact.get('preprocessing') != PREPROCESSING or artifact.get('probability') != PROBABILITY
                or artifact.get('model_base') != MODEL_BASE
                or artifact.get('family') != 'incumbent-intercept-season-pace-20-fixed-candidates-v1'):
            raise ValueError('Invalid NBA totals artifact contract')
        params = artifact['params']
        if (set(params) != {'method', 'shrinkage', 'recent_weight'} or params['method'] not in METHODS
                or _finite(params['shrinkage']) not in SHRINKAGES or _finite(params['recent_weight']) not in RECENT_WEIGHTS):
            raise ValueError('Invalid NBA totals fixed candidate parameters')
        cutoff = _time(artifact['training_cutoff'])
        _finite(artifact['intercept'])
        if not 1 <= _finite(artifact['sd_total']) <= 100:
            raise ValueError('Invalid NBA totals calibrated uncertainty')
        if params['method'] == 'incumbent' and artifact['intercept'] != 0:
            raise ValueError('NBA totals incumbent candidate may not alter its mean')
        dates = artifact['phase_dates']
        if set(dates) != {'fit', 'development', 'calibration'}:
            raise ValueError('Invalid NBA totals phase dates')
        for name in dates:
            if not dates[name] or dates[name] != sorted(set(dates[name])):
                raise ValueError('Invalid NBA totals phase dates')
            for day in dates[name]:
                datetime.strptime(day, '%Y-%m-%d')
            count = artifact['phase_rows'][name]
            if isinstance(count, bool) or not isinstance(count, int) or count < 2:
                raise ValueError('Invalid NBA totals phase counts')
            if _time(artifact['phase_receipt_max'][name]) > cutoff:
                raise ValueError('Invalid NBA totals phase receipt cutoff')
            if _time(artifact['phase_asof_min'][name]) >= _time(artifact['phase_receipt_max'][name]):
                raise ValueError('Invalid NBA totals phase receipt chronology')
        if (max(dates['fit']) >= min(dates['development'])
                or max(dates['development']) >= min(dates['calibration'])):
            raise ValueError('NBA totals phase date overlap')
        if any(artifact['phase_receipt_max'][earlier] >= artifact['phase_asof_min'][later]
               for earlier, later in (('fit', 'development'), ('development', 'calibration'))):
            raise ValueError('NBA totals phase label receipt chronology conflict')
        if artifact['version'] != _version(artifact):
            raise ValueError('NBA totals artifact version seal mismatch')
        _json(artifact)
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError('Invalid NBA totals artifact contract') from exc
    return artifact


def _candidate_mean(params, context, baseline_total):
    method = params['method']
    if method in ('incumbent', 'intercept'):
        return baseline_total, method
    score, _, pace, opponent = _totals(context, params['shrinkage'], params['recent_weight'])
    if method == 'pace' and context['coverage']['pace_supported'] and context['coverage']['opponent_supported']:
        return pace + opponent, 'pace_efficiency_opponent'
    return score, 'season_score' if method == 'season' else 'season_score_missing_pace_or_opponent'


def predict(artifact, context, baseline_total):
    baseline_total = _finite(baseline_total)
    retained = dict(total=baseline_total, sd_total=DEFAULT_SD_TOTAL, method='incumbent',
                    branch='incumbent', applied=False, fallback='no_validated_artifact')
    if artifact is None:
        return retained
    validate_artifact(artifact)
    if _time(artifact['training_cutoff']) >= _time(context['as_of']):
        return dict(retained, fallback='artifact_not_available_asof')
    if context.get('status') != 'ok':
        return dict(retained, fallback=context.get('fallback', 'insufficient_score_history'))
    if context.get('baseline_total') != baseline_total:
        raise ValueError('NBA totals prediction incumbent/context conflict')
    mean, branch = _candidate_mean(artifact['params'], context, baseline_total)
    total = _finite(mean + artifact['intercept'])
    if not 50 <= total <= 500:
        raise ValueError('NBA totals prediction outside supported score bounds')
    return dict(total=total, sd_total=artifact['sd_total'], method=artifact['params']['method'],
                branch=branch, applied=True,
                fallback='missing_pace_or_opponent' if branch.endswith('missing_pace_or_opponent') else None)


def compose(projection, input_map, artifact=None, model_state=None):
    """Add totals after spread projection; active-state lookup belongs to caller.

    Explicit artifact is for independently verified candidate replay. Serving
    supplies one active model_state snapshot containing artifact/sha256 and the
    activation time. Unknown or unavailable components retain incumbent numbers.
    """
    if 'total_base_total' in projection or 'total_component_version' in projection:
        raise ValueError('NBA totals projection is already composed; supply its raw spread projection')
    base_total = _finite(projection['total'])
    base_sd_total = _finite(projection.get('sd_total',
        (projection.get('uncertainty') or {}).get('sd_total', DEFAULT_SD_TOTAL)))
    if base_sd_total <= 0:
        raise ValueError('Invalid NBA totals raw base uncertainty')
    if not isinstance(input_map, dict) or input_map.get('schema') != 'nba-totals-input-1':
        raise ValueError('Invalid NBA totals input map contract')
    payload = {k: v for k, v in input_map.items() if k != 'sha256'}
    digest = artifact_sha256(payload)
    if input_map.get('sha256') != digest:
        raise ValueError('NBA totals input map hash mismatch')
    if input_map.get('reconstructed') or input_map.get('reconstruction_cutoff') is not None:
        raise ValueError('NBA totals reconstructed inputs cannot enter serving composition')
    if not input_map.get('game_id') or any(str(r.get('game_id')) == str(input_map['game_id']) for r in input_map['rows']):
        raise ValueError('NBA totals target game cannot be a feature input')
    out = deepcopy(projection)
    context = features(input_map['home'], input_map['away'], input_map['rows'], input_map['tipoff'],
                       as_of=input_map['as_of'], baseline_total=base_total)
    state = model_state or {}
    if artifact is None:
        artifact = state.get('artifact')
    if artifact is not None:
        validate_artifact(artifact)
        if state.get('sha256') is not None and state['sha256'] != artifact_sha256(artifact):
            raise ValueError('NBA totals active artifact hash mismatch')
        for name in ('version', 'training_cutoff'):
            if state.get(name) is not None and state[name] != artifact[name]:
                raise ValueError('NBA totals active artifact identity conflict')
    result = predict(artifact, context, base_total)
    if state.get('activated_at') is not None and _time(state['activated_at']) > context['as_of']:
        result = dict(result, total=projection['total'], applied=False, fallback='artifact_not_available_asof')
    applied = result['applied']
    if applied:
        total = round(result['total'], 2)
        margin = _finite(projection.get('home_margin', projection.get('margin')))
        home_score, away_score = round((total + margin) / 2., 2), round((total - margin) / 2., 2)
        out.update(total=total, home_score=home_score, away_score=away_score,
                   proj_home=home_score, proj_away=away_score, sd_total=result['sd_total'])
        out['uncertainty'] = dict(out.get('uncertainty') or {}, sd_total=result['sd_total'])
    coverage = deepcopy(context['coverage'])
    if applied:
        shrinkage, recent_weight = artifact['params']['shrinkage'], artifact['params']['recent_weight']
        coverage.update(season_shrinkage=shrinkage, recent_weight=recent_weight)
        for side in ('home', 'away'):
            current, prior = coverage[side]['current_season_rows'], coverage[side]['prior_season_rows']
            coverage[side]['current_weight'] = current / (current + shrinkage) if prior else float(bool(current))
    out.update(total_component_version=artifact['version'] if applied else INCUMBENT_VERSION,
               total_base_total=base_total, total_base_sd_total=base_sd_total,
               total_artifact_sha256=artifact_sha256(artifact) if applied else None,
               total_training_cutoff=artifact['training_cutoff'] if applied else None,
               total_input_map=deepcopy(input_map), total_input_map_sha256=digest,
               total_method=result['method'] if applied else 'incumbent',
               total_branch=result['branch'] if applied else 'incumbent',
               total_applied=applied, total_fallback=result['fallback'],
               total_coverage=coverage, total_feature_names=list(FEATURE_NAMES) if applied else [],
               total_probability_method=PROBABILITY,
               total_uncertainty=dict(sd_total=out.get('sd_total', DEFAULT_SD_TOTAL),
                                      method='separate_calibration_rmse' if applied else 'retained_incumbent'))
    return out


def total_probabilities(mean, sd, line):
    """Actual full-game total; integer pushes excluded from binary Brier."""
    mean, sd, line = _finite(mean), _finite(sd), _finite(line)
    if sd <= 0:
        raise ValueError('NBA totals uncertainty must be positive')
    cdf = lambda value: .5 * math.erfc(-(value - mean) / (sd * math.sqrt(2.)))
    survival = lambda value: .5 * math.erfc((value - mean) / (sd * math.sqrt(2.)))
    if line == int(line):
        under = cdf(line - .5)
        over = survival(line + .5)
        push = max(0., 1 - under - over)
    else:
        under = cdf(line)
        over, push = survival(line), 0.
    decided = under + over
    # Both nonpush tails may underflow in the deterministic-push limit. Their
    # conditional limit is symmetric at the line, or favors the nearer tail.
    conditional = over / decided if decided else (.5 if mean == line else float(mean > line))
    return dict(over=over, under=under, push=push,
                conditional_over=conditional, conditional_under=1 - conditional)
