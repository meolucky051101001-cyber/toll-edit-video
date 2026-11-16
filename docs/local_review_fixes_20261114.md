# Tool V1 local review fixes — 2026-11-14

Scope: local Tool V1 only. Cloud files and existing uncommitted work are preserved.

## Changes

- OCR text repair now requires at least four CJK characters on both sides and
  similarity >= 0.80. Recognition confidence alone cannot replace unrelated ASR
  speech with visible captions. Existing confidence and partial-text guards remain.
- Restored `V1_QC_VISUAL_PIXEL_CHECK` in supported settings, defaulting to true.
  The dashboard can persist both enabled and disabled values again.
- New dual-voice decisions with multiple cues but only one detected role request
  review, even when the pitch heuristic reports high confidence. Per-cue review
  markers and a Vietnamese explanation reach the segment editor. Existing role
  assignments are retained; no speaker is invented or automatically flipped.
- Existing job voice locks remain frozen on resume. Visual-only transcripts keep
  their existing handling. The narrowly tuned rescue from the earlier sample
  video was inspected but not promoted into a general speaker classifier.

## Validation

- 58 passed across OCR correction/acquisition, QC policy, dual voice, voice cache,
  and pixel cover QC; pytest exit code 0.
- After the final review-message change: 15 dual-voice/cache tests passed,
  pytest exit code 0 (a subset, not 15 additional unique tests).
- Read-only runtime health: 5/5 passed. Python 3.12.14, pip check clean,
  preflight 25/25, dashboard idle/active=false. Control check confirms TCP
  listening; the running control service does not expose `/health`.
- Initial restricted health requests were denied by local socket permissions.
  The read-only machine-level check above succeeded.

At the end of the initial fix turn, no restart, real render, paid API call,
package install, Git commit, or push was performed. That runtime health describes the existing processes; it does
not prove they have reloaded these source changes. A normal restart when no jobs
are running is needed for already-imported Python modules to use the changes.

## Backup and limits

Pre-edit copies (including the user's uncommitted changes) and SHA-256 manifest:
`C:\tool v1\scratch\before-local-review-fixes-20261114-152322`.

The dual-voice addition is a review safeguard, not a new speaker-recognition
model. High male / low female voices can still be misclassified. Review warnings
do not pause the dubbing pipeline, and existing saved jobs are not rewritten.
Cloud review findings remain outstanding by the user's instruction.

## Subsequent handoff

The user subsequently authorized restart, commit and PR. See
[the 14 November handoff](HANDOFF_TOOLV1_TO_CLOUD_20261114.md) for the restart,
isolated PR checkout and the expanded suite's five pre-existing failures.
The initial 58-test result above is not a full release certification.
