"""Local saved research profile and optional invited beta accounts.

Hosted sessions use random, hashed, expiring tokens in HttpOnly cookies. Only an
operator can invite or grant access. Membership never modifies a forecast.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import smtplib
import time
from email.message import EmailMessage

from fastapi import HTTPException, Request

from . import db, nfl_top_five
from .config import settings
from .integrity import parse_time

COOKIE = 'et_session'
SESSION_SECONDS = 14 * 86400
LOGIN_SECONDS = 15 * 60
LOCAL_ID = 'local-owner'


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def local_owner() -> dict:
    db.ex('INSERT OR IGNORE INTO members(id,created_at) VALUES(?,?)', (LOCAL_ID, time.time()))
    return db.q1('SELECT * FROM members WHERE id=?', (LOCAL_ID,))


def current(request: Request) -> dict | None:
    if not settings.hosted_mode:
        if not request.client or request.client.host not in {'127.0.0.1', '::1', 'testclient'}:
            raise HTTPException(403, 'Local profiles require a loopback connection')
        return local_owner()
    token = request.cookies.get(COOKIE, '')
    if not token or len(token) > 128:
        return None
    return db.q1('SELECT m.* FROM members m JOIN member_sessions s ON m.id=s.member_id '
                 'WHERE s.token_hash=? AND s.expires_at>?', (digest(token), time.time()))


def require(request: Request) -> dict:
    member = current(request)
    if not member:
        raise HTTPException(401, 'Sign in to save research')
    return member


def full_access(request: Request) -> bool:
    if not settings.hosted_mode:
        return True
    member = current(request)
    return bool(member and (member.get('access_until') or 0) > time.time())


def profile(request: Request) -> dict:
    member = current(request)
    return {'signed_in': member is not None, 'local': not settings.hosted_mode,
            'email': member.get('email') if member else None,
            'full_access': full_access(request),
            'access_until': member.get('access_until') if member else None,
            'email_login_available': bool(settings.smtp_host and settings.smtp_from),
            'billing_enabled': False,
            'membership': 'Local research beta' if not settings.hosted_mode else
                          'Invited beta member' if full_access(request) else 'Free preview'}


def rate_limit(bucket: str, limit: int, seconds: int = 3600) -> None:
    now = time.time()
    with db.tx() as conn:
        count = conn.execute('SELECT COUNT(*) FROM member_auth_attempts WHERE bucket=? AND attempted_at>?',
                             (bucket, now - seconds)).fetchone()[0]
        if count >= limit:
            raise HTTPException(429, 'Too many attempts. Try again later.')
        conn.execute('INSERT INTO member_auth_attempts(bucket,attempted_at) VALUES(?,?)', (bucket, now))
        conn.execute('DELETE FROM member_auth_attempts WHERE attempted_at<?', (now - 86400,))


def email_address(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', value):
        raise HTTPException(422, 'Enter a valid email address')
    return value


def login_link(member_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with db.tx() as conn:
        conn.execute('DELETE FROM member_login_tokens WHERE member_id=? OR expires_at<=?', (member_id, now))
        conn.execute('INSERT INTO member_login_tokens VALUES(?,?,?,?)',
                     (digest(token), member_id, now, now + LOGIN_SECONDS))
    # Fragments are not sent to the web server or included in its access log.
    return settings.public_url + '/#signin=' + token


def send_login(email: str, link: str) -> None:
    message = EmailMessage()
    message['From'], message['To'] = settings.smtp_from, email
    message['Subject'] = 'Your EdgeTerminal sign-in link'
    message.set_content('Open this link within 15 minutes to sign in to EdgeTerminal.\n\n' + link +
                        '\n\nIf you did not request it, ignore this email.')
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)


def request_login(email: str, request: Request) -> dict:
    email = email_address(email)
    rate_limit('request:' + digest(request.client.host), 12)
    rate_limit('email:' + digest(email), 3)
    if not settings.smtp_host or not settings.smtp_from:
        raise HTTPException(503, 'Email sign-in is not enabled. Ask the beta owner for an invitation link.')
    member = db.q1('SELECT id FROM members WHERE email=?', (email,))
    if member:
        try:
            send_login(email, login_link(member['id']))
        except Exception:
            raise HTTPException(503, 'The sign-in email could not be delivered. Try again later.') from None
    return {'message': 'If this email has an invitation, a sign-in link has been sent.'}


def consume(token: str, request: Request) -> str:
    rate_limit('consume:' + digest(request.client.host), 20, 900)
    if len(token) > 128:
        raise HTTPException(400, 'This sign-in link is invalid or expired')
    session = secrets.token_urlsafe(32)
    with db.tx() as conn:
        row = conn.execute('SELECT member_id FROM member_login_tokens WHERE token_hash=? AND expires_at>?',
                           (digest(token), time.time())).fetchone()
        if not row:
            raise HTTPException(400, 'This sign-in link is invalid or expired')
        conn.execute('DELETE FROM member_login_tokens WHERE token_hash=?', (digest(token),))
        previous = request.cookies.get(COOKIE)
        if previous:
            conn.execute('DELETE FROM member_sessions WHERE token_hash=?', (digest(previous),))
        conn.execute('DELETE FROM member_sessions WHERE expires_at<=?', (time.time(),))
        conn.execute('INSERT INTO member_sessions VALUES(?,?,?)',
                     (digest(session), row['member_id'], time.time() + SESSION_SECONDS))
    return session


def selected_rows() -> dict[int, dict]:
    return {r['quote_id']: {**r, 'game_id': g['game_id'], 'freeze_sha': g['freeze_sha']}
            for g in nfl_top_five.view()['games'] for r in g['rows']}


def save_prop(member: dict, quote_id: int) -> None:
    row = selected_rows().get(quote_id)
    if not row:
        raise HTTPException(404, 'This prop is not a locked research selection')
    count = db.q1('SELECT COUNT(*) n FROM member_saved_props WHERE member_id=?', (member['id'],))['n']
    if count >= 200 and not db.q1('SELECT 1 FROM member_saved_props WHERE member_id=? AND quote_id=?', (member['id'], quote_id)):
        raise HTTPException(409, 'Saved selection limit reached. Remove an old selection first.')
    db.ex('INSERT OR IGNORE INTO member_saved_props VALUES(?,?,?,?,?)',
          (member['id'], quote_id, row['game_id'], time.time(), json.dumps(row, sort_keys=True)))


def saved(member: dict) -> dict:
    latest = selected_rows()
    props = []
    for stored in db.q('SELECT * FROM member_saved_props WHERE member_id=? ORDER BY saved_at DESC', (member['id'],)):
        row = latest.get(stored['quote_id'])
        if row is None:
            raise ValueError('saved research selection no longer matches its source')
        original = json.loads(stored['payload'])
        for key in ('game_id', 'freeze_sha', 'quote_id', 'player_id', 'market', 'side', 'line', 'projection'):
            if original.get(key) != row.get(key):
                raise ValueError('saved research selection identity mismatch')
        props.append({**row, 'saved_at': stored['saved_at']})
    return {'props': props, 'games': db.q('SELECT e.id,e.home_team,e.away_team,e.start_time '
                                         'FROM member_saved_games s JOIN events e ON e.id=s.game_id '
                                         'WHERE s.member_id=? ORDER BY e.start_time', (member['id'],))}


def sync_alerts() -> dict:
    """Local inbox alerts only. Opt-in outbound delivery is a future launch step."""
    from .product_insights import prop as insight
    stored = db.q('SELECT member_id,quote_id,game_id,saved_at FROM member_saved_props')
    if not stored:
        return {'created': 0}
    rows, created = selected_rows(), 0
    for saved_row in stored:
        row = rows.get(saved_row['quote_id'])
        if row is None:
            raise ValueError('saved selection source mismatch')
        changes = []
        if row['result'] != 'pending' and (row.get('graded_at') or 0) > saved_row['saved_at']:
            title = {'won': 'Win', 'lost': 'Loss', 'push': 'Push', 'void': 'Void'}.get(row['result'], row['result'])
            changes.append(('result', str(row.get('revision')), f"{row['player']} {row['market'].replace('_', ' ')}: {title}. Verified stat: {row.get('actual')}.", row['graded_at']))
        quote = insight(row)['latest_quote']
        if quote and quote['line_changed'] and quote['observed_at'] > saved_row['saved_at']:
            changes.append(('line', str(quote['id']), f"{row['player']}: {row['sportsbook']} line moved from {row['line']:g} to {quote['line']:g}. Your original selection remains fixed.", quote['observed_at']))
        for kind, version, message, stamp in changes:
            key = digest(f"{saved_row['member_id']}:{saved_row['quote_id']}:{kind}:{version}")
            with db.tx() as conn:
                cursor = conn.execute('INSERT OR IGNORE INTO member_alerts(id,member_id,created_at,kind,game_id,message) '
                                      'VALUES(?,?,?,?,?,?)', (key, saved_row['member_id'], stamp, kind, row['game_id'], message))
                created += cursor.rowcount
    return {'created': created}


def invite(email: str, days: int) -> str:
    email = email_address(email)
    if not 1 <= days <= 365:
        raise ValueError('Beta access must be between 1 and 365 days')
    with db.tx() as conn:
        conn.execute('INSERT INTO members(id,email,created_at,access_until) VALUES(?,?,?,?) '
                     'ON CONFLICT(email) DO UPDATE SET access_until=excluded.access_until',
                     (secrets.token_hex(16), email, time.time(), time.time() + days * 86400))
    return login_link(db.q1('SELECT id FROM members WHERE email=?', (email,))['id'])


def main() -> None:
    parser = argparse.ArgumentParser(description='Invite a beta tester. This command never sends email or charges money.')
    parser.add_argument('email')
    parser.add_argument('--days', type=int, default=30)
    args = parser.parse_args()
    print(invite(args.email, args.days))


if __name__ == '__main__':
    main()
