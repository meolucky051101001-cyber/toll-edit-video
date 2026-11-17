# Dashboard stability: restart and PR record — 2026-11-17

This record supersedes the pre-commit and pre-restart status in the historical implementation and controls reports. Their older test totals remain historical evidence and do not certify this merged revision.

## Source and scope

- Branch: `codex/dashboard-control-responsiveness-20261117`.
- Implementation commit: `bba9286` (`fix(v1): make dashboard controls respond immediately`).
- Tested code commit: `fcefc13039b70338110c65455946e2e6e4fda19b`, merging current `origin/main` at `b348174` and resolving 26 conflicted files.
- This branch includes the cumulative dashboard remediation: durable batch admission/recovery, execution ownership and fencing, tracker/queue/Telegram consistency, guarded Cloud publication/cancellation, worker/GPU foundation, and endurance tooling, as well as immediate dashboard settings and stop acknowledgements.

Model/mode/ASR selections update immediately; new batches wait for confirmed persistence. Existing jobs retain their model snapshot. Stop intent is persisted before acknowledgement, and cleanup drains in the background. Native work finishes at its cancellation checkpoints; the UI only reports completion after cleanup is confirmed.

The merge preserves main's OCR boundary/CJK evidence and persistent Cloud CU ledger alongside the remediation ownership/publication safeguards. Studio manual execution now uses exclusive batch admission and an execution context held through native cleanup. Regression fixtures use the current durable stop/ACK contract.

## Verification of the merged code

| Suite | Result |
| --- | --- |
| Integrated selection from `tools/run_dashboard_acceptance.py` | 478 passed, 7 skipped, 0 failures/errors |
| Python quality CI selection plus controls and Telegram dispatch regressions | 408 passed, 0 skipped/failures/errors |
| Focused merged Cloud/control/Studio regressions | 124 passed |
| All `tests/js/*.test.js` plus `tests/test_dashboard_progress.cjs` | 42 passed |
| Python AST and staged whitespace checks | Passed |

These suites overlap; their totals must not be added together. The seven integrated skips are existing quarantines for old tracker, checkpoint, stage-log, and GPU-lock expectations. They do not certify those quarantined contracts. Deprecation warnings from Starlette/httpx and pydub/audioop did not fail the suites.

Local ignored evidence: `scratch/pr-integrated-final.log`, `scratch/pr-ci-final.log`, `scratch/pr-merged-core.log`, and `test_evidence/dashboard/pr-restart-20261117/{integrated-results,ci-results}.xml`.

Tests used the existing project Python environment and temporary test dependency path; no production dependencies or secrets were installed or committed. Runtime dependency/model junctions were created after testing and are ignored. A documentation-only follow-up records this result without changing the tested runtime code.

## Service restart and observed health

The user authorized restart, commit, and PR creation. The scoped shutdown verified no active work before stopping 10 owned service processes, with no surviving selected process. The independent downloader and scheduled-task actions were preserved.

The control service then started the dashboard and Telegram supervisors from `C:/Users/admin/.codex/worktrees/dashboard-stability/tool v1`. The dirty checkout at `C:/tool v1` was not overwritten. Existing Python/model environments and configuration were reused; the environment file was read into process memory, not copied into this branch.

Existing data locations remain `D:/workspace`, inputs `D:/workspace/downloads`, and outputs `D:/banve`.

Post-restart checks:

- `127.0.0.1:8088/health/live`: HTTP 200, alive, dashboard PID 34992.
- `127.0.0.1:8088/health/ready`: HTTP 200, ready, no readiness reasons, live queue owner.
- `127.0.0.1:8090/status`: HTTP 200; dashboard, bot, heartbeat, and polling all true.
- Verified dashboard process working directory: the requested worktree's `backend` folder.
- Initial checks observed an idle worker and empty queue. A subsequent check observed one active worker and three queued items, with readiness still healthy; no further restart was performed.

Ignored restart evidence is in `scratch/service-restart-20261117.json` and `scratch/service-restart-console.log`. An initial helper polled the nonexistent `/api/heartbeat`; health was verified separately against the actual endpoints above. Those 404s were not treated as service failures or successful health checks.

## Open acceptance gates

- Production support remains `MAX_SUPPORTED_WORKERS=1`. Registry tests with two slots do not certify two production workers.
- Cloud live remains gated pending secret/log/artifact transport and scoped cancellation acceptance, content/QC checks, and a budgeted provider canary. No paid provider was called for this verification.
- The 24-hour soak is not complete. The historical smoke failed with a 9.344-second poll gap; subsequent checkpoint/timing regressions do not prove a successful real-time smoke or stress run. No new 24-hour workload was started for this restart/PR request.
- This verification does not certify native GPU/AI rendering end to end, translation/TTS quality, or a Telegram message canary. Bot/polling readiness is a service-health observation.

## PR synchronization after main advanced

PR #31 was created after main advanced to `57cf100` (OCR/QC performance and progress changes). The PR was synchronized in a separate managed checkout at `C:/Users/admin/.codex/worktrees/dashboard-pr-sync/tool v1`, because the restarted dashboard-stability checkout was already processing live work.

Three conflicts were resolved by retaining both sides' required behavior: tracker ownership fencing plus the new QC progress arguments; durable controller stop helpers plus boot-pause intent names; and native thread draining plus QC cancellation propagation. Test fixtures now explicitly provide nonvisual resume mode, the controller operation lock/state, and an isolated Studio workspace. Production ownership, pause, stop, and publication guards remain intact.

Final verification on this synchronized PR source:

| Suite | Result |
| --- | --- |
| Union of dashboard acceptance, current Python CI selections, controls/Telegram dispatch and changed OCR regressions (75 selections) | **1,007 passed, 11 skipped, 0 failures/errors**, 222.18 seconds |
| Focused fixture/ownership regressions | **58 passed** |
| All JavaScript suites including new QC progress and dashboard progress | **49 passed**, 0 skipped/failures |
| Python syntax inventory | **488 files parsed** |

The suites overlap. The earlier 478/408/42 totals above remain evidence for the deployed `fcefc13` code; they are not added to these final PR totals. The first synchronized run and its five fixture failures are preserved separately.

The 11 final skips are declared quarantines: tracker lifecycle/stop (3), ASR checkpoint (1), resource/stage logs (2), separator GPU locking (1), voice/OCR cache (3), and load-sensitive early subtitle-band frame count (1). No immediate-control or boot-pause regression is skipped. ASR reuse after changed audio and all persistent voice-cache corruption/write-failure/invalid-duration behavior are not fully certified by substitute coverage.

Final ignored evidence: `test_evidence/dashboard/pr-sync-final-20261117/{results.xml,run.json,source-hashes.json,pytest.log}`, `scratch/pr-sync-acceptance-final.log`, and `scratch/pr-sync-javascript.log`. The final runner confirms source was unchanged during the run. The failed first run remains under `pr-sync-20261117`; the focused run is under `pr-sync-focused-20261117`.

The running service remains at deployed runtime commit `fcefc13`; PR synchronization did not replace files or restart the busy service. A final read-only check still returned HTTP 200/readiness true, with dashboard, bot, heartbeat, and polling healthy, one active worker, and three queued items. The PR source includes newer main OCR/QC work that has not been redeployed into that active session. The open acceptance gates above remain unchanged.
