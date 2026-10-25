# Changelog

## Unreleased

- Retry transient macOS process-group `EPERM` during timeout/cancellation cleanup
  while the child is being reaped. Persistent permission errors still surface.

- M4: opt-in automatic context compaction with an explicit estimated budget and
  percentage threshold. Manual and automatic compaction share cancellation,
  timeout, summary validation, and history-preserving behavior.

- M3: explicit session-only patch-directory and exact-command grants, inspection,
  and revocation through `/permissions`; per-operation review stays the default.
- Approval history distinguishes session grants from explicit reviews. Existing v1
  sessions still load; active grants are never restored from saved history.

- M2: `git.managed = false` leaves reviewed edits uncommitted, preserves staging,
  and skips Git initialization, checkpoints, and commit offers; lint stays separate.
- `/undo` now defaults to a reversing commit. `/undo local` explicitly rewrites an
  eligible tip. Undo no longer fetches or inspects remote publication status.

- M1: refreshing tool activity with elapsed time, separate model/approval waiting,
  concise completion statuses, and Ctrl-C cleanup across commands and diff reviews.
- Preserve streamed replies across activity changes and retain successful lint
  output for `/output` alongside failed lint and command output.

## 1.0.0 — 2026-10-21

First local v1 release of Skua.

- Terminal chat with streaming Markdown, a single-row input, path completion,
  cancellation, and background-highlighted diff reviews.
- Optional in-place startup animation with transparent black backgrounds.
- Explicit session saves, resume, Markdown export, context estimates, and manual
  compaction that preserves full history.
- Reviewed commands and patches, lint workflows, generated commit messages,
  checkpoints, and Git undo. Command changes require one final commit approval.
- Pinned dependencies, fresh-install checks, end-to-end workflows, and a configured
  GitHub Actions matrix.

Validation evidence and remaining platform limitations are in [VALIDATION.md](VALIDATION.md).
Remote CI and live-provider acceptance were not run for this release checkpoint.
