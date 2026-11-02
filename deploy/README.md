# Always-on EdgeTerminal, with private phone access

This package prepares a single Linux host to run the existing web app and the
normal research worker every five minutes. It does not provision a server,
publish the app, transfer credentials, disable the Mac service, or change the
model by itself. Docker running on the same sleeping Mac is still dependent on
that Mac; moving the backend to an always-on host is what removes that dependency.

The phone opens the hosted web app and can add it to its Home Screen. It is a
viewer, not the background research worker. Apple's [Home Screen web-app
instructions](https://support.apple.com/en-lamr/guide/iphone/iphea86e5236/ios)
describe that installation. No browser background timer is required for grading.

## Deployment contract

- One authoritative host, one clean committed checkout, one persistent local
  state directory. The services mount the same `data/` and `reports/`.
- One web process in `HOSTED_MODE=true`; one source/grading worker controller
  calling the normal `edgeterminal.research_refresh run` CLI; and one independent
  offline learner. Existing source cadence, quota guards, append-only ledgers
  and shared publication lock remain in effect.
- Keep every supported-game pick. This package adds no selection threshold,
  official-pick activation, stakes, Odds API call, or Codex automation.
- SQLite WAL and `fcntl` locks require a normal local Linux filesystem; do not
  place this state on NFS/SMB or share it between independently running hosts.
- The read-only checkout includes genuine Git metadata for future NFL freezes.
  Runtime preflight rejects dirty code, a revision differing from the built image,
  or changed requirements. No Git/code-integrity guard is bypassed.
- State includes the database, `data/research`, `data/nflverse`, and `reports`.
  Model artifacts, source snapshots, historical freezes and corrections move
  together. Do not migrate only the `.db` file.

## Independent learning lifecycle

The five-minute source/grading worker queues due learning without fitting or
replaying whole validation histories. Bounded historical source collection
remains with that worker. The learner consumes archived evidence, has its own
OS lock and persistent job/event ledger, and never receives the provider env
file. Its Compose service has no network, one CPU, background scheduling and
single-thread numeric-library limits. Model parameters and acceptance gates
are unchanged.

The ordinary controller retains its **900-second** cycle limit. The separate
learner controller and CLI have a **three-hour wall-time** limit; the CLI also
has a **one-hour CPU** budget. These are resource ceilings, not expected task
durations. Jobs record their own real start/end, elapsed and CPU time. The
previous two-hour observation covered whole-cycle wall time and did not measure
two hours of fitting CPU. `check-learner` reads an independent job heartbeat;
it never substitutes for a genuine source/grading tick.

One learner process owns each job until it exits. An old heartbeat cannot
authorize a second fitter while the OS lock is held. Stopped or timed-out jobs
are recorded as interrupted and retried by the next owner. Candidate
registration checkpoints are stored in the same database transaction as the
proposal, so recovery resumes guarded publication instead of refitting a spent
holdout. Expensive proof verification runs before the brief research lock and
SQLite publication transaction; relevant source/result heads are checked again
before activation. Changed evidence or a busy capture lock defers publication.

About & data displays separate learning job health. Queuing a retry does not
erase a failed attempt, and a current source tick does not imply model learning
has completed successfully. The Mac version uses the separate
`com.edgeterminal.learning` LaunchAgent (`python -m
edgeterminal.learning_scheduler install` after committing/releasing reviewed
code). Its original app and five-minute source LaunchAgents stay separate.
Both still require the Mac to be awake; this does not complete Windows cutover.

## Host and access decision still needed

No usable deployment host is configured in this workspace. Before actual
cutover, choose an always-on Linux host, its monthly budget, and private access.
A Docker-capable VPS or an existing always-on home server fits this package.
For a free-only option, see [the Oracle Always Free preparation guide](ORACLE_FREE.md).
Size disk and RAM from actual use and the largest training task. At preparation
time the database was approximately 4.9 GB, research/cache files approximately
1 GB, and old local SQLite backups approximately 12 GB. Those old backups are
not needed in the initial state transfer, but daily backup retention and growth
need additional space. A small ephemeral web-service disk is unsuitable.

The default Compose port is **host loopback only**. For phone access, an option
is [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve), which
provides HTTPS within the private tailnet. Install/authenticate Tailscale on the
host and phone, restrict the tailnet policy to the owner's devices, and follow
its current Serve instructions to proxy the host's `127.0.0.1:8799`. Do not use
public Funnel. Set `EDGETERMINAL_PUBLIC_URL` to that exact private HTTPS origin.
An authenticated HTTPS reverse proxy on an approved host is another option.

Hosted preview boundaries remain enabled. Use the app's existing owner/member
sign-in for full upcoming props; anonymous preview deliberately hides them.
The operator `python -m edgeterminal.members EMAIL --days DAYS` creates a sign-in
link without sending email. Run it only on the authoritative host, keep the link
private, and do not put its token in shared logs or this repository.

## Prepare the reviewed release

1. Commit the reviewed changes, then clone/check out that exact commit on the
   host at `/srv/edgeterminal/app`. Keep `.env` out of that checkout. Never change
   code in place while either container is running.
2. Put service secrets in root-owned, mode-0600 files outside the checkout:
   `/etc/edgeterminal/web.env` and `/etc/edgeterminal/worker.env`. Only the worker
   file receives `SPORTSGAMEODDS_API_KEY`. Omit unused Odds API credentials.
   The web file may be empty or contain the existing SMTP settings needed for
   explicitly requested owner sign-in; no email is sent by this deployment.
3. Copy `host.env.example` outside the checkout and fill in the committed SHA,
   code directory, restored state directory, private HTTPS origin, and two env
   paths. Never put provider keys in that Compose configuration file.
4. Restore state using the process below. Ensure UID/GID `10001:10001` owns the
   state directory (`sudo chown -R 10001:10001 /srv/edgeterminal/state`);
   the runtime is nonroot. Code needs only read permission.
   The Docker image pins key dependencies to the verified local runtime through
   `constraints.txt`, and records requirement/constraint hashes; the
   build context allowlist excludes database files, archives and secrets.

The following commands are examples for the chosen host, not already executed:

```sh
docker compose --env-file /etc/edgeterminal/host.env -f deploy/compose.yaml config --quiet
docker compose --env-file /etc/edgeterminal/host.env -f deploy/compose.yaml build
docker compose --env-file /etc/edgeterminal/host.env -f deploy/compose.yaml run --rm worker python deploy/runtime.py preflight
docker compose --env-file /etc/edgeterminal/host.env -f deploy/compose.yaml up -d
docker compose --env-file /etc/edgeterminal/host.env -f deploy/compose.yaml ps
```

The host must start Docker on boot. `restart: unless-stopped` restarts exited
containers and restores them after a daemon reboot unless deliberately stopped.
[Docker restart documentation](https://docs.docker.com/engine/containers/start-containers-automatically/)
and [persistent-volume documentation](https://docs.docker.com/engine/storage/volumes/)
describe the underlying behavior. A container marked unhealthy is reported as
such; Docker does not automatically restart a still-running unhealthy process.
The worker itself times out a stalled cycle after 15 minutes and retries on the
next scheduled boundary, while emitting no raw provider/error payloads.

## Consistent state archive and cutover

`state_backup.py` uses SQLite online backup, including committed WAL data. It
holds the same research lock while copying associated files, normalizes the
temporary database to a standalone journal, and writes hashes for every included
file, together with original file modification times used by source-receipt
checks. Restore reapplies those times even if transport changed file timestamps.
The source DB/WAL is never deleted, copied raw, or checkpointed by this tool.
Symlinks, traversal, changed/unlisted archive files and nonempty restore targets
are rejected. Nested SQLite files or unfinished WAL/journal/temp files under
the filesystem archives are rejected rather than copied raw. Database triggers
survive the backup.

For a final migration, first stop the local web, research **and learning**
LaunchAgents and confirm both research and learning locks are free. The archive
tool is unchanged and its research lock alone cannot stop an offline fit from
writing a new proposal. On an already hosted machine, stop all three Compose
services before creating the final migration archive. A lock on one host cannot stop a worker on a
different copied database; neither a file lock nor a local DB lease solves that.
Keep the old local files intact and its services unloaded during cloud operation.

```sh
.venv/bin/python deploy/state_backup.py create /PRIVATE_LOCATION/cutover-archive
.venv/bin/python deploy/state_backup.py verify /PRIVATE_LOCATION/cutover-archive
```

Transfer that archive privately to the host and verify it again. Restore into a
new, empty state directory, separate from the Git checkout:

```sh
python deploy/state_backup.py verify /PRIVATE_LOCATION/cutover-archive
python deploy/state_backup.py restore /PRIVATE_LOCATION/cutover-archive --destination /srv/edgeterminal/state
```

Review the archive's `code_commit` and require the deployment checkout to match.
The restore tool only restores state; it does not clone or rewrite source code.
After verification and the original services are stopped, start the host services.
Check one actual successful research tick, quota accounting and immutable ledger
integrity before considering cutover complete. A no-target/cached cycle is valid;
no past-game selections may be backfilled to fill a gap.

The existing worker still maintains daily SQLite backups. For complete disaster
recovery, schedule this full archive tool on the host and transfer verified
archives to an approved off-host destination; this package does not create paid
storage or claim offsite backups are already configured. Restore tests should use
scratch state, never overwrite the running database. Archive hashes protect
consistency; preserve the manifest independently if authenticity is required.

Rollback requires stopping the hosted source and learning writers first, archiving their newest state,
restoring that state to the Mac, verifying it, and only then restarting launchd.
Do not restart the old Mac database while the host is still writing.

## Health and release checks

- Web health checks a local `/api/health` request with the configured host header.
- Worker health reads the latest actual `research_refresh_ticks` row and fails
  if absent, future-dated, failed, or completed more than 25 minutes ago. This
  process-health tolerance covers a 15-minute cycle plus a scheduling gap;
  individual source/forecast freshness remains a separate product status.
  A lock-skipped CLI
  invocation does not fabricate a successful tick.
- `data/runtime/worker.json` records sanitized controller status using atomic
  replacement; it never records raw output, API URLs, keys, or exception text.
- About & data continues using the app's real cadence/result status. Keep an
  external alert on worker health or observe the deployment dashboard; a serving
  webpage alone does not establish that updates are current.
- On the private phone URL, verify HTTPS, owner sign-in, the NBA date, current
  forecast/source times, background-worker status, and no mutation/debug routes.
  Add the URL to the Home Screen after those checks.

Focused local checks:

```sh
.venv/bin/python -m pytest -q tests/test_always_on_runtime.py tests/test_state_archive.py
```

A read-only Claude CLI infrastructure/security critique was completed during
preparation. The resulting package still requires code review, host selection,
and actual cutover verification; it is not an already-running cloud deployment.

Fly is possible with a different process supervisor, but its [volume model](https://docs.fly.io/volumes/overview)
attaches a volume to one Machine. Separate web and worker Machines cannot share
this same SQLite/lock directory. Both processes would need one Machine and one
volume, with [autostop disabled](https://docs.fly.io/launch/autostop-autostart).
The generic single-host package avoids introducing replication for this task.
