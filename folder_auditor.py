#!/usr/bin/env python3
"""Offline folder change auditor. Source contents are never intentionally written.

Designed for stable, trusted directory hierarchies, not hostile live filesystems.
See README.md for the race, privacy, and metadata limitations.
"""

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
import unicodedata
from pathlib import Path

__version__ = "0.1.0"
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_PATH_LENGTH = 4096
MAX_PATH_DEPTH = 128
BLOCK_SIZE = 1024 * 1024


class AuditError(Exception):
    """An incomplete scan, unsafe output request, or invalid snapshot."""


def _is_link(info):
    # All Windows reparse points are excluded, including junctions and cloud
    # placeholders. This does not depend on a particular DirEntry API version.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _identity(info):
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _signature(info):
    # On Windows, ctime is creation time, and this runtime's fstat/lstat values
    # can disagree. POSIX ctime is a useful additional change signal.
    times = (info.st_size, info.st_mtime_ns)
    if os.name != "nt":
        times += (info.st_ctime_ns,)
    return _identity(info) + times


def _absolute(path):
    try:
        return Path(os.path.abspath(Path(path).expanduser()))
    except (OSError, ValueError, RuntimeError) as exc:
        raise AuditError(f"Invalid filesystem path: {ascii(str(exc))}") from exc


def _plain_directory(path):
    """Reject link/reparse components; do not silently resolve aliases."""
    for component in [*reversed(path.parents), path]:
        info = component.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise AuditError(f"Directory must not use links/reparse points: {ascii(str(component))}")
    return info


def _read_opener(path, flags):
    # O_NONBLOCK prevents a swapped-in FIFO from blocking on POSIX. The fstat
    # below still rejects non-regular objects. Windows has no O_NOFOLLOW here.
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0)
                   | getattr(os, "O_NONBLOCK", 0))


def hash_file(path):
    """Hash a regular file, rejecting observable replacement or modification."""
    digest = hashlib.sha256()
    try:
        initial = Path(path).lstat()
        if _is_link(initial) or not stat.S_ISREG(initial.st_mode):
            raise AuditError(f"Not a regular file: {ascii(str(path))}")
        count = 0
        with open(path, "rb", opener=_read_opener) as stream:
            before = os.fstat(stream.fileno())
            if _signature(initial) != _signature(before) or not stat.S_ISREG(before.st_mode):
                raise AuditError(f"File replaced before reading: {ascii(str(path))}")
            while block := stream.read(BLOCK_SIZE):
                count += len(block)
                if count > before.st_size:
                    raise AuditError(f"File grew while scanning: {ascii(str(path))}")
                digest.update(block)
            after = os.fstat(stream.fileno())
        current = Path(path).lstat()
        if (_is_link(current) or count != before.st_size
                or _signature(before) != _signature(after)
                or _signature(after) != _signature(current)):
            raise AuditError(f"File changed while scanning: {ascii(str(path))}")
    except (OSError, ValueError) as exc:
        raise AuditError(f"Cannot read {ascii(str(path))}: {ascii(str(exc))}") from exc
    return {"size": after.st_size, "sha256": digest.hexdigest()}


def _valid_name(name):
    if (not isinstance(name, str) or not name or len(name) > MAX_PATH_LENGTH
            or "\\" in name or ":" in name
            or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in name)):
        raise AuditError("Invalid relative path in snapshot")
    parts = name.split("/")
    if len(parts) > MAX_PATH_DEPTH or any(p in {"", ".", ".."} for p in parts):
        raise AuditError("Invalid relative path in snapshot")


def _validate_manifest(manifest):
    if (not isinstance(manifest, dict)
            or set(manifest) != {"version", "algorithm", "files", "skipped"}
            or type(manifest["version"]) is not int or manifest["version"] != 1
            or manifest["algorithm"] != "sha256"
            or not isinstance(manifest["files"], dict)
            or not isinstance(manifest["skipped"], list)):
        raise AuditError("Unsupported or malformed snapshot")
    files, skipped = manifest["files"], manifest["skipped"]
    if len(files) + len(skipped) > MAX_ENTRIES:
        raise AuditError("Snapshot has too many entries")
    names = set()
    for name in [*files, *skipped]:
        _valid_name(name)
        if name in names:
            raise AuditError("Duplicate or overlapping snapshot entries")
        names.add(name)
    for name in names:
        parts = name.split("/")
        if any("/".join(parts[:i]) in names for i in range(1, len(parts))):
            raise AuditError("File/skipped entry cannot also be a parent directory")
    for info in files.values():
        if (not isinstance(info, dict) or set(info) != {"size", "sha256"}
                or type(info["size"]) is not int or not 0 <= info["size"] <= 2**63 - 1
                or not isinstance(info["sha256"], str) or len(info["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in info["sha256"])):
            raise AuditError("Malformed file entry in snapshot")
    return manifest


def snapshot_folder(folder):
    """Return (absolute_root, version_1_manifest); fail on unreadable entries."""
    root = _absolute(folder)
    files, skipped, directories, observed, listings = {}, [], {}, {}, {}
    try:
        _plain_directory(root)
        stack = [(root, "", root.lstat())]
        seen_directories = set()
        entries_seen = 0
        while stack:
            directory, relative, expected = stack.pop()
            current = _plain_directory(directory)
            if _signature(expected) != _signature(current):
                raise AuditError("Directory changed before scanning")
            if _identity(current) in seen_directories:
                raise AuditError("Repeated directory identity; possible mount cycle")
            seen_directories.add(_identity(current))
            directories[directory] = current
            with os.scandir(directory) as iterator:
                entries = []
                for entry in iterator:
                    entries_seen += 1
                    if entries_seen > MAX_ENTRIES:
                        raise AuditError("Source has too many entries")
                    entries.append(entry.name)
            listings[directory] = frozenset(entries)
            for name in sorted(entries):
                rel = f"{relative}/{name}" if relative else name
                _valid_name(rel)
                path = directory / name
                info = path.lstat()  # Fresh, not cached DirEntry metadata.
                observed[path] = info
                if _is_link(info):
                    skipped.append(rel)
                elif stat.S_ISDIR(info.st_mode):
                    stack.append((path, rel, info))
                elif stat.S_ISREG(info.st_mode):
                    files[rel] = hash_file(path)
                    if _signature(info) != _signature(path.lstat()):
                        raise AuditError(f"File changed while scanning: {ascii(rel)}")
                else:
                    skipped.append(rel)
        # Detect ordinary additions/removals, changes to earlier files, and
        # directory substitutions throughout the complete walk. Not an atomic
        # filesystem snapshot: changes after these checks remain possible.
        for path, expected in {**observed, **directories}.items():
            if _signature(expected) != _signature(path.lstat()):
                raise AuditError(f"Entry changed while scanning: {ascii(str(path))}")
        # Directory timestamps are not a reliable addition/removal signal on
        # every platform (e.g. Windows with Python 3.14), so also re-list names.
        for directory, names in listings.items():
            with os.scandir(directory) as iterator:
                if frozenset(entry.name for entry in iterator) != names:
                    raise AuditError(f"Directory contents changed while scanning: "
                                     f"{ascii(str(directory))}")
        _plain_directory(root)
    except (OSError, ValueError, RuntimeError) as exc:
        raise AuditError(f"Cannot scan source: {ascii(str(exc))}") from exc
    manifest = {"version": 1, "algorithm": "sha256",
                "files": dict(sorted(files.items())), "skipped": sorted(skipped)}
    _validate_manifest(manifest)
    return root, manifest


def _encode_manifest(manifest):
    _validate_manifest(manifest)
    data = (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True,
                       allow_nan=False) + "\n").encode("ascii")
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise AuditError("Snapshot exceeds 8 MiB limit")
    return data


def _write_payload(fd, payload):
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
        return os.fstat(stream.fileno())


def save_snapshot(root, manifest, destination):
    """Publish complete JSON outside root, atomically and without overwriting.

    Uses a same-directory temporary file and an exclusive hard-link publication.
    If hard links are unsupported, fail rather than use an unsafe fallback.
    Call only with the root returned by snapshot_folder and stable directories.
    """
    payload = _encode_manifest(manifest)  # Validate before creating any file.
    source, target = _absolute(root), _absolute(destination)
    temporary = None
    temporary_identity = None
    try:
        _plain_directory(source)
        if target == source or source in target.parents:
            raise AuditError("Snapshot must be outside the scanned directory")
        parent_info = _plain_directory(target.parent)
        # lstat sees dangling links too. No resolve() that follows output links.
        if os.path.lexists(target):
            raise AuditError("Snapshot destination already exists")
        fd, name = tempfile.mkstemp(prefix=".folder-auditor-", suffix=".tmp", dir=target.parent)
        temporary = Path(name)
        temporary_identity = _identity(os.fstat(fd))
        written = _write_payload(fd, payload)
        if _identity(parent_info) != _identity(_plain_directory(target.parent)):
            raise AuditError("Output directory was replaced")
        if _signature(written) != _signature(temporary.lstat()):
            raise AuditError("Temporary output file was replaced or changed")
        _plain_directory(source)
        os.link(temporary, target)  # Atomic create-if-absent, never replace().
    except (OSError, ValueError, RuntimeError) as exc:
        raise AuditError(f"Cannot save snapshot: {ascii(str(exc))}") from exc
    finally:
        if temporary is not None:
            try:
                if (_identity(parent_info) != _identity(_plain_directory(target.parent))
                        or temporary_identity != _identity(temporary.lstat())):
                    raise AuditError("Refusing cleanup through a replaced path")
                temporary.unlink()
            except (OSError, AuditError) as exc:
                # A complete destination may already exist. Explicitly report
                # that state rather than silently hiding a residual temp file.
                raise AuditError("Temporary cleanup failed; inspect destination and "
                                 f"temporary file {ascii(str(temporary))}") from exc


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuditError("Duplicate JSON key in snapshot")
        result[key] = value
    return result


def _reject_constant(value):
    raise AuditError("Non-finite JSON number in snapshot")


def _load_manifest(path):
    try:
        initial = Path(path).lstat()
        if _is_link(initial) or not stat.S_ISREG(initial.st_mode):
            raise AuditError("Snapshot input must be a regular file, not a link/reparse point")
        if initial.st_size > MAX_SNAPSHOT_BYTES:
            raise AuditError("Snapshot exceeds 8 MiB limit")
        with open(path, "rb", opener=_read_opener) as stream:
            before = os.fstat(stream.fileno())
            data = stream.read(MAX_SNAPSHOT_BYTES + 1)
            after = os.fstat(stream.fileno())
        current = Path(path).lstat()
        if (len(data) > MAX_SNAPSHOT_BYTES or len(data) != before.st_size
                or _is_link(current) or _signature(initial) != _signature(before)
                or _signature(before) != _signature(after)
                or _signature(after) != _signature(current)):
            raise AuditError("Snapshot input changed or exceeds size limit")
        manifest = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
        return _validate_manifest(manifest)
    except (OSError, ValueError, RuntimeError) as exc:
        raise AuditError(f"Invalid snapshot: {ascii(str(exc))}") from exc


def load_snapshot(path):
    """Read and validate a manifest, returning its file map (original API)."""
    return _load_manifest(path)["files"]


def compare_snapshots(before, after, *, allow_skipped=False):
    old, new = _load_manifest(before), _load_manifest(after)
    if (old["skipped"] or new["skipped"]) and not allow_skipped:
        raise AuditError("Snapshot contains skipped entries; use --allow-skipped "
                         "only to compare the recorded regular files")
    old, new = old["files"], new["files"]
    old_names, new_names = set(old), set(new)
    return {"added": sorted(new_names - old_names),
            "removed": sorted(old_names - new_names),
            "modified": sorted(n for n in old_names & new_names if old[n] != new[n])}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audit folder contents locally using SHA-256.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    snap = commands.add_parser("snapshot", help="Read regular files and write a new external snapshot")
    snap.add_argument("folder")
    snap.add_argument("--out", required=True)
    compare = commands.add_parser("compare", help="Compare two saved snapshots offline")
    compare.add_argument("before")
    compare.add_argument("after")
    compare.add_argument("--json", action="store_true")
    compare.add_argument("--allow-skipped", action="store_true",
                         help="Explicitly compare only recorded files if either snapshot has skipped entries")
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            root, manifest = snapshot_folder(args.folder)
            save_snapshot(root, manifest, args.out)
            print(f"Recorded {len(manifest['files'])} files; skipped {len(manifest['skipped'])} entries.")
            if manifest["skipped"]:
                print("Warning: skipped entries were not hashed:", file=sys.stderr)
                for name in manifest["skipped"]:
                    print(f"  {ascii(name)}", file=sys.stderr)
            return 0
        result = compare_snapshots(args.before, args.after, allow_skipped=args.allow_skipped)
        if args.allow_skipped:
            print("Warning: comparing recorded files only; skipped entries are excluded.", file=sys.stderr)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=True))
        else:
            for category, items in result.items():
                print(f"{category.title()}: {len(items)}")
                for item in items:
                    print(f"  {ascii(item)}")
        return 1 if any(result.values()) else 0
    except AuditError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
