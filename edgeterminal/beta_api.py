"""Account and saved-research endpoints. No market ingestion or pick mutation."""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import db, members
from .config import settings

router = APIRouter()


class LoginBody(BaseModel):
    email: str = Field(max_length=254)


class TokenBody(BaseModel):
    token: str = Field(max_length=128)


@router.get('/api/account')
def account(request: Request):
    return members.profile(request)


@router.post('/api/auth/request-link')
def request_link(body: LoginBody, request: Request):
    return members.request_login(body.email, request)


@router.post('/api/auth/signin')
def signin(body: TokenBody, request: Request, response: Response):
    token = members.consume(body.token, request)
    response.set_cookie(members.COOKIE, token, max_age=members.SESSION_SECONDS,
                        httponly=True, secure=settings.hosted_mode, samesite='lax', path='/')
    return {'ok': True}


@router.post('/api/auth/signout')
def signout(request: Request, response: Response):
    token = request.cookies.get(members.COOKIE)
    if token:
        db.ex('DELETE FROM member_sessions WHERE token_hash=?', (members.digest(token),))
    response.delete_cookie(members.COOKIE, path='/', secure=settings.hosted_mode, httponly=True, samesite='lax')
    return {'ok': True}


@router.get('/api/account/saved')
def saved(request: Request):
    member = members.require(request)
    payload = members.saved(member)
    # Expired beta accounts can still review started games and their outcomes.
    if not members.full_access(request):
        payload['props'] = [r for r in payload['props'] if r['kickoff_utc'] <= time.time()]
    return payload


@router.post('/api/account/props/{quote_id}')
def save_prop(quote_id: int, request: Request):
    if not members.full_access(request):
        raise HTTPException(403, 'Full beta access is required to save player props')
    members.save_prop(members.require(request), quote_id)
    return {'ok': True}


@router.delete('/api/account/props/{quote_id}')
def remove_prop(quote_id: int, request: Request):
    db.ex('DELETE FROM member_saved_props WHERE member_id=? AND quote_id=?',
          (members.require(request)['id'], quote_id))
    return {'ok': True}


@router.post('/api/account/games/{game_id}')
def save_game(game_id: str, request: Request):
    member = members.require(request)
    if not db.q1("SELECT id FROM events WHERE id=? AND sport='NFL'", (game_id,)):
        raise HTTPException(404, 'Unknown NFL game')
    db.ex('INSERT OR IGNORE INTO member_saved_games VALUES(?,?,?)', (member['id'], game_id, time.time()))
    return {'ok': True}


@router.delete('/api/account/games/{game_id}')
def remove_game(game_id: str, request: Request):
    db.ex('DELETE FROM member_saved_games WHERE member_id=? AND game_id=?', (members.require(request)['id'], game_id))
    return {'ok': True}


@router.get('/api/account/alerts')
def alerts(request: Request):
    return {'alerts': db.q('SELECT id,created_at,kind,game_id,message,read_at FROM member_alerts '
                           'WHERE member_id=? ORDER BY created_at DESC LIMIT 100', (members.require(request)['id'],))}


@router.post('/api/account/alerts/read')
def read_alerts(request: Request):
    db.ex('UPDATE member_alerts SET read_at=? WHERE member_id=? AND read_at IS NULL',
          (time.time(), members.require(request)['id']))
    return {'ok': True}
