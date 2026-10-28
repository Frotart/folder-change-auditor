# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-28

First public release.

### Added
- `snapshot` command: records the size and SHA-256 of every regular file in a
  folder into a new JSON snapshot (format version `1`) written outside the source.
- `compare` command: reports added, removed and modified files between two
  snapshots, as text or `--json`, with exit codes `0` (same), `1` (differs) and
  `2` (error).
- Explicit handling of symlinks, junctions, reparse points and special files,
  which are listed as `skipped`; comparison refuses them unless `--allow-skipped`
  is passed.
- Strict snapshot validation: duplicate keys, unknown fields, unsafe relative
  paths, invalid digests and size limits are rejected.
- Atomic, no-overwrite snapshot publication through an exclusive hard link.
- Python API: `snapshot_folder`, `save_snapshot`, `load_snapshot`,
  `compare_snapshots` and `AuditError`.
- `folder-audit` console command via `pyproject.toml`.
- Synthetic end-to-end demo and a 72-test suite.
- CI on Windows, Linux and macOS with Python 3.12–3.14, plus a package install
  check; a release workflow that publishes `SHA256SUMS.txt` with each release.

### Fixed
- Files added to a folder during a scan are now detected on every platform. The
  scan re-lists each directory at the end instead of relying only on directory
  timestamps, which did not change reliably on Windows with Python 3.14.
- Tests and the demo use real (symlink-free) temporary paths, so they pass on
  macOS, where `/var` is a symlink to `/private/var`.

[Unreleased]: https://github.com/Frotart/folder-change-auditor/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Frotart/folder-change-auditor/releases/tag/v0.1.0
