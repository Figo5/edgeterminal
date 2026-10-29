# Always-on runtime preparation — 2026-10-29

This is a deployment preparation and isolated validation record. No remote
service was provisioned, no public URL was created, no paid storage was enabled,
and neither local service was disabled or migrated.

## Orchestration evidence

Codex inspected the existing hosted-mode boundaries, Git freeze guard, SQLite
research lock, persistent model/source paths and service backup logic. A bounded
Claude CLI infrastructure critique completed with exit 0, followed by a separate
bounded read-only code/security review, also exit 0. Both had tools disabled and
received only the supplied architecture/source text; no credentials were supplied.

Review changes incorporated:

- Recheck clean source and image revision before every worker child, including
  ignored model/deployment files and top-level Python import-shadowing files.
- Require release metadata for the explicit `always_on` runtime; reject a mounted
  checkout `.env`, including a broken symlink. Keep provider keys off the web service.
- Run nonroot with read-only root filesystem, loopback-only web publishing,
  separate service env files, Docker init and process-group shutdown.
- Use actual completed research tick health with sufficient process-timeout
  tolerance. A failed latest tick remains visible rather than being masked by an
  older successful one. The app's source/forecast freshness remains separate.
- Document state ownership for UID/GID 10001 before startup.
- Use online SQLite backup, standalone destination journal normalization, full
  research/cache/report checksums, original source-file times and empty-only
  restore. Reject symlinks, traversal, unlisted bytes, nested SQLite and unfinished
  WAL/journal/temp files instead of copying those databases raw.

Two critique suggestions did not fit the verified implementation: the normal
worker inserts ticks after completion, not at start, and a lease in a separately
copied database cannot exclude a second host. Consequently completed timestamps
and explicit one-writer cutover are used. The lock path/primitive were verified
against `research_refresh.LOCK` (`db filename + '-research.lock'`, `flock`).

## Verification

- **21 focused Python tests passed** on the local Python 3.9 runtime. They cover
  real Git provenance/dirty-source rejection, private hosting, source-key isolation,
  skipped locks, failed cycles, child shutdown, health staleness/future times,
  WAL-aware DB restore with immutable triggers, tamper/traversal/symlink refusal,
  source modification-time preservation, and omitted secrets/old backups/logs.
- Python syntax checks passed for runtime, archive and both test modules.
- `docker compose ... config --quiet` passed with temporary nonsecret env files.
- The Linux Python 3.11 runtime image built successfully. Key web/scientific
  dependencies are constrained to the already-tested local versions; `pip check`
  passed. The Docker build context contained requirements/constraints only.
- A separate Docker smoke used a clean temporary Git checkout, full-schema dummy
  SQLite DB, read-only code/root, nonroot user, Docker init, and **network disabled**.
  Web health returned success; worker health read a successful fixture tick;
  `/api/refresh` returned **403**. The smoke container was removed afterward.
  Only copied application source (including its packaged fixed parameters) and
  dummy state were mounted. No live database, research archive, learned runtime
  artifact, historical freeze, grade or provider key was used, and no outbound
  provider call was possible.

The validation image is not the final release image. Rebuild with the approved
committed revision after review. Host selection, a private HTTPS origin, actual
state-transfer verification, owner sign-in, and cutover remain deployment steps.
An off-host backup destination and its schedule have not been configured.
