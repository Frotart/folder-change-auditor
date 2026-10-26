# Folder Change Auditor

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Find added, removed, and modified files by comparing two local SHA-256 inventories.
The auditor reads regular file contents and writes a new JSON snapshot outside the
source folder. It does not change source contents, delete source files, synchronize
folders, or contact a service.

**Version:** `0.1.0` (public release candidate; not yet published). Snapshot
format remains version `1`. See [testing and compatibility](TESTING.md) for
observed results and limitations.

## Start here

Use Python 3.12 or newer and the standard library. No third-party packages or
installation are required. The test suite has run on Windows and Linux (see
[TESTING.md](TESTING.md)); not every platform-specific test runs on both. Snapshot
output requires a filesystem supporting hard links (including tested NTFS and
local Linux filesystems).

Replace `python` below with the path to a working Python interpreter if the
command is not configured on your machine.

From the extracted project folder:

```powershell
python folder_auditor.py --help
python folder_auditor.py --version
python examples/demo.py
python -m unittest -v test_folder_auditor
```

The example creates only disposable synthetic files, runs the actual command-line
tool, checks its results, and removes its temporary example folder. Expected diff:

```json
{
  "added": ["added.txt"],
  "removed": ["removed.txt"],
  "modified": ["changed.txt"]
}
```

The tests also use synthetic temporary directories. On Windows they create
temporary junctions inside those directories. Real symlink tests report a skip
when the account lacks permission; they do not change Windows settings or request
additional privileges themselves.

## Take and compare snapshots

For an existing local folder named `demo-source`, run these commands from its
parent directory, with the auditor script available in the current directory:

```powershell
python folder_auditor.py snapshot .\demo-source --out .\before.json
# Make your intended changes to demo-source, then take a fresh snapshot.
python folder_auditor.py snapshot .\demo-source --out .\after.json
python folder_auditor.py compare .\before.json .\after.json
python folder_auditor.py compare .\before.json .\after.json --json
```

Use new output filenames each time. Existing files, directories, and dangling
links are never overwritten. The output parent must already exist. Keep snapshots
outside the scanned folder. Source and output directory paths must have no symlink
or Windows reparse-point components, including junctions. Use their actual paths.

Snapshot paths use `/` separators and are case-sensitive strings. Unicode and
spaces are supported; terminal output escapes non-ASCII characters for predictable
display. Snapshots reject ambiguous or unsafe relative paths, including absolute
paths, `..`, empty components, backslashes, colons, and Unicode control, format,
or surrogate characters. Encountering such a real source filename aborts the
scan rather than creating a snapshot that cannot be validated later.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Snapshot saved, or compared recorded files are unchanged. |
| `1` | Compared recorded files differ. This is an expected diff result. |
| `2` | Invalid arguments, unreadable/changing input, unsafe output, or other audit error. |

With `--json`, stdout contains only the diff JSON. Warnings and errors go to
stderr. A snapshot command can succeed with skipped entries: check its reported
count and warning before relying on its coverage.

### Skipped entries need explicit treatment

Symlinks, junctions, all other Windows reparse points, and non-regular special
files are listed under `skipped`; their targets and contents are not inventoried
when encountered normally. Reparse points include cloud placeholders. An
unreadable regular file or directory aborts the scan instead of being silently
skipped.

Comparison refuses snapshots with any skipped entries by default. If you
deliberately want a comparison of recorded regular files only:

```powershell
python folder_auditor.py compare before.json after.json --allow-skipped --json
```

This option always prints a scope warning. A zero exit code with this option
says nothing about skipped contents, skipped targets, or changes to the skipped
list. Inspect both snapshots if those matter.

## Python API

```python
from pathlib import Path
from folder_auditor import AuditError, compare_snapshots, save_snapshot, snapshot_folder

root, manifest = snapshot_folder(Path("demo-source"))
save_snapshot(root, manifest, Path("before.json"))
# Later: scan the same intended folder and save to a new external filename.
root, manifest = snapshot_folder(Path("demo-source"))
save_snapshot(root, manifest, Path("after.json"))
changes = compare_snapshots("before.json", "after.json")
```

Pass the root returned by `snapshot_folder` to `save_snapshot`; the two-function
API cannot prove that a caller supplied the correct root. `AuditError` reports
expected operational failures. `load_snapshot(path)` retains the draft API and
returns the validated `files` map. `compare_snapshots` accepts the optional
keyword `allow_skipped=True`, corresponding to the CLI flag.

## Snapshot format and validation

A format-1 snapshot has exactly `version`, `algorithm`, `files`, and `skipped`.
Each file has exactly a nonnegative integer `size` and a lowercase 64-character
SHA-256 `sha256`. The file-size upper bound is `2**63 - 1`. No source absolute
path, timestamps, file contents, or credentials are embedded automatically.

Loading rejects duplicate JSON keys, unknown fields, invalid sizes/digests,
duplicate skipped names, file/skipped overlap, and impossible file-parent
relationships. It rejects invalid UTF-8, malformed JSON, non-finite numbers,
and unsupported versions/algorithms. Names in snapshots are never used to open,
restore, delete, or execute files. A well-formed snapshot is still an unauthenticated
claim: this tool cannot establish who created it or whether its hashes are true.

Limits: 8 MiB per input or serialized output snapshot, 100,000 recorded names,
4,096 characters per relative path, and 128 path components. Walking also limits
all discovered source entries, including directories, to 100,000. Each file is
hashed in 1 MiB chunks; total source bytes and execution time are not capped.
Memory holds the inventory, validation data, and JSON. These limits are not a
general defense against resource exhaustion.

## Safety and remaining limits

- **Use a stable, trusted local directory tree.** The scanner checks identities,
  sizes, and modification times before/after reads and rechecks observed entries
  at the end. POSIX also uses change time. This detects the tested ordinary
  mutations, but it is not an atomic filesystem snapshot or a security boundary.
- **Concurrent attackers remain out of scope.** Path-based operations have gaps
  between checking and opening/using a path. On Windows the portable file opener
  has no `O_NOFOLLOW`; POSIX uses it for the final component only. An attacker
  replacing an ancestor or swapping a link in and back can evade checks or cause
  an unintended read/write. Stop writers and keep source/output directories
  controlled; use OS snapshots or a hardened OS-specific implementation for
  adversarial or evidence-preservation work.
- **Output is published only when complete.** The writer validates and serializes
  first, writes a sibling temporary file, flushes and fsyncs it, then publishes
  with an exclusive hard link. A concurrent destination creator wins without
  being overwritten. Unsupported hard-link operations fail; there is no weaker
  overwrite fallback. This is atomic naming on the tested filesystem, not a
  power-loss durability guarantee.
- **Cleanup can need inspection.** Ordinary handled write failures remove the
  temporary file. If cleanup fails or a temporary path/directory was replaced,
  the tool reports an error and refuses unsafe cleanup. A complete destination
  may already exist; an interrupted process can leave `.folder-auditor-*.tmp`
  files. Inspect those paths before retrying with a fresh output filename.
- **Coverage is content-based.** Empty directories, permissions, ACLs, ownership,
  timestamps as diff data, extended attributes, alternate data streams, and link
  target changes are not tracked. Renames appear as a removal plus an addition.
  Hard links are inventoried as separate regular-file names. Comparison reports
  a modification if the size or digest differs.
- **Metadata and authenticity matter.** Reads can update access times or trigger
  filesystem/antivirus activity. Coarse timestamps, changes restored to the same
  metadata, changes after the final checks, or tampered snapshots can defeat
  inference. Format 1 does not identify the source root: callers must compare
  snapshots of the intended scope. Case-only differences remain name changes.
- **Keep snapshots private.** Relative filenames, sizes, and hashes can reveal
  sensitive information. The program contains no network client or telemetry;
  mapped/network filesystems and sync software can have their own activity.

## If a command fails

Read stderr and confirm the exit code. For an unreadable file, close applications
holding it or correct access through your normal process; no access settings are
changed by the auditor. For a changing folder, stop writers and retry. For an
existing output, choose a new name. For hard-link support errors, select a suitable
local output volume. For a cleanup error, inspect the destination and named
temporary location first. A failed scan does not produce a new snapshot through
the normal CLI flow.

## Project files

- [folder_auditor.py](folder_auditor.py): executable and importable implementation.
- [test_folder_auditor.py](test_folder_auditor.py): original six tests plus regressions.
- [examples/demo.py](examples/demo.py): disposable synthetic CLI demonstration.
- [TESTING.md](TESTING.md): platform coverage and known test limitations.
- [LICENSE](LICENSE): MIT open-source license.
- [.github/workflows/ci.yml](.github/workflows/ci.yml): automated tests on
  supported Windows and Linux runners.

The program does not monitor folders in the background or connect to any service.
GitHub Actions only runs the synthetic test suite when enabled in a repository;
using the CLI does not start or require GitHub Actions.

## References

Implementation choices were checked against Python's documentation for
[filesystem operations and `stat`](https://docs.python.org/3.12/library/os.html),
[reparse points and junction paths](https://docs.python.org/3.12/library/pathlib.html),
and [Windows reparse attributes](https://docs.python.org/3.12/library/stat.html).
These describe APIs; the verification report records what actually ran here.