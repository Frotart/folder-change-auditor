# Testing and compatibility

The tool uses Python's standard library and has no required third-party runtime
dependencies. The suite creates **synthetic, temporary filesystem fixtures**.
It must not be run against private or live source directories.

## Run locally

```shell
python -m unittest -v test_folder_auditor
python examples/demo.py
```

The comparison command returns exit code `1` when changes exist, by design;
`examples/demo.py` verifies this rather than treating it as a failure.

## Continuous integration

Every push to `main` and every pull request runs
[the Tests workflow](https://github.com/Frotart/folder-change-auditor/actions/workflows/ci.yml):
the unit and integration suite plus the end-to-end demo on Windows, Ubuntu and
macOS with Python 3.12, 3.13 and 3.14, and a separate job that builds the
package, installs the wheel and runs the `folder-audit` command.

## Observed results

| Date | Environment | Outcome |
| --- | --- | --- |
| 26 Oct 2026 | GitHub Actions, Ubuntu, CPython 3.12.15 and 3.13.16 | 71 tests, 64 passed, 7 skipped; demo passed |
| 26 Oct 2026 | GitHub Actions, Windows, CPython 3.12.10 and 3.13.16 | 71 tests, all passed; demo passed |
| 26 Oct 2026 | Linux, CPython 3.13.5 (local) | 71 tests, 64 passed, 7 skipped; demo passed |
| 26 Oct 2026 | Windows, CPython 3.12.14 (local) | 71 tests, 67 passed, 4 skipped (real symlink tests; no symlink privilege) |
| 28 Oct 2026 | Linux, CPython 3.13.18 (local) | 71 tests, 64 passed, 7 skipped; demo and installed `folder-audit` command passed |

On Linux and macOS the 7 skips are Windows-only junction and file-locking
integration tests. On GitHub's Windows runners the symlink tests run because the
runner account has symlink privilege, so nothing is skipped there.

macOS and Python 3.14 were added to the matrix for the 0.1.0 release; their
results are in the Actions history for that release.

## Coverage and known limitations

Coverage includes schema validation, duplicate keys, invalid encodings and
relative paths, symlink/junction policies, concurrent changes, write errors,
exclusive snapshot publication, and CLI exit codes. Platform-specific tests are
skipped where not applicable or supported.

Network drives and hostile/racing file systems are not verified. The scan is not
atomic and is not designed for adversarial filesystems or forensic
chain-of-custody use. Reads may update access times. Skipped entries,
metadata-only changes, alternate data streams, ACLs, and empty directories are
outside content comparison. Keep JSON snapshots private when relative paths,
file sizes, and hashes may reveal information.
