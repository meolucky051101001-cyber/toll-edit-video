# Phase 4 acceptance — 2026-09-15

Scope: Tool V2 white cover dimensions and rejection of packaging/static scene text.

## Changes

- Retained the hard visible cover width cap of floor(0.90 * canvas width).
- Centered cover and text exactly, including odd normalized canvas widths; minimum 5% horizontal margins remain intact and all four corners remain rounded.
- Rejected OCR boxes wider than 90%, including composite candidates through the same geometry validation.
- Honored explicit packaging/static/non-subtitle/out-of-band/logo/watermark labels before candidate mapping and before ASS generation, including cached or imported geometry.
- Prevented repeated unmatched scene text from entering via bracketed subtitle recovery. ASR-matched dialogue at a fixed position remains eligible.
- Bumped pipeline implementation to 2.9.0 so older OCR/subtitle artifacts are invalidated.

## Verification

- Targeted cover/locator/layout/tracking suite: 42 tests passed.
- Full unittest discovery: 287 tests passed in 66.725 seconds, exit 0.
- New tests cover portrait, landscape, square, 4:3, odd dimensions, 4K inputs, both margins, exact centering, rounded corners, explicit classification, stationary dialogue, repeated unmatched text and source geometry that cannot fit under the width cap.
- Existing geometry tests now enforce 90% instead of 99%, with actual zero-outline coverage.
- Tool V1 baseline: commit c822af952d4fdd8ddbe72ffb27f8863a87a0b6b6 and all 267 Git blobs match the baseline. This verifies committed objects, not every working-tree file.

## Limits

Validation uses automated local tests and mocked integrations; no live bot job or real-video benchmark was run for Phase 4. OCR classification remains heuristic. A source rectangle extending outside the capped card is still a cover-QC failure; clamping does not make it fully covered. Unrecognized/rejected OCR requires the existing unverified-subtitle handling and is not proof that source text is absent.

Tool V1 was not edited or restarted. Telegram Bot V2 remains off. Release hashes and a source backup are produced by scripts/generate_release_manifest.py.
