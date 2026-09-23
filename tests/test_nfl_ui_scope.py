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


def test_frontend_shows_research_only_board_and_compact_provider_health():
    js = (ROOT / "edgeterminal/web/app.js").read_text()
    html = (ROOT / "edgeterminal/web/index.html").read_text()
    assert "Research only" in js
    assert "Upcoming games remain PASS" in js
    assert "Forward shadow record:" in js
    assert "All designations are PASS" in js
    assert 'section: "games"' in js
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


def test_props_and_parlays_are_deferred_from_the_visible_game_flow():
    js = (ROOT / "edgeterminal/web/app.js").read_text()
    html = (ROOT / "edgeterminal/web/index.html").read_text()
    assert "${propsPanel()}" not in js
    assert "${parlayCard(" not in js
    assert "Player Props" not in html
    assert "Best Parlay" not in html
