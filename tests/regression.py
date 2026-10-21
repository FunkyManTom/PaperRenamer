#!/usr/bin/env python3
"""Regression test: identify every PDF in a folder and compare with the expected results.

Online lookups are replayed from tests/responses.json, so the test runs offline and gives the
same answers every time. Only the PDFs aren't stored here (they're your own files).

    python tests/regression.py                    # replay and compare
    python tests/regression.py --record           # refresh responses.json from the live services
    python tests/regression.py --update           # accept the current results as expected
    python tests/regression.py --folder ~/papers  # another folder (default: test_pdf)

Record after adding a lookup (new requests aren't in responses.json yet), and review the diff
before running --update.

The test PDFs may be renamed, moved into the folder under any name, or have details written
into them by Paper Renamer: each is recognised by the hash of its original contents, and
the test runs on links (or, if details were written, restored copies) with the original
names in the work folder (default tests/.pdfs; restored copies are kept there for next time).
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import requests
from requests.structures import CaseInsensitiveDict

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import rename_papers as rp  # noqa: E402

RESPONSES = HERE / "responses.json"
EXPECTED = HERE / "expected.json"
WORKDIR = HERE / ".pdfs"
WORKDIR_MARKER = ".regression-workdir"  # only a folder with this file in it gets cleaned up
MUPDF_UPDATE = b"\n% Written by MuPDF"  # how Paper Renamer's metadata writes (incremental saves) begin


def original_bytes(data):
    """The PDF as it was before Paper Renamer wrote details into it: those writes are appended."""
    i = data.find(MUPDF_UPDATE)
    return data[:i] if i >= 0 else data


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def prepare(folder, expected, workdir):
    """Put each PDF of `folder` into `workdir` under its original name: a link if its contents
    are unchanged, else a restored copy. Returns the paths to test, in name order."""
    by_hash = {want["sha256"]: name for name, want in expected.items() if want.get("sha256")}
    folder, workdir = Path(folder).resolve(), Path(workdir).resolve()
    if folder == workdir or folder in workdir.parents or workdir in folder.parents:
        sys.exit(f"The work folder ({workdir}) must be separate from the PDFs ({folder}): files in it get replaced.")
    marker = workdir / WORKDIR_MARKER
    if workdir.is_dir() and any(workdir.iterdir()) and not marker.exists():
        sys.exit(f"{workdir} isn't empty and wasn't made by this test; choose another --workdir.")
    workdir.mkdir(parents=True, exist_ok=True)
    marker.touch()
    wanted = {}
    for pdf in sorted(Path(folder).iterdir()):
        if pdf.suffix.lower() != ".pdf" or pdf.name.startswith("."):
            continue
        data = pdf.read_bytes()
        whole, original = sha256(data), None
        name = by_hash.get(whole)
        if not name:
            restored = original_bytes(data)
            name = by_hash.get(sha256(restored))
            original = restored if name else None
        name = name or pdf.name  # not in expected.json yet: tested under its own name
        target = workdir / name
        wanted[name] = target
        if original is not None:  # restored copy (kept: writing it is the slow part)
            if not (target.is_file() and not target.is_symlink() and target.stat().st_size == len(original)):
                if target.is_symlink() or target.exists():
                    target.unlink()
                target.write_bytes(original)
            continue
        if target.is_symlink() or target.exists():
            target.unlink()
        try:
            os.symlink(pdf.resolve(), target)
        except OSError:  # Windows without symlink rights
            try:
                os.link(pdf, target)
            except OSError:
                shutil.copy2(pdf, target)
    for stale in workdir.iterdir():
        if stale.name not in wanted and stale.name != WORKDIR_MARKER:
            stale.unlink()
    return [wanted[n] for n in sorted(wanted)]


def request_key(url, params=None, headers=None):
    full = requests.Request("GET", url, params=params).prepare().url
    accept = (headers or {}).get("Accept", "")
    return f"{full} | {accept}" if accept else full


class Replay:
    """Stands in for rp.session.get: answers from recorded responses, or records live ones."""

    def __init__(self, record):
        self.record = record
        self.live = rp.session.get
        self.saved = json.loads(RESPONSES.read_text(encoding="utf-8")) if RESPONSES.exists() else {}
        self.used, self.missing = set(), []

    def get(self, url, params=None, headers=None, timeout=None):
        key = request_key(url, params, headers)
        self.used.add(key)
        if self.record:
            r = self.live(url, params=params, headers=headers, timeout=timeout)
            self.saved[key] = {"status": r.status_code, "type": r.headers.get("Content-Type", ""),
                               "body": r.content.decode("utf-8", "replace")}
        if key not in self.saved:
            self.missing.append(key)
            raise requests.ConnectionError(f"not recorded (run with --record): {key}")
        entry = self.saved[key]
        r = requests.Response()
        r.status_code, r.url, r.encoding = entry["status"], key, "utf-8"
        r.headers = CaseInsensitiveDict({"Content-Type": entry["type"]})
        r._content = entry["body"].encode("utf-8")
        return r

    def save(self):
        # Drop responses no lookup asks for any more, so the file doesn't grow forever.
        kept = {k: v for k, v in sorted(self.saved.items()) if k in self.used}
        RESPONSES.write_text(json.dumps(kept, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", type=Path, default=HERE.parent / "test_pdf")
    ap.add_argument("--workdir", type=Path, default=WORKDIR, help="where the original-named copies go")
    ap.add_argument("--record", action="store_true", help="query the live services and save their answers")
    ap.add_argument("--update", action="store_true", help="save the current results as the expected ones")
    args = ap.parse_args()

    replay = Replay(args.record)
    rp.session.get = replay.get
    if not args.record:
        rp.time = type("NoSleep", (), {"sleep": staticmethod(lambda s: None)})

    expected = json.loads(EXPECTED.read_text(encoding="utf-8")) if EXPECTED.exists() else {}
    pdfs = prepare(args.folder, expected, args.workdir)
    results, failures = {}, 0
    for plan in rp.plan_renames(pdfs, cache=None):
        name = plan.pdf.name
        got = {"name": plan.target.name if plan.target else "", "how": plan.detail,
               "sha256": expected.get(name, {}).get("sha256") or sha256(original_bytes(plan.pdf.read_bytes()))}
        if plan.status == "skip":
            got["name"] = ""
        results[name] = got
        want = expected.get(name)
        if want is None:
            print(f"  NEW   {name[:70]}\n        -> {got['name'] or '(not found)'}  [{got['how'][:80]}]")
        elif {k: want.get(k) for k in ("name", "how")} != {k: got[k] for k in ("name", "how")}:
            failures += 1
            print(f"  FAIL  {name[:70]}\n        expected {want['name'] or '(not found)'}  [{want['how'][:80]}]"
                  f"\n        got      {got['name'] or '(not found)'}  [{got['how'][:80]}]")
        else:
            print(f"  ok    {name[:70]}")
    gone = [n for n in expected if n not in results]
    for n in gone:
        print(f"  GONE  {n[:70]} (expected, but not in the folder)")

    if args.record:
        replay.save()
        print(f"\nRecorded {len(replay.used)} responses to {RESPONSES.name}.")
    if replay.missing:
        print(f"\n{len(replay.missing)} requests weren't recorded; run with --record.")
    if args.update:
        EXPECTED.write_text(json.dumps(results, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Saved {len(results)} results to {EXPECTED.name}.")
        return 0
    print(f"\n{len(results) - failures} of {len(results)} as expected"
          + (f", {failures} changed." if failures else "."))
    return 1 if failures or replay.missing else 0


if __name__ == "__main__":
    sys.exit(main())
