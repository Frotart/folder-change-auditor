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

## Observed results (26 October 2026)

| Environment | Outcome | Important qualification |
| --- | --- | --- |
| Windows, CPython 3.12.14 | 71 tests, 67 passed, 4 skipped, 0 failures/errors | Results reported in the preceding private development verification; four real symlink tests were skipped due to unavailable symlink privileges. |
| Linux, CPython 3.13.5 | 71 tests, 64 passed, 7 skipped, 0 failures/errors | Independently re-executed; skipped tests are Windows junction/locking integrations. |

The Linux synthetic end-to-end demonstration also passed and returned the
expected added/removed/modified results. The code in this candidate was not
changed from the tested implementation. The Windows results have not been
reproduced on a Windows machine during candidate packaging.

A GitHub Actions workflow is included for Python 3.12 and 3.13 on Windows and
Ubuntu. **That workflow has not run on GitHub yet.** CI results must be reviewed
before tagging a public release. macOS, network drives, and hostile/racing file
systems are not verified by these runs.

## Coverage and known limitations

Coverage includes schema validation, duplicate keys, invalid encodings and
relative paths, symlink/junction policies, concurrent changes, write errors,
exclusive snapshot publication, and CLI exit codes. Platform-specific tests are
skipped where not applicable or supported.

The scan is not atomic and is not designed for adversarial filesystems or
forensic chain-of-custody use. Reads may update access times. Skipped entries,
metadata-only changes, alternate data streams, ACLs, and empty directories are
outside content comparison. Keep JSON snapshots private when relative paths,
file sizes, and hashes may reveal information.