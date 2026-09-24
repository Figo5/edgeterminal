from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _frontend():
    return ((ROOT / "edgeterminal/web/index.html").read_text(),
            (ROOT / "edgeterminal/web/app.js").read_text())


def test_frontend_is_nfl_projection_product_with_secondary_research():
    html, js = _frontend()
    visible = html + js
    assert "NFL projections" in html
    for section in ('["nfl","NFL"]', '["performance","Performance"]',
                    '["paper","Paper Portfolio"]', '["research","Research"]'):
        assert section in js
    assert '"CFB"' not in visible
    assert '"NBA"' not in visible
    assert '"PGA"' not in visible
    assert "Week 3 game and prop freezes" in js


def test_browser_reads_stored_state_without_owning_ingestion():
    html, js = _frontend()
    assert 'api("/api/product/nfl")' in js
    assert '/api/product/nfl/${encodeURIComponent(id)}' in js
    assert "/api/product/performance" in js
    assert "/api/research/paper" in js
    assert '"/api/refresh"' not in js
    assert '"/api/event/' not in js
    assert "refreshBtn" not in html
    assert '#backBtn' in js


def test_matchup_props_and_confidence_are_visible_before_research():
    _, js = _frontend()
    assert "PLAYER PROPS" in js
    assert "Over ${odds(r.over_price)}" in js
    assert "Under ${odds(r.under_price)}" in js
    assert "Confidence: ${pct(r.confidence)}" in js
    assert "model-estimated probability" in js
    assert "paper-mark" in js
    assert "filterSortProps" in js
    assert "Research documents" in js


def test_unsupported_betting_controls_are_absent():
    html, js = _frontend()
    assert 'value="player_prop"' not in html
    assert 'value="outright"' not in html
    assert "Best Parlay" not in js
    assert "PAPER / EXPERIMENTAL" in js
