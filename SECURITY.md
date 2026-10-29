# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |

## Scope and threat model

Folder Change Auditor is designed for **stable, trusted local directory trees**.
It is not a forensic tool and does not defend against a concurrent attacker who
can modify the scanned or output directories while it runs. The README's
"Safety and remaining limits" section describes the known gaps, including:

- path check-then-use races (no atomic filesystem snapshot);
- snapshots are unauthenticated claims, so a tampered snapshot is not detected;
- metadata-only changes, ACLs, alternate data streams and empty directories are
  not compared.

Reports about those documented limits are welcome as improvements, but they are
not treated as vulnerabilities.

**In scope:** anything that makes the tool write to, delete or execute anything
inside the scanned folder; overwrite an existing file; follow a link it claims to
skip under normal (non-racing) conditions; accept a snapshot that its own
validation rules say it should reject; or leak data over the network.

## Reporting a vulnerability

Please use GitHub's **private vulnerability reporting** on this repository
(Security tab → "Report a vulnerability"). Do not open a public issue for a
suspected vulnerability.

Include the version (`folder-audit --version`), operating system, Python version,
and the smallest synthetic steps that reproduce the problem. Please don't attach
snapshots of real folders: file names, sizes and hashes can reveal private
information.

You can expect an acknowledgement within 7 days. Fixes will be released as a
patch version and noted in [CHANGELOG.md](CHANGELOG.md).
