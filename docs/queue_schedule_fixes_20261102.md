# Queue deletion and one-shot night schedules

Base: 0ee0c43. Changes developed in codex/fix-v1-queue-night-schedule.

Queue delete requests are immutable operation files beside the queue snapshot.
The bot acknowledges applied/already_absent/too_late. Stable job IDs distinguish
requests sharing a URL. Legacy identities are only accepted when unambiguous.
Pending deletes are checked before claiming work and during recovery; active
jobs are retained. Removing pending work also updates asyncio queue accounting.
Operation records are retained for replay protection; no retention policy is
added in this patch.

Schedules store a concrete run_at timestamp computed once at creation. Legacy
files derive it from created_at; fired legacy schedules remain inactive and
incomplete legacy records require review. A filesystem lock serializes claims,
cancellation and replacement. Dispatch is recorded before batch admission;
202 consumes the one-shot schedule, 409 allows retry within the 30-minute
window. Unknown handoffs require review rather than automatic retry. A stale
dispatch becomes needs_review after five minutes. This favors avoiding duplicate
batches; a crash before batch acceptance can require manual rescheduling.

The dashboard displays the server's run_at date and waits for queue-delete
acknowledgement. No real render or external paid API is used for acceptance.

Validation:
- Focused Python 3.12: 62 passed.
- Focused Python 3.10: 62 passed, 13 subtests passed.
- JavaScript: 13 passed.
- Full Python 3.12 in isolated worktree: 1262 passed, 80 skipped, 11 failed.
- Clean baseline in equivalent isolated worktree: 1243 passed, 80 skipped,
  the same 11 failed test IDs. Existing failures concern script-studio token,
  Gemini fixtures/configuration, OCR sampling, missing SenseVoice/audio fixtures,
  and runtime cache expectations. No new failing test IDs were observed.
- Final expiry-notification adjustment was rechecked by the focused suites.

Runtime reload must use the scoped shutdown idle guard and restore the original
verified Scheduled Tasks. Do not use force, broad process-name kills, Git reset,
or rollback of user data. Source originals should be retained before deployment.

## Runtime deployment

### Follow-up after independent review

The independent review found three additional defects: pending deletes could
publish an empty queue before startup recovery, a corrupt operation record could
terminate the consumer, and corrupt schedules could not be cancelled/replaced.

Startup now awaits recovery before launching the watcher/heartbeat. Publication
and delete application are suspended during recovery; failed partial recovery
leaves the saved snapshot intact and propagates the error. Missing in-memory
delete targets are checked against the snapshot before acknowledgement, and an
empty in-memory queue is never published merely to acknowledge a missing target.

Malformed operation records are retained, reported once per file version in the
log, and isolated from valid commands. Corrupt operation reads through the API
return an error. Explicit schedule cancellation/replacement backs up the exact
corrupt bytes before modifying the original; backup failure preserves the source.

The three independent probes were promoted to tests/test_queue_control_followup.py,
with additional tests for actual post_init ordering, failed partial recovery,
mixed valid/invalid commands, malformed schedule schemas and backup failure.
Focused Python 3.12 plus independent probes: 75 passed. JavaScript: 13 passed.
Full Python 3.12: 1272 passed, 80 skipped, the same 11 baseline failures listed
above. No additional failing test IDs were observed.

Applied to C:\tool v1 after checking idle and an empty Telegram queue. Scoped
shutdown stopped 16 verified processes without force; the five verified V1
Scheduled Tasks were restarted. Telegram supervisor was already started by the
watchdog, so its duplicate task exited successfully (Ready); the live bot has
heartbeat=true, polling=true, busy=false and queue=0. Downloader PID 7568 and
its 17:06:46 start time were preserved.

Post-reload health: 5/5 PASS, Python 3.12.14, pip check clean, preflight 25/25,
dashboard idle/active=false, control listening. OpenAPI exposes the new queue
acknowledgement route. No real video job was submitted. Source originals and
original task names are retained in the fix worktree under
scratch/production-before-queue-schedule-fix/. No Git commit, merge or push.

Follow-up deployment completed after a fresh idle/empty-queue check. Focused
Python 3.10 including independent probes: 75 passed, 13 subtests passed.
Post-reload: runtime running, heartbeat=true, polling=true, busy=false, queue=0;
health 5/5 PASS and preflight 25/25. All eight follow-up files matched the tested
worktree. Downloader PID 7568 retained its original start time. No live render
was submitted. Backup of the previous deployed source is retained at
scratch/before-followup-1bcefbfafd8c4e26b7216c3c5458da77/ in the fix worktree.
