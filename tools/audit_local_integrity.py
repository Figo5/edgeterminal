"""Audit and quarantine local prop rows that cannot be shown to belong to their event.

Reproducible integrity check for local state:

    python tools/audit_local_integrity.py --report        # dry run, prints findings
    python tools/audit_local_integrity.py --backup --apply

Quarantine never deletes or rewrites a record: it marks it so analysis and
evaluation skip it (see `db.quarantine`).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edgeterminal import db, integrity as I              # noqa: E402
from edgeterminal.analytics import usage as U            # noqa: E402
from edgeterminal.config import settings                 # noqa: E402
from edgeterminal.providers import registry as R         # noqa: E402

PLAYER_MARKETS = ("player_%",)
DEMO_PREFIX = "demo-"


def _usage_index(seasons=(2026, 2025)):
    """Usage rows per season, for participant evidence."""
    index = {}
    for season in seasons:
        plays = R.nflverse_plays(season)
        if plays:
            index[season] = U.aggregate_player_usage(plays, season=season)
    return index


def audit(apply: bool = False, backup: bool = False) -> dict:
    if backup:
        source = Path(settings.db_path)
        target_dir = source.parent / "backups"
        target_dir.mkdir(exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = target_dir / f"{source.stem}-pre-integrity-{stamp}.db"
        shutil.copy2(source, target)
        print(f"backup written: {target}")

    index = _usage_index()
    findings = {"expired_events": [], "off_matchup_rows": [], "quarantined_odds": 0,
                "quarantined_predictions": 0, "rows_scanned": 0, "events_scanned": 0}
    odds_rows = db.q("""SELECT id, event_id, market, selection, book, captured_at
                        FROM odds_snapshots
                        WHERE market LIKE 'player_%'""")
    findings["rows_scanned"] = len(odds_rows)
    by_event: dict[str, list] = {}
    for row in odds_rows:
        by_event.setdefault(row["event_id"], []).append(row)

    now = dt.datetime.now(dt.timezone.utc).timestamp()
    for event_id, rows in by_event.items():
        event = db.q1("SELECT * FROM events WHERE id=?", (event_id,))
        if not event:
            continue
        event = dict(event)
        findings["events_scanned"] += 1
        expired = I.event_is_expired(event, now)
        if expired:
            findings["expired_events"].append(dict(event_id=event_id, name=event.get("name"),
                                                   kickoff=event.get("start_time"),
                                                   rows=len(rows)))
        if str(event_id).startswith(DEMO_PREFIX):
            continue                      # demo fixtures are a separate family

        bad_ids = []
        for row in rows:
            player = row["selection"]
            for suffix in (" Over ", " Under "):
                player = player.split(suffix)[0]
            candidates = []
            for season, usage_rows in index.items():
                candidates += [dict(c, basis=f"season_{season}")
                               for c in U.find_candidates(usage_rows, player)]
            verdict = I.classify_player(player, candidates, event)
            if not I.is_model_eligible(verdict):
                bad_ids.append((row["id"], player, verdict["state"], verdict.get("team"),
                                verdict.get("reason")))
        if bad_ids:
            findings["off_matchup_rows"].append(dict(
                event_id=event_id, name=event.get("name"), rows=len(bad_ids),
                sample=[dict(player=p, state=s, team=t, reason=r)
                        for _, p, s, t, r in bad_ids[:5]]))
        if apply:
            for row_id, player, state, team, reason in bad_ids:
                db.quarantine("odds", str(row_id), event_id,
                              f"prop row player not confirmed for this event: {state}",
                              detail=dict(player=player, resolved_team=team, reason=reason))
                findings["quarantined_odds"] += 1
            # Only a *contaminated* event invalidates its predictions. A game
            # that merely finished is legitimate history and must stay gradeable.
            if bad_ids:
                for row in db.q("SELECT id FROM predictions WHERE event_id=?", (event_id,)):
                    db.quarantine("prediction", str(row["id"]), event_id,
                                  "event carried prop rows that could not be attributed")
                    findings["quarantined_predictions"] += 1
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write quarantine records")
    parser.add_argument("--backup", action="store_true", help="copy the database first")
    parser.add_argument("--report", action="store_true", help="dry run (default)")
    args = parser.parse_args()
    findings = audit(apply=args.apply, backup=args.backup)
    print(json.dumps(findings, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())