from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_frontend_is_nfl_only_and_has_focused_sections():
    html = (ROOT / "edgeterminal/web/index.html").read_text()
    js = (ROOT / "edgeterminal/web/app.js").read_text()
    visible = html + js
    assert "NFL Research Terminal" in html
    assert 'state = { sport: "NFL"' in js
    assert '"NFL Slate"' in js
    assert '"CFB"' not in visible
    assert '"NBA"' not in visible
    assert '"PGA"' not in visible
    assert 'data-section="moves"' not in js


def test_frontend_has_clear_no_official_pick_state_and_compact_provider_health():
    js = (ROOT / "edgeterminal/web/app.js").read_text()
    html = (ROOT / "edgeterminal/web/index.html").read_text()
    assert "NO OFFICIAL PICKS RIGHT NOW" in js
    assert "required price, confidence, data-quality, and market-coverage thresholds" in js
    assert "providerHealth" in html
    assert "Data providers" not in html


def test_frontend_preserves_nfl_scoped_requests_and_game_back_navigation():
    js = (ROOT / "edgeterminal/web/app.js").read_text()
    assert 'JSON.stringify({sports:["NFL"]})' in js
    assert 'sport=NFL' in js
    assert '#backBtn' in js


def test_frontend_has_no_dead_market_filters():
    """Props cannot be priced without usage data, so the control must not exist."""
    html = (ROOT / "edgeterminal/web/index.html").read_text()
    assert 'value="player_prop"' not in html
    assert 'value="outright"' not in html


def test_props_live_only_in_game_detail_with_category_filters():
    js = (ROOT / "edgeterminal/web/app.js").read_text()
    assert "propsPanel" in js and "loadProps" in js
    assert 'data-propcat="${k}"' in js
    for category in ("td", "passing", "rushing", "receiving"):
        assert f'["{category}",' in js
    # fetched on demand from the event endpoint, never bundled into the dashboard
    assert "/props?category=" in js
    assert "dashboard?.props" not in js
