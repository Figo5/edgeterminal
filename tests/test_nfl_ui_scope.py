from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _frontend():
    return ((ROOT / "edgeterminal/web/index.html").read_text(),
            (ROOT / "edgeterminal/web/app.js").read_text())


def test_frontend_is_nfl_projection_product_with_secondary_research():
    html, js = _frontend()
    visible = html + js
    assert "NFL projections" in html
    for section in ('["nfl","Projections"]', '["performance","Season record"]',
                    '["research","About & data"]'):
        assert section in js
    assert '"CFB"' not in visible
    assert '"NBA"' not in visible
    assert '"PGA"' not in visible
    assert "Week 3 game and prop freezes" in js


def test_browser_reads_stored_state_without_owning_ingestion():
    html, js = _frontend()
    assert 'api(boardUrl())' in js
    assert '/api/product/nfl/${encodeURIComponent(id)}' in js
    assert "/api/product/performance" in js
    assert "/api/research/paper" in js
    assert '"/api/refresh"' not in js
    assert '"/api/event/' not in js
    assert "refreshBtn" not in html
    assert '#backBtn' in js


def test_matchup_props_and_confidence_are_visible_before_research():
    _, js = _frontend()
    assert "Top 5 player props" in js
    assert "Over ${odds(r.over_price)}" in js
    assert "Under ${odds(r.under_price)}" in js
    assert "Confidence: ${pct(r.confidence)}" in js
    assert "model-estimated probability" in js
    assert "paper-mark" in js
    assert "Research documents" in js
    assert "Weather and injury context" in js


def test_unsupported_betting_controls_are_absent():
    html, js = _frontend()
    assert 'value="player_prop"' not in html
    assert 'value="outright"' not in html
    assert "Best Parlay" not in js
    assert "PAPER / EXPERIMENTAL" in js


def test_chat_is_removed_and_original_dark_palette_is_active():
    html, js = _frontend()
    css = (ROOT / "edgeterminal/web/style.css").read_text()
    assert "Research chat" not in html and 'id="chat"' not in html
    assert "sendChat" not in js and "chatToggle" not in js
    assert "color-scheme:dark" in css and "color-scheme:light" not in css
