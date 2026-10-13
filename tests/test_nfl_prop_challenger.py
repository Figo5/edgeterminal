import numpy as np
import pytest
from edgeterminal import nfl_prop_challenger as model


def test_probability_counts_pushes_separately_and_rejects_bad_residuals():
    residuals = np.array([-1., 0., 1.]*100)
    over = model.probability(5., 5., residuals)
    under = model.probability(-5., -5., -residuals)
    assert over == under
    assert over+under < 1
    with pytest.raises(ValueError):
        model.probability(5,5,[float('nan')]*100)


def test_usage_forecast_responds_to_volume_not_future_labels():
    f = {'targets_last3': 8., 'receptions_last5': 4., 'targets_last5': 6.}
    first = model.usage([{'features': f, 'target': 999}], 'receptions')[0]
    assert first == pytest.approx(8*4/6)
    f['targets_last3'] = 3.
    assert model.usage([{'features': f, 'target': 0}], 'receptions')[0] == 2.


def test_weight_selection_keeps_baseline_on_a_tie():
    assert model.choose_weight(np.array([1.]), np.array([1.]), np.array([1.])) == 0.
    assert model.choose_weight(np.array([2.]), np.array([1.]), np.array([2.])) == 1.


def test_rank_caps_related_player_markets_and_deduplicates_sides():
    rows = [dict(game_id='g',player_id='p',market=str(i),side='over',quote_id=i,
                 probability_over=.9-i*.01,probability_under=.1+i*.01) for i in range(5)]
    rows.append({**rows[0], 'side': 'under', 'quote_id': 9})
    rows += [dict(game_id='g',player_id='q'+str(i),market='receptions',side='under',quote_id=20+i,
                  probability_over=.3,probability_under=.7) for i in range(4)]
    chosen = model.rank(rows)['g']
    assert len(chosen) == 5
    assert sum(r['player_id']=='p' for r in chosen) == 2
    assert len({(r['player_id'],r['market']) for r in chosen}) == 5


def test_shadow_rejects_changed_model_before_loading(tmp_path, monkeypatch):
    import hashlib, json
    monkeypatch.setattr(model, 'DIRECTORY', tmp_path)
    artifact = tmp_path/'artifact'
    artifact.mkdir()
    manifest = {'training_cutoff': [2026,3], 'mode': 'shadow', 'models_sha256': 'original'}
    raw = json.dumps(manifest).encode()
    (artifact/'manifest.json').write_bytes(raw)
    (artifact/'models.joblib').write_bytes(b'changed')
    (tmp_path/'active.json').write_text(json.dumps({'path':str(artifact),'sha256':hashlib.sha256(raw).hexdigest()}))
    with pytest.raises(ValueError, match='model changed'):
        model.shadow('batch', 2026, 6)


def test_shadow_record_uses_first_selection_and_verified_stat(tmp_path, monkeypatch):
    import hashlib,json
    from edgeterminal import nfl_prop_grading
    monkeypatch.setattr(model,'DIRECTORY',tmp_path)
    artifact=tmp_path/'artifact'
    artifact.mkdir()
    (tmp_path/'active.json').write_text(json.dumps({'path':str(artifact),'sha256':'manifest'}))
    row=dict(game_id='game',player_id='p',market='receptions',side='over',line=3.5,
             projection=5.,baseline_projection=4.)
    for i,side in enumerate(('over','under')):
        p=artifact/f'shadow-{i}.json'
        raw=json.dumps({'created_at':i,'model_manifest_sha256':'manifest','top_five':{'game':[{**row,'side':side}]}}).encode()
        p.write_bytes(raw)
        p.with_suffix('.sha256').write_text(hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(model.db,'q',lambda *a:[{'freeze_sha':'f'}])
    monkeypatch.setattr(nfl_prop_grading,'_latest',lambda *a:[{'canonical_game_id':'game','player_id':'p',
        'market':'receptions','actual_stat':6.,'research_statistical_result':'won'}])
    assert model.report()['record']=={'won':1}
    result=json.loads((tmp_path/'performance.json').read_text())
    assert result['same_selections_comparison']=={'n':1,'baseline_mae':2.,'challenger_mae':1.}
    (artifact/'shadow-0.json').write_text('{}')
    with pytest.raises(ValueError,match='snapshot changed'):
        model.report()
