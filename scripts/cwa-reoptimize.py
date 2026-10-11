#!/usr/bin/env python3
"""
Bring a Calibre-Web-Automated library in line with the single "(x)" copy scheme.

  1. Remove the legacy "(X3) "/"(X4) " copies whose untagged original is in the library.
  2. Give every untagged EPUB book an optimized "(x) " copy, in batches that the
     optimizer stack and CWA's ingest can absorb.

Runs on the Docker host (needs docker; run with sudo). Dry run by default: it prints
the plan and changes nothing until --apply is given. Safe to re-run: books that
already have an "(x)" copy, or were handled by an earlier run (state file), are
skipped.

  sudo python3 scripts/cwa-reoptimize.py                    # show the plan
  sudo python3 scripts/cwa-reoptimize.py --apply --limit 3  # try three books
  sudo python3 scripts/cwa-reoptimize.py --apply            # do the rest

Removed books go to Calibre's trash (calibredb remove without --permanent).
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# "(x)"/"(X)" is the current optimized-copy tag; "(X3)"/"(X4)" are the old per-device ones.
TAG_RE = re.compile(r'^\((x|X|X3|X4)\) |\s\((x|X|X3|X4)\)$')


def classify(title: str):
    """Return (kind, untagged_title): kind is 'legacy', 'optimized' or 'original'."""
    m = TAG_RE.search(title)
    if not m:
        return 'original', title
    tag = m.group(1) or m.group(2)
    untagged = TAG_RE.sub('', title).strip()
    return ('legacy' if tag in ('X3', 'X4') else 'optimized'), untagged


def book_key(title: str, authors: str):
    norm = lambda s: re.sub(r'\s+', ' ', (s or '').lower()).strip()  # noqa: E731
    return norm(title), norm(authors)


def epub_path(book: dict):
    for path in book.get('formats') or []:
        if path.lower().endswith('.epub'):
            return path
    return None


def plan(books: list, done_ids: set):
    """Split the library into removals, optimization jobs and things to report."""
    originals, optimized, legacy = {}, set(), []
    for b in books:
        kind, untagged = classify(b['title'])
        key = book_key(untagged, b.get('authors', ''))
        if kind == 'original':
            originals.setdefault(key, []).append(b)
        elif kind == 'optimized':
            optimized.add(key)
        else:
            legacy.append((key, b))

    remove, orphans = [], []
    for key, b in legacy:
        (remove if key in originals else orphans).append(b)

    jobs, no_epub, skipped = [], [], 0
    for key, group in originals.items():
        for b in group:
            if key in optimized or b['id'] in done_ids:
                skipped += 1
            elif epub_path(b):
                jobs.append(b)
            else:
                no_epub.append(b)
    jobs.sort(key=lambda b: b['id'])
    return remove, orphans, jobs, no_epub, skipped


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


class Host:
    def __init__(self, a):
        self.a = a
        self.compose = ['docker', 'compose', '-f', str(Path(a.optimizer_dir) / 'docker-compose.calibre.yml'),
                        '--project-directory', a.optimizer_dir]

    def calibredb(self, *args):
        return run(['docker', 'exec', '-u', self.a.cwa_user, self.a.container, 'calibredb', *args,
                    f'--library-path={self.a.container_library}'])

    def list_books(self):
        out = self.calibredb('list', '--fields', 'id,title,authors,formats', '--for-machine').stdout
        return json.loads(out)

    def host_path(self, container_path: str) -> Path:
        rel = os.path.relpath(container_path, self.a.container_library)
        return Path(self.a.library) / rel

    def ingest_busy(self) -> bool:
        ingest = Path(self.a.ingest)
        if any(p.is_file() and not p.name.startswith('.') for p in ingest.iterdir()):
            return True
        out = subprocess.run(self.compose + ['exec', '-T', 'optimizer', 'sh', '-c', 'ls /output/*.epub 2>/dev/null'],
                             text=True, capture_output=True).stdout
        return bool(out.strip())

    def wait_idle(self, timeout_s: int):
        start = time.time()
        while self.ingest_busy():
            if time.time() - start > timeout_s:
                raise TimeoutError(f'ingest still busy after {timeout_s}s')
            time.sleep(10)
        time.sleep(5)  # CWA deletes the file only after its import finishes; small settle margin


def title_overwrite_risk(cwa_db: Path):
    """True if CWA fetches metadata on import AND may replace the title. That would
    strip the "(x) " tag from new copies, and a later run would then optimize those
    copies again as if they were originals. None if the settings can't be read."""
    try:
        con = sqlite3.connect(f'file:{cwa_db}?mode=ro', uri=True)
        row = con.execute('SELECT auto_metadata_fetch_enabled, auto_metadata_update_title '
                          'FROM cwa_settings LIMIT 1').fetchone()
        con.close()
    except sqlite3.Error:
        return None
    return bool(row and row[0] and row[1])


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {'done_ids': []}


def save_state(path: Path, state: dict):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(path)


def describe(b):
    return f"#{b['id']} {b['title']} — {b.get('authors', '')}"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--apply', action='store_true', help='make changes (default: dry run)')
    p.add_argument('--skip-remove', action='store_true', help='keep the (X3)/(X4) copies')
    p.add_argument('--skip-optimize', action='store_true', help='only remove the (X3)/(X4) copies')
    p.add_argument('--limit', type=int, default=0, help='optimize at most N books this run')
    p.add_argument('--batch', type=int, default=5, help='books per optimizer batch (default 5)')
    p.add_argument('--batch-timeout', type=int, default=1800, help='seconds to wait for CWA per batch')
    p.add_argument('--library', default='/srv/dev-disk-by-uuid-e177ff75-ca81-42d6-bfa7-d9c5ea37289f/ebooks',
                   help='Calibre library on the host')
    p.add_argument('--container', default='calibre-web-automated')
    p.add_argument('--container-library', default='/calibre-library')
    p.add_argument('--cwa-user', default='abc', help='user CWA runs as inside its container')
    p.add_argument('--optimizer-dir', default='/mnt/ssd/docker/src/auto-epub-optimizer')
    p.add_argument('--incoming', default='/mnt/ssd/books/incoming', help='optimizer drop folder on the host')
    p.add_argument('--ingest', default='/mnt/ssd/books/cwa-ingest', help='CWA ingest folder on the host')
    p.add_argument('--prefix', default='(x) ')
    p.add_argument('--cwa-db', default='/mnt/ssd/docker/calibre-web/config/cwa.db',
                   help="CWA's settings database on the host (to check metadata fetch)")
    p.add_argument('--allow-title-fetch', action='store_true',
                   help="optimize even if CWA's metadata fetch may replace titles")
    p.add_argument('--state', default=None, help='state file (default: <incoming>/.reoptimize-state.json)')
    a = p.parse_args(argv)

    host = Host(a)
    state_path = Path(a.state or Path(a.incoming) / '.reoptimize-state.json')
    state = load_state(state_path)
    done_ids = set(state.get('done_ids', []))

    books = host.list_books()
    remove, orphans, jobs, no_epub, skipped = plan(books, done_ids)
    if a.limit:
        jobs = jobs[:a.limit]

    print(f"Library: {len(books)} books")
    print(f"\n(X3)/(X4) copies to remove (original present): {len(remove)}")
    for b in remove:
        print('  -', describe(b))
    if orphans:
        print(f"\n(X3)/(X4) copies KEPT — no untagged original found: {len(orphans)}")
        for b in orphans:
            print('  ?', describe(b))
    print(f"\nOriginals to optimize: {len(jobs)}  (already done: {skipped})")
    for b in jobs:
        print('  +', describe(b))
    if no_epub:
        print(f"\nOriginals without an EPUB format (skipped): {len(no_epub)}")
        for b in no_epub:
            print('  !', describe(b))

    risk = title_overwrite_risk(Path(a.cwa_db))
    blocked = jobs and not a.skip_optimize and risk is not False and not a.allow_title_fetch
    if blocked:
        why = ("CWA fetches metadata on import and may replace titles, which would strip the "
               "\"(x) \" tag" if risk else f"could not read CWA settings at {a.cwa_db}")
        print(f"\nWARNING: {why}. Turn off 'Update title' (or metadata fetch) in "
              "Admin > CWA Settings while this runs, or pass --allow-title-fetch.")

    if not a.apply:
        print("\nDry run — nothing changed. Re-run with --apply.")
        return 0
    if blocked:
        print("Not optimizing. Use --skip-optimize to only remove the (X3)/(X4) copies.")
        return 2

    host.wait_idle(a.batch_timeout)

    if remove and not a.skip_remove:
        ids = ','.join(str(b['id']) for b in remove)
        print(f"\nRemoving {len(remove)} legacy copies (to Calibre's trash)...")
        host.calibredb('remove', ids)
        print("  done")

    if a.skip_optimize or not jobs:
        print("\nFinished.")
        return 0

    stage = Path(a.incoming) / '.reoptimize'  # hidden subfolder: the optimizer only scans the top level
    stage.mkdir(exist_ok=True)
    failed = 0
    for i in range(0, len(jobs), a.batch):
        batch = jobs[i:i + a.batch]
        print(f"\nBatch {i // a.batch + 1}: books {i + 1}-{i + len(batch)} of {len(jobs)}")
        for b in batch:
            src = host.host_path(epub_path(b))
            dst = stage / f"{b['id']}.epub"
            try:
                shutil.copyfile(src, dst)
            except OSError as e:
                print(f"  ! copy failed {describe(b)}: {e}")
                failed += 1
                continue
            # One optimizer call per book so each copy is titled from Calibre's title,
            # which can differ from the title inside the EPUB file.
            cmd = host.compose + ['exec', '-T', 'optimizer', 'python3', '/app/cli/optimize.py',
                                  '-o', '/output', f'--title-prefix={a.prefix}', f"--title={b['title']}",
                                  f"/bookdrop/.reoptimize/{dst.name}"]
            res = subprocess.run(cmd, text=True, capture_output=True)
            dst.unlink(missing_ok=True)
            if res.returncode == 0:
                print(f"  ok {describe(b)}")
                done_ids.add(b['id'])
                save_state(state_path, {'done_ids': sorted(done_ids)})
            else:
                failed += 1
                print(f"  ! optimizer failed {describe(b)}:\n{res.stdout[-800:]}{res.stderr[-800:]}")
        print("  waiting for CWA to import the batch...")
        host.wait_idle(a.batch_timeout)

    print(f"\nFinished: {len(jobs) - failed} optimized, {failed} failed.")
    print("If Calibre-Web still lists removed books, restart it: docker restart calibre-web-automated")
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
