# Oracle Always Free option — checked 2026-10-29

This is a free-only host preparation guide. No Oracle account has been created,
resource provisioned, payment plan upgraded, state uploaded, or Mac service
stopped. The existing app remains local until a real host is configured and the
one-writer cutover is verified.

## Proposed size

Use **one `VM.Standard.A1.Flex` ARM instance, 1 OCPU, 4 GB RAM, and a 50 GB boot
volume**, running an Always Free-eligible Ubuntu ARM64 image. This leaves room
within the account's allowance rather than allocating its maximum immediately.
Check existing account usage before creating anything.

Oracle currently documents a combined A1 allowance equivalent to **2 OCPUs and
12 GB RAM**, and **200 GB total boot/block storage**, in the tenancy's home
region. These are account totals, not per-instance allocations. Older guides
advertising 4 OCPUs/24 GB are not the limits used here. The same documentation
lists five included volume backups and 20 GB of object storage for an
Always-Free-only account. [Official Oracle limits](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

The prepared Docker validation image was inspected as **`linux/arm64`**, matching
A1. Its nonroot/read-only web and worker-health smoke passed on that architecture.
Build the release image on the host from the approved commit; do not substitute
an x86-only image or bypass the code-revision checks.

Current live state needs approximately 6 GB without the Mac's old backups.
A 50 GB boot disk provides working space, but local backup retention, Docker
images and growing source history must be monitored. The default daily SQLite
backups alone can consume roughly another 15 GB at the current database size.
Keep boot volume data after an instance failure; do not delete it during recovery.

## Free-only account and capacity checks

1. Keep the account on its free plan. Do not select Pay As You Go, a paid shape,
   trial-credit-dependent resource, extra paid storage, or a licensed image.
2. Select the tenancy's home region, check Limits/Quotas/Usage, and confirm this
   proposed instance plus existing resources stays within the current free
   allocation. Verify the creation estimate is zero before proceeding.
3. Use the A1 Flex shape and Ubuntu ARM64 image, allocating only 1 OCPU/4 GB.
   Do not add a managed database, NAT gateway, public load balancer, or extra
   services for this single-host package.
4. If A1 capacity is unavailable, try another availability domain in the same
   home region or wait. Do not switch to a paid option to get around capacity.

Oracle states that free capacity can be unavailable and idle free instances can
be reclaimed. Therefore this is a no-cost candidate, not a guaranteed-uptime
service; a five-minute job does not establish protection against reclamation.
[Oracle capacity and reclamation policy](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

## Private phone access

Use an SSH key for administration and initially restrict inbound access to the
operator's address. The app's Compose configuration publishes `8799` only on the
host's loopback interface. Do not open that port publicly or expose local mode.

Install Tailscale on the Ubuntu host and phone, sign into the owner's private
tailnet, and use [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)
for a private HTTPS URL forwarding to `127.0.0.1:8799`. Restrict access to the
owner's devices; public Funnel is unnecessary. Set that exact HTTPS origin in
`/etc/edgeterminal/host.env`, then use the existing owner sign-in in EdgeTerminal.
Adding the URL to the phone's Home Screen makes it easy to open. The Oracle host
performs the background work even when the phone page is closed and the Mac is
asleep; the phone itself does not run the research worker.

Install Docker Engine and its Compose plugin using [Docker's Ubuntu
instructions](https://docs.docker.com/engine/install/ubuntu/), which include
ARM64 support. Follow [the deployment guide](README.md) for the clean checkout,
separate secret files, persistent state, nonroot ownership and release preflight.
No provider key belongs in the image, Git checkout, phone storage or a URL.

## Migration and validation

Use the existing `state_backup.py` tool to create and verify a consistent DB plus
source/model/report archive. For the final transfer, stop both local services
first; never run the Mac and Oracle workers against separate copies of the
ledger. Transfer privately, restore into a new state directory, and verify the
code commit and archive bytes before starting Compose.

Before treating the phone URL as live, confirm:

- The actual research worker has completed a tick and continues on its five-minute
  cadence without a browser open.
- `/api/product/nba/status` identifies the always-on runtime and current worker
  time; About & data does not report a missing macOS LaunchAgent on Linux.
- `/api/product/status` and internal refresh diagnostics distinguish source or
  grading failures from host/process state. Genuine source warnings remain
  visible instead of being hidden by the move.
- Current NBA forecast and source times advance, grading remains append-only,
  provider quotas remain enforced, and historical locks and freezes verify.
- The app survives a host reboot; owner sign-in works over HTTPS on the phone;
  ingestion/debug routes remain private.

The Mac services stay unloaded after cutover. To roll back, stop Oracle's writer,
bring its newest verified state back, and only then resume the Mac services.

## Backups within the free allocation

Keep at least one verified full state archive outside the instance; an archive
on the same boot disk is not sufficient if that disk or account is lost. Use an
already-approved private destination or explicitly configure a free backup
allocation after checking its current usage. Reserve space before uploading:
one complete archive is already several gigabytes, so unlimited daily retention
does not fit a small free object-storage allocation.

Oracle volume backups can supplement the app's consistent archives; they do not
replace a verified SQLite/source restore drill. This guide does not configure
automatic off-host backups, create a bucket, or upload research data. Keep a
private Mac copy until remote recovery has actually been tested.
