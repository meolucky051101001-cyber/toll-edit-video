# Cloud remediation contract

F2/F3/F4 are covered by offline acceptance in `tests/test_cloud_remediation.py`.
This does not enable production cloud processing. `ensure_live_ready()` rejects
every live attempt before reading credentials or provisioning a session. No
environment flag bypasses this gate. `install_secret` deliberately has no
unverified transport fallback.

Before enabling a live provider, verify the exact installed CLI version/source:
`exec -f` notebook/history behavior, exit status and timeout semantics; scoped
stop acknowledgement; upload destinations and secret file permissions. Prove
with sentinel credentials that argv, generated source, logs, provider errors,
local/WSL/remote temporary files and artifacts do not retain secret values.
Only then implement and accept a dedicated secret transport. Do not infer
support for `--env`, stdin or remote cancellation from a wrapper's API.

## Entry points and ownership

Telegram enqueues with a persistent `external_job_id` and unique `claim_owner`.
Enqueue and initial claim are atomic; replay returns the same record. A queued
Telegram record is reserved for its explicit caller and is not consumed by the
standalone worker. Both paths use the same SQLite claim primitive. An attempt
can start only once. Progress requires attempt/owner and accepts an expected
revision; terminal progress is rejected.

`process_job` returns `CloudRunResult`, not a success boolean. Truthiness means
`completed` with a verified receipt. `get_receipt(job_id)` additionally requires
confirmed session/secret cleanup, exact job/attempt/source identity, matching
sidecar and unchanged output bytes. Caller must finalize its own tracker before
notifying success. Existing attempts with uncertain ownership are held for
reconciliation rather than submitted to the provider again.

## Cancellation and publication

Cancel intent is committed before any provider stop call. Pending provisioning
is drained before stopping its possibly newly allocated session. Other long
operations poll cancellation, request scoped stop, and drain before reporting
terminal state. Failed cleanup remains `stop_incomplete`; no new claim may use
the slot while cleanup is unconfirmed. No account-wide assignment cleanup runs.

Transfers use fresh `.part` files and require rc=0 and nonempty regular files.
Verification checks manifest identity/size/SHA-256, positive finite duration,
video dimensions and audio/video streams. A remote manifest also checks source
file hash and output duration against input. These checks establish integrity,
not translation or TTS content quality.

The existing output name `cloud_<video_name>` is retained. A different or
unverifiable owner yields `output_conflict`, even if its bytes happen to match.
Verified files are copied/fsynced to staging on the output volume. Publication
uses an atomic **no-clobber hardlink installation** (NTFS supported), instead of
replace/overwrite rename. Unsupported filesystems fail closed. Keep the staging
link as ownership proof for recovery; deleting it before receipt commit breaks
automatic reconciliation.

A durable SQLite publication intent is the cancellation decision point. Before
it, cancel wins and no output is installed. After it, cancel reports publication
in progress/completed, not successful cancellation. Output/sidecar/DB are not a
single filesystem transaction: recovery handles crashes after intent, output
installation and receipt installation. It verifies identity, hash and the
staging inode before completion. Conflicts remain `recovery_required`.

## Migration and rollback

Version 2 adds fields without deleting existing history. Before changing a
legacy database, the store creates a consistent SQLite backup (including WAL)
named `<database>.pre-v2-<unique>.sqlite`. Legacy active rows without attempt
ownership become `recovery_required`; completed legacy rows without receipts
are not accepted as verified success. A newer schema is refused, not downgraded.
Active attempts are never reclaimed merely because they are old.

Drain/disable admission before deployment or rollback. Retain backups, receipts
and staging ownership evidence. Roll back application code without dropping
added columns; keep cloud admission disabled if the old code cannot enforce
fencing. Do not run an old worker against active version 2 attempts. No rollback
step deletes or overwrites existing outputs.

Remaining production gates: CLI/secret transport acceptance, provider cleanup
verification, existing full CU accounting and translation/TTS QC roadmap, and
real provider canary. No paid provider or production session was used for the
offline acceptance suite.
