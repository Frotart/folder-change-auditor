import builtins
import contextlib
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import folder_auditor as auditor

from folder_auditor import (
    AuditError,
    compare_snapshots,
    save_snapshot,
    snapshot_folder,
)


class FolderAuditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()

    def make_snapshot(self, name):
        root, manifest = snapshot_folder(self.source)
        destination = self.base / name
        save_snapshot(root, manifest, destination)
        return destination

    def test_unchanged_files(self):
        (self.source / "file.txt").write_text("hello")
        before = self.make_snapshot("before.json")
        after = self.make_snapshot("after.json")

        self.assertEqual(
            compare_snapshots(before, after),
            {"added": [], "removed": [], "modified": []},
        )

    def test_added_removed_and_modified(self):
        (self.source / "changed.txt").write_text("old")
        (self.source / "removed.txt").write_text("delete")
        before = self.make_snapshot("before.json")

        (self.source / "changed.txt").write_text("new")
        (self.source / "removed.txt").unlink()
        (self.source / "added.txt").write_text("added")
        after = self.make_snapshot("after.json")

        self.assertEqual(
            compare_snapshots(before, after),
            {
                "added": ["added.txt"],
                "removed": ["removed.txt"],
                "modified": ["changed.txt"],
            },
        )

    def test_nested_files(self):
        nested = self.source / "docs"
        nested.mkdir()
        (nested / "readme.txt").write_text("example")
        _, snapshot = snapshot_folder(self.source)
        self.assertIn("docs/readme.txt", snapshot["files"])

    def test_reject_snapshot_inside_source(self):
        root, snapshot = snapshot_folder(self.source)
        with self.assertRaises(AuditError):
            save_snapshot(root, snapshot, self.source / "snapshot.json")

    def test_no_overwrite(self):
        first = self.make_snapshot("snapshot.json")
        root, manifest = snapshot_folder(self.source)
        with self.assertRaises(AuditError):
            save_snapshot(root, manifest, first)

    def test_symlink_exclusion(self):
        target = self.base / "external.txt"
        target.write_text("outside")
        link = self.source / "external-link.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable")

        _, snapshot = snapshot_folder(self.source)
        self.assertIn("external-link.txt", snapshot["skipped"])
        self.assertNotIn("external-link.txt", snapshot["files"])


class FixtureHelpers:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()

    def manifest(self):
        return {"version": 1, "algorithm": "sha256", "files": {
            "file.txt": {"size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
        }, "skipped": []}

    def write_manifest(self, value, name="input.json"):
        path = self.base / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def link(self, path, target, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Real symlink creation unavailable: {exc}")

    def junction(self, path, target):
        if os.name != "nt":
            self.skipTest("Windows junction integration test")
        # Fixed command words; paths come only from this synthetic TemporaryDirectory.
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(path), str(target)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode:
            self.skipTest(f"Real junction creation unavailable: {result.stderr.strip()}")
        self.addCleanup(lambda: path.rmdir() if path.exists() else None)


class EdgeCaseTests(FixtureHelpers, unittest.TestCase):
    def test_untrusted_relative_names(self):
        for name in ["", "/absolute", "../escape", "a/../b", "a/./b", "a//b",
                     "a/", "C:/drive", "C:relative", "a\\b", "//server/share",
                     "nul\x00name", "line\nname", "\x1b[31m", "bad\ud800"]:
            with self.subTest(name=repr(name)):
                data = self.manifest()
                data["files"] = {name: next(iter(data["files"].values()))}
                with self.assertRaises(AuditError):
                    auditor.load_snapshot(self.write_manifest(data))

    def test_skipped_schema_and_paths(self):
        for value in [[None], [{}], ["../escape"], ["a", "a"], ["file.txt"],
                      ["dir", "dir/child"], [True]]:
            with self.subTest(value=value):
                data = self.manifest()
                data["skipped"] = value
                with self.assertRaises(AuditError):
                    auditor.load_snapshot(self.write_manifest(data))

    def test_boolean_version_rejected(self):
        data = self.manifest()
        data["version"] = True
        with self.assertRaises(AuditError):
            auditor.load_snapshot(self.write_manifest(data))

    def test_duplicate_json_keys_rejected(self):
        path = self.base / "duplicate.json"
        record = json.dumps(next(iter(self.manifest()["files"].values())))
        for text in [
            '{"version":9,"version":1,"algorithm":"sha256","files":{},"skipped":[]}',
            '{"version":1,"algorithm":"sha256","files":{"a":' + record +
            ',"a":' + record + '},"skipped":[]}',
        ]:
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(AuditError):
                    auditor.load_snapshot(path)

    def test_deeply_nested_json_reports_audit_error(self):
        path = self.base / "deep.json"
        path.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
        with self.assertRaises(AuditError):
            auditor.load_snapshot(path)

    def test_oversized_input_rejected(self):
        path = self.base / "large.json"
        path.write_bytes(b" " * (8 * 1024 * 1024) + json.dumps(self.manifest()).encode())
        with self.assertRaises(AuditError):
            auditor.load_snapshot(path)

    def test_invalid_utf8_reports_audit_error(self):
        path = self.base / "encoding.json"
        path.write_bytes(b"\xff")
        with self.assertRaises(AuditError):
            auditor.load_snapshot(path)

    def test_file_cannot_also_be_parent(self):
        data = self.manifest()
        data["files"]["file.txt/child"] = data["files"]["file.txt"].copy()
        with self.assertRaises(AuditError):
            auditor.load_snapshot(self.write_manifest(data))

    def test_unknown_fields_rejected(self):
        data = self.manifest()
        data["unexpected"] = "value"
        with self.assertRaises(AuditError):
            auditor.load_snapshot(self.write_manifest(data))

    def test_modified_compares_size_as_well_as_digest(self):
        before = self.write_manifest(self.manifest(), "before.json")
        data = self.manifest()
        data["files"]["file.txt"]["size"] = 2
        after = self.write_manifest(data, "after.json")
        self.assertEqual(compare_snapshots(before, after)["modified"], ["file.txt"])

    def test_comparison_with_skipped_entries_requires_opt_in(self):
        data = self.manifest()
        data["skipped"] = ["link"]
        path = self.write_manifest(data)
        with self.assertRaises(AuditError):
            compare_snapshots(path, path)

    def test_invalid_manifest_not_published(self):
        data = self.manifest()
        data["files"]["file.txt"]["sha256"] = object()
        out = self.base / "out.json"
        with self.assertRaises(AuditError):
            save_snapshot(self.source, data, out)
        self.assertFalse(out.exists())

    def test_dangling_output_symlink_rejected(self):
        target = self.base / "must-not-be-created.json"
        link = self.base / "out.json"
        self.link(link, target)
        root, data = snapshot_folder(self.source)
        with self.assertRaises(AuditError):
            save_snapshot(root, data, link)
        self.assertFalse(target.exists())

    def test_output_through_junction_rejected(self):
        parent = self.base / "real"
        parent.mkdir()
        link = self.base / "alias"
        self.junction(link, parent)
        root, data = snapshot_folder(self.source)
        with self.assertRaises(AuditError):
            save_snapshot(root, data, link / "out.json")
        self.assertFalse((parent / "out.json").exists())

    def test_directory_junction_is_skipped(self):
        external = self.base / "external"
        external.mkdir()
        (external / "outside.txt").write_text("outside")
        link = self.source / "junction"
        self.junction(link, external)
        _, data = snapshot_folder(self.source)
        self.assertEqual(data["skipped"], ["junction"])
        self.assertEqual(data["files"], {})

    def test_junction_cycle_does_not_recurse(self):
        self.junction(self.source / "cycle", self.source)
        _, data = snapshot_folder(self.source)
        self.assertEqual(data["skipped"], ["cycle"])

    def test_detect_file_replacement_during_read(self):
        path = self.source / "data.txt"
        path.write_bytes(b"old")
        original = builtins.open

        @contextlib.contextmanager
        def replace_on_close(*args, **kwargs):
            with original(*args, **kwargs) as stream:
                yield stream
            replacement = path.with_suffix(".new")
            replacement.write_bytes(b"new")
            os.replace(replacement, path)

        with mock.patch("builtins.open", side_effect=replace_on_close):
            with self.assertRaises(AuditError):
                auditor.hash_file(path)


class RobustnessTests(FixtureHelpers, unittest.TestCase):
    def test_empty_and_binary_files(self):
        (self.source / "empty").touch()
        content = bytes(range(256)) * 9000
        (self.source / "binary").write_bytes(content)
        _, data = snapshot_folder(self.source)
        self.assertEqual(data["files"]["empty"]["size"], 0)
        self.assertEqual(data["files"]["empty"]["sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(data["files"]["binary"], {
            "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})

    def test_unicode_and_deterministic_serialization(self):
        for name in ["z.txt", "\u65e5\u672c\u8a9e.txt", "a.txt", "space name.txt"]:
            (self.source / name).write_text(name, encoding="utf-8")
        root, data = snapshot_folder(self.source)
        first, second = self.base / "one.json", self.base / "two.json"
        save_snapshot(root, data, first)
        save_snapshot(root, snapshot_folder(self.source)[1], second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(list(data["files"]), sorted(data["files"]))

    def test_rewritten_stable_file_is_not_a_false_positive(self):
        path = self.source / "rewrite"
        path.write_bytes(b"original")
        path.write_bytes(b"replacement")
        _, data = snapshot_folder(self.source)
        self.assertEqual(data["files"]["rewrite"]["sha256"], hashlib.sha256(b"replacement").hexdigest())

    def test_missing_source_and_file_source(self):
        file = self.base / "file"
        file.touch()
        for source in [self.base / "missing", file]:
            with self.subTest(source=source):
                with self.assertRaises(AuditError):
                    snapshot_folder(source)

    def test_unreadable_directory_injection(self):
        nested = self.source / "denied"
        nested.mkdir()
        original = auditor.os.scandir

        def denied(path):
            if Path(path) == nested:
                raise PermissionError("synthetic unreadable directory")
            return original(path)

        with mock.patch.object(auditor.os, "scandir", side_effect=denied):
            with self.assertRaises(AuditError):
                snapshot_folder(self.source)

    def test_unreadable_file_injection(self):
        (self.source / "denied").write_bytes(b"private fixture")
        with mock.patch("builtins.open", side_effect=PermissionError("synthetic read denial")):
            with self.assertRaises(AuditError):
                snapshot_folder(self.source)

    @unittest.skipUnless(os.name == "nt", "Windows sharing-lock integration")
    def test_real_unreadable_file_sharing_lock(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        path = self.source / "locked"
        path.write_bytes(b"synthetic locked data")
        handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            self.fail(f"Could not create synthetic sharing lock: {ctypes.get_last_error()}")
        try:
            with self.assertRaises(AuditError):
                snapshot_folder(self.source)
        finally:
            kernel.CloseHandle(handle)

    def test_entry_stat_denial_injection(self):
        path = self.source / "denied"
        path.touch()
        original = Path.lstat

        def denied(p, *args, **kwargs):
            if p == path:
                raise PermissionError("synthetic stat denial")
            return original(p, *args, **kwargs)

        with mock.patch.object(Path, "lstat", new=denied):
            with self.assertRaises(AuditError):
                snapshot_folder(self.source)

    def test_file_disappears_before_read(self):
        path = self.source / "gone"
        path.touch()
        original = auditor.hash_file

        def disappear(p):
            Path(p).unlink()
            return original(p)

        with mock.patch.object(auditor, "hash_file", side_effect=disappear):
            with self.assertRaises(AuditError):
                snapshot_folder(self.source)

    def test_same_size_mutation_while_hashing(self):
        path = self.source / "data"
        path.write_bytes(b"old")
        original = auditor.os.fstat
        called = False

        def mutate(fd):
            nonlocal called
            value = original(fd)
            if not called:
                called = True
                path.write_bytes(b"new")
                os.utime(path, ns=(value.st_atime_ns, value.st_mtime_ns + 2_000_000_000))
            return value

        with mock.patch.object(auditor.os, "fstat", side_effect=mutate):
            with self.assertRaises(AuditError):
                auditor.hash_file(path)

    def test_growing_file_is_bounded(self):
        path = self.source / "growing"
        path.write_bytes(b"x")
        original = auditor.os.fstat
        called = False

        def grow(fd):
            nonlocal called
            value = original(fd)
            if not called:
                called = True
                path.write_bytes(b"longer")
            return value

        with mock.patch.object(auditor.os, "fstat", side_effect=grow):
            with self.assertRaises(AuditError):
                auditor.hash_file(path)

    def test_real_concurrent_writer_detected(self):
        path = self.source / "concurrent"
        path.write_bytes(b"before")
        start, done = threading.Event(), threading.Event()
        errors = []

        def writer():
            try:
                if not start.wait(5):
                    raise RuntimeError("Reader did not reach the synchronization point")
                path.write_bytes(b"after with different length")
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        worker = threading.Thread(target=writer, daemon=True)
        worker.start()
        original = auditor.os.fstat
        first = True

        def synchronize(fd):
            nonlocal first
            info = original(fd)
            if first:
                first = False
                start.set()
                if not done.wait(5):
                    raise RuntimeError("Synthetic writer did not finish")
            return info

        try:
            with mock.patch.object(auditor.os, "fstat", side_effect=synchronize):
                with self.assertRaises(AuditError):
                    auditor.hash_file(path)
        finally:
            start.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_addition_during_scan_detected(self):
        (self.source / "first").touch()
        original = auditor.hash_file

        def add(p):
            result = original(p)
            (self.source / "added-late").touch()
            return result

        with mock.patch.object(auditor, "hash_file", side_effect=add):
            with self.assertRaises(AuditError):
                snapshot_folder(self.source)

    def test_earlier_file_changed_while_later_file_scans(self):
        first, last = self.source / "a", self.source / "z"
        first.write_bytes(b"old")
        last.touch()
        original = auditor.hash_file

        def change_earlier(p):
            result = original(p)
            if Path(p) == last:
                first.write_bytes(b"changed")
            return result

        with mock.patch.object(auditor, "hash_file", side_effect=change_earlier):
            with self.assertRaises(AuditError):
                snapshot_folder(self.source)

    def test_real_directory_symlink_and_cycle(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"synthetic outside")
        self.link(self.source / "alias", outside, True)
        self.link(self.source / "cycle", self.source, True)
        _, data = snapshot_folder(self.source)
        self.assertEqual(data["files"], {})
        self.assertEqual(data["skipped"], ["alias", "cycle"])

    def test_real_dangling_source_symlink(self):
        self.link(self.source / "dangling", self.base / "absent")
        _, data = snapshot_folder(self.source)
        self.assertEqual(data["skipped"], ["dangling"])

    def test_symlink_policy_injected_without_privileges(self):
        path = self.source / "link"
        path.touch()
        actual = path.lstat()
        fake = mock.Mock(st_mode=stat.S_IFLNK | 0o777, st_dev=actual.st_dev,
                         st_ino=actual.st_ino, st_size=actual.st_size,
                         st_mtime_ns=actual.st_mtime_ns, st_ctime_ns=actual.st_ctime_ns,
                         st_file_attributes=0)
        original = Path.lstat

        def link_info(p, *args, **kwargs):
            return fake if p == path else original(p, *args, **kwargs)

        with mock.patch.object(Path, "lstat", new=link_info):
            with mock.patch.object(auditor, "hash_file") as hashed:
                _, data = snapshot_folder(self.source)
        hashed.assert_not_called()
        self.assertEqual(data["skipped"], ["link"])

    def test_root_junction_rejected(self):
        link = self.base / "root-alias"
        self.junction(link, self.source)
        with self.assertRaises(AuditError):
            snapshot_folder(link)

    def test_directory_replaced_by_junction_before_descent(self):
        pending = self.source / "pending"
        pending.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"synthetic outside")
        marker = self.source / "z-trigger"
        marker.touch()
        original = auditor.hash_file
        # Create a real junction during the controlled scan, not on a timer.
        def substitute(p):
            result = original(p)
            pending.rmdir()
            self.junction(pending, outside)
            return result

        with mock.patch.object(auditor, "hash_file", side_effect=substitute):
            with self.assertRaises(AuditError):
                snapshot_folder(self.source)

    def test_reparse_point_policy_injection(self):
        path = self.source / "reparse"
        path.touch()
        actual = path.lstat()
        fake = mock.Mock(st_mode=actual.st_mode, st_dev=actual.st_dev, st_ino=actual.st_ino,
                         st_size=actual.st_size, st_mtime_ns=actual.st_mtime_ns,
                         st_ctime_ns=actual.st_ctime_ns, st_file_attributes=0x400)
        original = Path.lstat
        with mock.patch.object(Path, "lstat", new=lambda p: fake if p == path else original(p)):
            _, data = snapshot_folder(self.source)
        self.assertEqual(data["skipped"], ["reparse"])

    def test_snapshot_entry_limits(self):
        (self.source / "one").touch()
        (self.source / "two").touch()
        with mock.patch.object(auditor, "MAX_ENTRIES", 1):
            with self.assertRaises(AuditError):
                snapshot_folder(self.source)

    def test_input_entry_and_path_limits(self):
        data = self.manifest()
        data["files"]["second"] = next(iter(data["files"].values())).copy()
        path = self.write_manifest(data)
        with mock.patch.object(auditor, "MAX_ENTRIES", 1):
            with self.assertRaises(AuditError):
                auditor.load_snapshot(path)
        for name in ["x" * 4097, "/".join(["part"] * 129), "invisible\u202ename"]:
            with self.subTest(name=repr(name)):
                data = self.manifest()
                data["files"] = {name: next(iter(data["files"].values()))}
                with self.assertRaises(AuditError):
                    auditor.load_snapshot(self.write_manifest(data))

    def test_input_link_policy_injection(self):
        path = self.write_manifest(self.manifest())
        original = Path.lstat
        fake = mock.Mock(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
        with mock.patch.object(Path, "lstat", new=lambda p: fake if p == path else original(p)):
            with mock.patch("builtins.open") as opened:
                with self.assertRaises(AuditError):
                    auditor.load_snapshot(path)
        opened.assert_not_called()

    def test_nonregular_entry_policy_injection(self):
        path = self.source / "special"
        path.touch()
        original = Path.lstat
        actual = path.lstat()
        fake = mock.Mock(st_mode=stat.S_IFIFO | 0o600, st_dev=actual.st_dev,
                         st_ino=actual.st_ino, st_size=actual.st_size,
                         st_mtime_ns=actual.st_mtime_ns, st_ctime_ns=actual.st_ctime_ns,
                         st_file_attributes=0)
        with mock.patch.object(Path, "lstat", new=lambda p: fake if p == path else original(p)):
            with mock.patch.object(auditor, "hash_file") as hashed:
                _, data = snapshot_folder(self.source)
        hashed.assert_not_called()
        self.assertEqual(data["skipped"], ["special"])

    def test_dangling_output_link_policy_injection(self):
        root, data = snapshot_folder(self.source)
        path = self.base / "absent-output-link"
        with mock.patch.object(auditor.os.path, "lexists", return_value=True):
            with mock.patch.object(auditor.tempfile, "mkstemp") as create:
                with self.assertRaises(AuditError):
                    save_snapshot(root, data, path)
        create.assert_not_called()
        self.assertFalse(path.exists())

    def test_malformed_file_metadata(self):
        for value in [None, {}, {"size": True, "sha256": "a" * 64},
                      {"size": -1, "sha256": "a" * 64},
                      {"size": 2**64, "sha256": "a" * 64},
                      {"size": 1, "sha256": "A" * 64},
                      {"size": 1, "sha256": "g" * 64},
                      {"size": 1, "sha256": "a" * 63},
                      {"size": 1, "sha256": "a" * 64, "extra": 0}]:
            with self.subTest(value=value):
                data = self.manifest()
                data["files"]["file.txt"] = value
                with self.assertRaises(AuditError):
                    auditor.load_snapshot(self.write_manifest(data))

    def test_json_shapes_and_nonfinite_values(self):
        path = self.base / "malformed.json"
        for value in ["null", "[]", "42", "{}", "{", "NaN", "Infinity",
                      json.dumps(self.manifest()).replace('"version": 1', '"version": 1.0')]:
            with self.subTest(value=value):
                path.write_text(value, encoding="utf-8")
                with self.assertRaises(AuditError):
                    auditor.load_snapshot(path)

    def test_snapshot_input_directory_and_missing_file(self):
        for path in [self.source, self.base / "missing.json"]:
            with self.subTest(path=path):
                with self.assertRaises(AuditError):
                    auditor.load_snapshot(path)

    def test_input_changes_during_read(self):
        path = self.write_manifest(self.manifest())
        original = auditor.os.fstat
        count = 0

        def change(fd):
            nonlocal count
            info = original(fd)
            count += 1
            if count == 1:
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
            return info

        with mock.patch.object(auditor.os, "fstat", side_effect=change):
            with self.assertRaises(AuditError):
                auditor.load_snapshot(path)

    def test_explicit_skipped_comparison(self):
        data = self.manifest()
        data["skipped"] = ["link"]
        path = self.write_manifest(data)
        self.assertEqual(compare_snapshots(path, path, allow_skipped=True),
                         {"added": [], "removed": [], "modified": []})

    def test_output_missing_parent_and_existing_directory(self):
        root, data = snapshot_folder(self.source)
        for path in [self.base / "absent" / "out.json", self.base]:
            with self.subTest(path=path):
                with self.assertRaises(AuditError):
                    save_snapshot(root, data, path)

    def test_output_relative_root_containment(self):
        root, data = snapshot_folder(self.source)
        # A lexical alias of a path inside source must still be rejected.
        with self.assertRaises(AuditError):
            save_snapshot(root, data, root / ".." / "source" / "out.json")
        self.assertFalse((root / "out.json").exists())

    def test_similarly_named_sibling_is_valid_output(self):
        sibling = self.base / "source-backup"
        sibling.mkdir()
        root, data = snapshot_folder(self.source)
        target = sibling / "out.json"
        save_snapshot(root, data, target)
        self.assertEqual(auditor.load_snapshot(target), {})

    def test_snapshot_write_failure_no_partial_destination(self):
        root, data = snapshot_folder(self.source)
        path = self.base / "out.json"
        with mock.patch.object(auditor.os, "fsync", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(AuditError):
                save_snapshot(root, data, path)
        self.assertFalse(path.exists())
        self.assertEqual(list(self.base.glob(".folder-auditor-*.tmp")), [])

    def test_failure_after_partial_write_cleans_temporary(self):
        root, data = snapshot_folder(self.source)
        target = self.base / "out.json"

        def disk_full(fd, payload):
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload[:5])
                raise OSError("synthetic disk full during write")

        with mock.patch.object(auditor, "_write_payload", side_effect=disk_full):
            with self.assertRaises(AuditError):
                save_snapshot(root, data, target)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.base.glob(".folder-auditor-*.tmp")), [])

    def test_cleanup_refuses_replaced_parent_junction(self):
        if os.name != "nt":
            self.skipTest("Windows junction integration test")
        parent, moved = self.base / "output", self.base / "moved-output"
        parent.mkdir()
        root, data = snapshot_folder(self.source)
        original = auditor._write_payload
        protected = []

        def replace_parent(fd, payload):
            result = original(fd, payload)
            temp_name = next(parent.glob(".folder-auditor-*.tmp")).name
            parent.rename(moved)
            # Put a same-name sentinel in source, then redirect output to it.
            sentinel = self.source / temp_name
            sentinel.write_bytes(b"must survive cleanup")
            protected.append(sentinel)
            self.junction(parent, self.source)
            return result

        with mock.patch.object(auditor, "_write_payload", side_effect=replace_parent):
            with self.assertRaisesRegex(AuditError, "Temporary cleanup failed"):
                save_snapshot(root, data, parent / "out.json")
        self.assertFalse((self.source / "out.json").exists())
        self.assertEqual(len(protected), 1)
        self.assertEqual(protected[0].read_bytes(), b"must survive cleanup")
        self.assertEqual(len(list(moved.glob(".folder-auditor-*.tmp"))), 1)

    def test_cleanup_refuses_replaced_temporary_file(self):
        root, data = snapshot_folder(self.source)
        original = auditor._write_payload
        protected = []

        def substitute(fd, payload):
            result = original(fd, payload)
            path = next(self.base.glob(".folder-auditor-*.tmp"))
            # Preserve original inode at a different name before substitution.
            path.rename(self.base / "held-original")
            path.write_bytes(b"competing file")
            protected.append(path)
            return result

        with mock.patch.object(auditor, "_write_payload", side_effect=substitute):
            with self.assertRaisesRegex(AuditError, "Temporary cleanup failed"):
                save_snapshot(root, data, self.base / "out.json")
        self.assertFalse((self.base / "out.json").exists())
        self.assertEqual(protected[0].read_bytes(), b"competing file")

    def test_publish_race_never_overwrites_competing_file(self):
        root, data = snapshot_folder(self.source)
        path = self.base / "out.json"
        original = auditor.os.link

        def competing_writer(src, dst):
            Path(dst).write_bytes(b"other writer")
            return original(src, dst)

        with mock.patch.object(auditor.os, "link", side_effect=competing_writer):
            with self.assertRaises(AuditError):
                save_snapshot(root, data, path)
        self.assertEqual(path.read_bytes(), b"other writer")
        self.assertEqual(list(self.base.glob(".folder-auditor-*.tmp")), [])

    def test_hardlink_unavailable_fails_without_fallback(self):
        root, data = snapshot_folder(self.source)
        path = self.base / "out.json"
        with mock.patch.object(auditor.os, "link", side_effect=OSError("hard links unsupported")):
            with self.assertRaises(AuditError):
                save_snapshot(root, data, path)
        self.assertFalse(path.exists())
        self.assertEqual(list(self.base.glob(".folder-auditor-*.tmp")), [])

    def test_cleanup_failure_reports_complete_published_destination(self):
        root, data = snapshot_folder(self.source)
        path = self.base / "out.json"
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("synthetic cleanup failure")):
            with self.assertRaisesRegex(AuditError, "Temporary cleanup failed"):
                save_snapshot(root, data, path)
        self.assertEqual(auditor.load_snapshot(path), {})
        leftovers = list(self.base.glob(".folder-auditor-*.tmp"))
        self.assertEqual(len(leftovers), 1)
        leftovers[0].unlink()

    def test_oversized_output_not_created(self):
        root, data = snapshot_folder(self.source)
        path = self.base / "out.json"
        with mock.patch.object(auditor, "MAX_SNAPSHOT_BYTES", 1):
            with self.assertRaises(AuditError):
                save_snapshot(root, data, path)
        self.assertFalse(path.exists())

    def test_real_hardlink_content_is_inventoried(self):
        path = self.source / "original"
        path.write_bytes(b"synthetic shared contents")
        alias = self.source / "alias"
        try:
            os.link(path, alias)
        except OSError as exc:
            self.skipTest(f"Hard links unavailable: {exc}")
        _, data = snapshot_folder(self.source)
        self.assertEqual(data["files"]["alias"], data["files"]["original"])

    def test_existing_output_preserved(self):
        root, data = snapshot_folder(self.source)
        path = self.base / "out.json"
        path.write_bytes(b"original bytes")
        with self.assertRaises(AuditError):
            save_snapshot(root, data, path)
        self.assertEqual(path.read_bytes(), b"original bytes")

    def test_source_bytes_names_and_mtimes_unchanged(self):
        path = self.source / "original"
        path.write_bytes(b"synthetic unchanged source")
        before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in self.source.iterdir()))
        root, data = snapshot_folder(self.source)
        save_snapshot(root, data, self.base / "out.json")
        after = (path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in self.source.iterdir()))
        self.assertEqual(before, after)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(Path(auditor.__file__).resolve()), *map(str, args)],
                              capture_output=True, text=True, encoding="utf-8", timeout=20)

    def test_cli_roundtrip_exit_codes_and_json(self):
        file = self.source / "file"
        file.write_bytes(b"before")
        before, after = self.base / "before.json", self.base / "after.json"
        self.assertEqual(self.cli("snapshot", self.source, "--out", before).returncode, 0)
        same = self.cli("compare", before, before, "--json")
        self.assertEqual(same.returncode, 0, same.stderr)
        self.assertEqual(json.loads(same.stdout), {"added": [], "removed": [], "modified": []})
        file.write_bytes(b"after")
        self.assertEqual(self.cli("snapshot", self.source, "--out", after).returncode, 0)
        changed = self.cli("compare", before, after, "--json")
        self.assertEqual(changed.returncode, 1, changed.stderr)
        self.assertEqual(json.loads(changed.stdout)["modified"], ["file"])
        error = self.cli("compare", before, self.base / "missing")
        self.assertEqual(error.returncode, 2)
        self.assertNotIn("Traceback", error.stderr)

    def test_cli_skipped_warning_separate_from_json(self):
        data = self.manifest()
        data["skipped"] = ["link"]
        path = self.write_manifest(data)
        blocked = self.cli("compare", path, path, "--json")
        self.assertEqual(blocked.returncode, 2)
        allowed = self.cli("compare", path, path, "--allow-skipped", "--json")
        self.assertEqual(allowed.returncode, 0)
        self.assertIn("Warning", allowed.stderr)
        self.assertEqual(json.loads(allowed.stdout), {"added": [], "removed": [], "modified": []})

    def test_cli_unsafe_destination_does_not_create_output(self):
        out = self.source / "out.json"
        result = self.cli("snapshot", self.source, "--out", out)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(out.exists())
        self.assertNotIn("Traceback", result.stderr)

    def test_cli_help_version_and_usage(self):
        self.assertEqual(self.cli("--help").returncode, 0)
        self.assertEqual(self.cli("--version").stdout.strip(), "0.1.0")
        self.assertEqual(self.cli("unknown").returncode, 2)


if __name__ == "__main__":
    unittest.main()
