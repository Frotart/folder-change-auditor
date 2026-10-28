#!/usr/bin/env python3
"""Run the real CLI against disposable, entirely synthetic example files."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    auditor = Path(__file__).resolve().parents[1] / "folder_auditor.py"
    with tempfile.TemporaryDirectory(prefix="folder-auditor-example-") as name:
        base = Path(name).resolve()  # real path, e.g. /private/var on macOS
        source = base / "source"
        source.mkdir()
        (source / "changed.txt").write_text("old\n", encoding="utf-8")
        (source / "removed.txt").write_text("synthetic old file\n", encoding="utf-8")

        def run(*args, expected=0):
            result = subprocess.run([sys.executable, str(auditor), *map(str, args)],
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            if result.returncode != expected:
                raise RuntimeError(f"Unexpected exit {result.returncode}: {result.stderr}")
            return result

        before, after = base / "before.json", base / "after.json"
        run("snapshot", source, "--out", before)
        run("compare", before, before, "--json")
        (source / "changed.txt").write_text("new\n", encoding="utf-8")
        (source / "removed.txt").unlink()
        (source / "added.txt").write_text("synthetic new file\n", encoding="utf-8")
        run("snapshot", source, "--out", after)
        result = run("compare", before, after, "--json", expected=1)
        expected = {"added": ["added.txt"], "removed": ["removed.txt"], "modified": ["changed.txt"]}
        if json.loads(result.stdout) != expected:
            raise RuntimeError("Example comparison did not match expected changes")
        print(result.stdout, end="")
        print("Verified: unchanged comparison exits 0; changed comparison exits 1.")
    print("Disposable example files cleaned up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
