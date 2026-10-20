"""Conservative, deterministic participation evidence from official NFL sources."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import pdfplumber

from . import db, nfl_identity
from .providers.base import client

VERSION = 'official-participation-v1'
CACHE_SECONDS = 5 * 60
CLUBS = dict(zip(
    'ari atl bal buf car chi cin cle dal den det gb hou ind jax kc lac lar lv mia min ne no nyg nyj phi pit sea sf tb ten wsh'.split(),
    ('azcardinals.com atlantafalcons.com baltimoreravens.com buffalobills.com panthers.com '
     'chicagobears.com bengals.com clevelandbrowns.com dallascowboys.com denverbroncos.com '
     'detroitlions.com packers.com houstontexans.com colts.com jaguars.com chiefs.com '
     'chargers.com therams.com raiders.com miamidolphins.com vikings.com patriots.com '
     'neworleanssaints.com giants.com newyorkjets.com philadelphiaeagles.com steelers.com '
     'seahawks.com 49ers.com buccaneers.com tennesseetitans.com commanders.com').split()))
HOSTS = {'www.nfl.com', 'static.www.nfl.com', 'static.clubs.nfl.com'} | {'www.' + h for h in CLUBS.values()}


def _allowed(url):
    p = urlparse(url)
    roster = (p.hostname == 'github.com' and re.fullmatch(
        r'/nflverse/nflverse-data/releases/download/weekly_rosters/roster_weekly_\d{4}\.csv', p.path))
    return (p.scheme == 'https' and (p.hostname in HOSTS or roster or p.hostname == 'release-assets.githubusercontent.com') and not p.username and
            not p.password and p.port in (None, 443))


def fetch(url: str, now: float) -> tuple[bytes, dict]:
    """Share a bounded source cache across freezes; archive exact received bytes."""
    if not _allowed(url):
        raise ValueError('untrusted official-source URL')
    key = 'official_participation:' + hashlib.sha256(url.encode()).hexdigest()
    cached = db.kv_get(key) or {}
    if 0 <= now - cached.get('retrieved_at', -CACHE_SECONDS) < CACHE_SECONDS:
        if not cached.get('ok'):
            raise ValueError('official source unavailable; awaiting scheduled retry')
        row = db.q1('SELECT payload FROM nfl_prop_grade_source_artifacts WHERE sha256=?', (cached['sha256'],))
        raw = bytes(row['payload']) if row else b''
        if hashlib.sha256(raw).hexdigest() != cached['sha256']:
            raise ValueError('official-source cache hash mismatch')
        return raw, {k: v for k, v in cached.items() if k != 'ok'}
    try:
        target = url
        for _ in range(4):
            if not _allowed(target):
                raise ValueError('untrusted official-source redirect')
            if urlparse(target).hostname == 'release-assets.githubusercontent.com' and urlparse(url).hostname != 'github.com':
                raise ValueError('official source redirected outside NFL hosts')
            response = client().get(target, timeout=25, follow_redirects=False)
            if response.is_redirect:
                target = urljoin(target, response.headers.get('location', ''))
                if (urlparse(url).hostname in {'www.' + h for h in CLUBS.values()} and
                        urlparse(target).hostname != urlparse(url).hostname):
                    raise ValueError('official club redirect changed publisher')
                continue
            response.raise_for_status()
            raw = response.content
            if not raw or len(raw) > (50 if '/weekly_rosters/' in url else 10) * 1024 * 1024:
                raise ValueError('official source empty or too large')
            break
        else:
            raise ValueError('official-source redirect limit')
        digest = hashlib.sha256(raw).hexdigest()
        db.ex('INSERT OR IGNORE INTO nfl_prop_grade_source_artifacts VALUES(?,?,?,?)',
              (digest, url, now, raw))
        ref = {'source': url, 'sha256': digest, 'retrieved_at': now,
               'file': 'db:nfl_prop_grade_source_artifacts:' + digest}
        db.kv_set(key, {**ref, 'ok': True})
        return raw, ref
    except Exception:
        db.kv_set(key, {'ok': False, 'retrieved_at': now})
        raise


class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.gamebooks = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and 'Game Book' in attrs.get('data-analytics', ''):
            self.gamebooks.append(attrs.get('href', ''))


class Transactions(HTMLParser):
    def __init__(self):
        super().__init__(); self.rows = []; self.cells = None; self.cell = None; self.year = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'option' and 'selected' in attrs:
            match = re.fullmatch(r'/team/transactions/(\d{4})', attrs.get('value', ''))
            if match: self.year = int(match[1])
        if tag == 'tr': self.cells = []
        if tag == 'td' and self.cells is not None: self.cell = []

    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == 'td' and self.cell is not None:
            self.cells.append(' '.join(' '.join(self.cell).split())); self.cell = None
        if tag == 'tr' and self.cells is not None:
            if len(self.cells) == 2: self.rows.append(self.cells)
            self.cells = None


def reserved(raw: bytes, season: int, game_day, full_name: str) -> bool:
    """Only an explicit prior-day IR placement, with no later player transaction."""
    parser = Transactions(); parser.feed(raw.decode('utf-8'))
    if parser.year != season:
        raise ValueError('official transactions season mismatch')
    mentions = []
    for day, text in parser.rows:
        if not re.search(r'(?<!\w)' + re.escape(full_name) + r'(?!\w)', text, re.I): continue
        date = datetime.strptime(f'{season}/{day}', '%Y/%m/%d').date()
        if date <= game_day: mentions.append((date, text))
    if not mentions: return False
    last = max(d for d, _ in mentions)
    if last >= game_day: return False  # no time of day: same-day moves are ambiguous
    rows = [t for d, t in mentions if d == last]
    pattern = r'(?:^|\.\s+)Placed\s+[A-Z/]+\s+' + re.escape(full_name) + r'\s+on Injured Reserve\.'
    return (len(rows) == 1 and len(re.findall(re.escape(full_name), rows[0], re.I)) == 1
            and re.search(pattern, rows[0], re.I) is not None)


def identities(raw: bytes, roster_raw: bytes, quotes: list[dict], game: str) -> dict:
    """Resolve full name and short Game Book name uniquely to frozen GSIS and team."""
    season, week = int(game.split('-')[1]), int(game.split('-')[3])
    rows = [r for r in csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
            if r.get('season') == str(season) and r.get('season_type') == 'REG' and int(r['week']) < week]
    roster = [r for r in csv.DictReader(io.StringIO(roster_raw.decode('utf-8-sig')))
              if r.get('season') == str(season) and r.get('week') == str(week) and r.get('game_type') == 'REG']
    result = {}
    for q in quotes:
        gsis = q['gsis_player_id']
        snapshot = db.q1('SELECT team FROM nfl_prop_snapshots WHERE id=?', (q['quote_id'],))
        team = nfl_identity.team_code(snapshot['team']) if snapshot else None
        candidates = [r for r in rows if r['player_id'] == gsis and nfl_identity.team_code(r['team']) == team
                      and r.get('player_display_name', '').casefold() == q['player_name'].casefold()]
        names = {(r.get('player_name'), r.get('player_display_name')) for r in candidates}
        if len(names) != 1 or team not in game.split('-')[-2:]: continue
        short, full = next(iter(names))
        aliases = {r['player_id'] for r in rows if nfl_identity.team_code(r['team']) == team
                   and r.get('player_name') == short}
        if aliases != {gsis} or not short: continue
        matches = [r for r in roster if r.get('gsis_id') == gsis and nfl_identity.team_code(r['team']) == team
                   and r.get('full_name', '').casefold() == full.casefold()]
        if len(matches) != 1: continue
        jersey = matches[0].get('jersey_number', '')
        if not jersey.isdigit() or not 0 <= int(jersey) <= 99: continue
        owners = {r['gsis_id'] for r in roster if nfl_identity.team_code(r['team']) == team
                  and r.get('jersey_number') == jersey}
        if owners != {gsis}: continue
        result[gsis] = {'short': short, 'full': full, 'team': team, 'jersey': int(jersey),
                        'reserve': matches[0].get('status') == 'RES'}
    return result


def _table(text, heading, width, sums):
    lines = text.splitlines()
    starts = [i for i, s in enumerate(lines) if s.startswith(heading + ' ')]
    if len(starts) != 1: raise ValueError('Game Book stat heading missing or ambiguous')
    rows = {}; total = None
    for line in lines[starts[0] + 1:]:
        parts = line.split()
        if not parts: continue
        if len(parts) != width: raise ValueError('Game Book stat row incomplete')
        if parts[0] in rows: raise ValueError('Game Book duplicate stat player')
        if parts[0] == 'Total': total = parts[1:]; break
        rows[parts[0]] = parts[1:]
    if total is None or not rows: raise ValueError('Game Book stat total missing')
    for index in sums:
        if sum(float(r[index]) for r in rows.values()) != float(total[index]):
            raise ValueError('Game Book stat totals do not reconcile')
    return rows, total


def parse_book(raw: bytes, game: str, kickoff: float, score: dict, people: dict) -> dict:
    """A supported layout must prove game identity, participation and complete stats."""
    if not raw.startswith(b'%PDF-'): raise ValueError('official Game Book is not PDF')
    day = datetime.fromtimestamp(kickoff, ZoneInfo('America/New_York')).date()
    away, home = game.split('-')[-2:]
    names = [nfl_identity.canonical_team(t) for t in (away, home)]
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        first = pdf.pages[0].extract_text() or ''
        if ('National Football League Game Summary' not in first or
                f'{day.month:02d}/{day.day:02d}/{day.year}' not in first or
                not all(n in first for n in names)):
            raise ValueError('Game Book game/date identity mismatch')
        for label, name, points in [('VISITOR', names[0], score['away']), ('HOME', names[1], score['home'])]:
            matches = re.findall(r'^' + label + r':\s*' + re.escape(name) + r'\s+([\d\s]+)$', first, re.M)
            if len(matches) != 1 or int(matches[0].split()[-1]) != points:
                raise ValueError('Game Book final score mismatch')
        final = [p for p in pdf.pages if 'Final Individual Statistics' in (p.extract_text() or '')
                 and 'RUSHING ATT' in (p.extract_text() or '')]
        if len(final) != 1: raise ValueError('Game Book final stat page missing or ambiguous')
        snaps = [p for p in pdf.pages if 'Playtime Percentage' in (p.extract_text() or '')]
        words = pdf.pages[0].extract_words()
        field_goals = [w for i, w in enumerate(words[:-1]) if w['text'] == 'Field'
                       and words[i + 1]['text'] == 'Goals']
        if len(field_goals) != 1: raise ValueError('Game Book roster boundary missing')
        results = {}
        for gsis, person in people.items():
            side = 0 if person['team'] == away else 1
            def half(page):
                return page.crop((side * page.width / 2, 0, (side + 1) * page.width / 2, page.height))
            page = pdf.pages[0]
            roster = page.crop((side * page.width / 2, 0, (side + 1) * page.width / 2,
                                field_goals[0]['top'])).extract_text() or ''
            if names[side] not in roster or not all(x in roster for x in ('Substitutions', 'Did Not Play', 'Not Active')):
                raise ValueError('Game Book roster layout incomplete')
            short = person['short']; token = r'(?<![\w.])' + re.escape(short) + r'(?![\w.-])'
            roster_token = r'(?<!\w)[A-Z/0-9]+\s+' + str(person['jersey']) + r'\s+' + token
            inactive = roster.split('Not Active', 1)[1].split('1 2 3 4', 1)[0]
            dnp = roster.split('Did Not Play', 1)[1].split('Not Active', 1)[0]
            active = roster.split('Did Not Play', 1)[0]
            flags = [bool(re.search(roster_token, x)) for x in (active, dnp, inactive)]
            if len(re.findall(token, roster)) > 1: raise ValueError('Game Book ambiguous roster name')
            if sum(flags) > 1: raise ValueError('Game Book participation conflict')
            if flags[2]:
                results[gsis] = {'active_roster': False, 'snap_played': False, 'official_stats': {}}
                continue
            stat_text = half(final[0]).extract_text() or ''
            if names[side] not in stat_text: raise ValueError('Game Book stat team mismatch')
            rush, rt = _table(stat_text, 'RUSHING', 6, (0, 1, 4))
            passing, pt = _table(stat_text, 'PASSING', 9, (0, 1, 2, 4, 6))
            receiving, ct = _table(stat_text, 'PASS RECEIVING', 7, (0, 1, 2, 5))
            if float(ct[1]) != float(pt[1]) or float(ct[2]) != float(pt[2]):
                raise ValueError('Game Book passing/receiving totals conflict')
            played = False
            for page in snaps:
                words = half(page).extract_words()
                found = [w for w in words if w['text'] == short]
                if len(found) > 1: raise ValueError('Game Book ambiguous playtime player')
                if found:
                    word = found[0]
                    row = [w['text'] for w in words if abs(w['top'] - word['top']) < 2 and w['x0'] > word['x1']]
                    played |= any(x.isdigit() and int(x) > 0 for x in row)
            if flags[1] and played: raise ValueError('Game Book DNP/playtime conflict')
            if not flags[0] and not flags[1]:
                if played or any(short in table for table in (rush, passing, receiving)):
                    raise ValueError('Game Book roster/stats conflict')
                continue
            if not flags[1] and not played: continue
            values = {'rushing_attempts': float(rush.get(short, ['0'] * 5)[0]),
                      'rushing_yards': float(rush.get(short, ['0'] * 5)[1]),
                      'passing_yards': float(passing.get(short, ['0'] * 8)[2]),
                      'receptions': float(receiving.get(short, ['0'] * 6)[1]),
                      'receiving_yards': float(receiving.get(short, ['0'] * 6)[2])}
            results[gsis] = {'active_roster': True, 'snap_played': played, 'official_stats': values}
        return results


def resolve(game: str, quotes: list[dict], stats_raw: bytes, kickoff: float, score: dict,
            now: float) -> tuple[dict, dict]:
    season, week, away, home = int(game.split('-')[1]), int(game.split('-')[3]), *game.split('-')[-2:]
    roster_raw, roster_source = fetch(f'https://github.com/nflverse/nflverse-data/releases/download/weekly_rosters/roster_weekly_{season}.csv', now)
    people = identities(stats_raw, roster_raw, quotes, game)
    if not people: raise ValueError('official participation needs unique week/player/jersey identity')
    # NFL slugs use club nicknames. Take them from the closed canonical team map.
    slug = lambda t: nfl_identity.canonical_team(t).split()[-1].lower()
    page_url = f'https://www.nfl.com/games/{slug(away)}-at-{slug(home)}-{season}-reg-{week}'
    html, page_source = fetch(page_url, now)
    links = Links(); links.feed(html.decode('utf-8'))
    urls = set(links.gamebooks)
    if (len(urls) != 1 or not _allowed(next(iter(urls))) or
            urlparse(next(iter(urls))).hostname not in {'www.nfl.com', 'static.www.nfl.com', 'static.clubs.nfl.com'}):
        raise ValueError('official game page lacks a unique Game Book')
    raw, book_source = fetch(next(iter(urls)), now)
    players = parse_book(raw, game, kickoff, score, people)
    sources = [page_source, roster_source]
    day = datetime.fromtimestamp(kickoff, ZoneInfo('America/New_York')).date()
    for gsis, person in people.items():
        if gsis in players: continue
        if not person['reserve']: continue
        url = f'https://www.{CLUBS[person["team"]]}/team/transactions/{season}'
        transactions, ref = fetch(url, now); sources.append(ref)
        if reserved(transactions, season, day, person['full']):
            players[gsis] = {'active_roster': False, 'snap_played': False, 'official_stats': {}}
    if not players: raise ValueError('official sources do not establish player participation')
    return {'players': players, 'reviewer': VERSION, 'reviewed_at': now}, {
        **book_source, 'supporting_sources': sources, 'parser_version': VERSION}
