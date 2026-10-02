#!/usr/bin/env python3
"""
dedupe.py - Find and remove duplicate folders and files (macOS).

Step 0 - empty placeholders: leftovers of an interrupted copy or iCloud sync
(greyed out in Finder, "Zero bytes"). These count as placeholders:
  - a 0-byte file whose name has a copy number ("x 6.pdf", "x.torrent 6"),
    that Finder marked as an unfinished copy, or whose type can never be
    empty (PDF, JPEG, MP4, DOCX, ...)
  - a folder or app with a copy number ("Dentsu 6", "OpenCore-Patcher 6")
    that contains no data at all (only empty files / empty subfolders)
A placeholder is removed when a real (non-empty) version with the same name
sits in the same folder; otherwise it is only listed as a warning, because the
real item may never have arrived (--empty-orphans removes those too).
Ordinary empty files (e.g. notes.txt, __init__.py) are never touched.

Step 1 - folders: two folders are duplicates when they contain the same files
(same names, same contents) in the same subfolder layout. The folders' own
names may differ ("Photos" vs "Photos copy"). Only the top-most duplicate
folder is reported, not every matching subfolder inside it.

Step 2 - files: remaining files are compared by content (SHA-256) after cheap
pre-filtering by size (and optionally by name). Files inside folders that are
already being removed in step 1 are left out of this step.

Nothing is removed unless you pass --delete, and by default removed items go
to the Trash (so Finder's "Put Back" works).

Examples:
  python3 dedupe.py ~/Downloads                     # dry run: report only
  python3 dedupe.py ~/Downloads --delete            # move duplicates to Trash
  python3 dedupe.py ~/Backups --folders-only        # only whole duplicate folders
  python3 dedupe.py ~/Pictures --no-folders         # only individual files
  python3 dedupe.py ~/Pictures --match name         # files must also have similar names
  python3 dedupe.py ~/Music --prefer ~/Music/Main --delete
  python3 dedupe.py ~/Docs --log dupes.csv          # save a CSV report
  python3 dedupe.py ~/Desktop --empty-orphans       # also remove placeholders with no original

Don't run with --delete while a copy, migration or iCloud sync is still in
progress: unfinished files look exactly like placeholders until they're done.
"""

import argparse
import csv
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

CHUNK = 1024 * 1024        # 1 MB read chunks for full hashing
PARTIAL = 64 * 1024        # first 64 KB for the quick pre-hash
SKIP_NAMES = {".DS_Store", ".localized", "Icon\r"}   # Finder clutter, ignored everywhere

# macOS "packages": look like folders but must never be deduped internally
BUNDLE_EXTS = {
    ".app", ".photoslibrary", ".musiclibrary", ".tvlibrary", ".imovielibrary",
    ".fcpbundle", ".logicx", ".band", ".lrlibrary", ".lrdata", ".bundle",
    ".framework", ".plugin", ".kext", ".pkg", ".mpkg", ".xcodeproj",
    ".xcworkspace", ".rtfd", ".pages", ".numbers", ".key",
}

# File types that can't be valid when empty: a 0-byte one is a broken copy
NONEMPTY_EXTS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".tif", ".tiff",
    ".webp", ".bmp", ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".psd",
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mp3", ".m4a", ".wav", ".aac", ".flac",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pages", ".numbers", ".key",
    ".zip", ".rar", ".7z", ".dmg", ".pkg", ".iso", ".torrent", ".epub",
}

# "name copy", "name copy 2", "name (1)", "name 2"
COPY_SUFFIX = re.compile(r"(?:\s+copy(?:\s+\d+)?|\s*\(\d+\)|\s+\d+)$", re.IGNORECASE)


# ---------- helpers ----------

def warn(msg):
    print(f"  ! {msg}", file=sys.stderr)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def sha(obj):
    return hashlib.sha256(repr(obj).encode()).hexdigest()


def strip_copy_suffix(stem):
    prev = None
    while prev != stem:
        prev, stem = stem, COPY_SUFFIX.sub("", stem)
    return stem.strip()


def split_name(name):
    """-> (base, ext, has_copy_number). Handles "x 6.pdf", "x (1) 6.pdf",
    "x.torrent 6" (number after the extension) and "Folder 6"."""
    stem, ext = os.path.splitext(name)
    copied = False
    if ext and strip_copy_suffix(ext) != ext:
        ext, copied = strip_copy_suffix(ext), True
    base = strip_copy_suffix(stem)
    copied = copied or base != stem.strip()
    return (base or stem), ext, copied


def has_copy_suffix(p):
    return split_name(p.name)[2]


def normalized_name(p):
    base, ext, _ = split_name(p.name)
    return (base + ext).lower()


def created(p):
    st = p.stat()
    return getattr(st, "st_birthtime", st.st_mtime)


def is_bundle(dirname):
    return Path(dirname).suffix.lower() in BUNDLE_EXTS


def inside(p, dirs):
    """True if p is one of dirs or lies anywhere inside one of them."""
    return p in dirs or any(a in dirs for a in p.parents)


@lru_cache(maxsize=None)
def hash_file(path, limit=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if limit:
            h.update(f.read(limit))
        else:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                h.update(chunk)
    return h.hexdigest()


def group_by(items, keyfunc):
    groups = defaultdict(list)
    for item in items:
        try:
            groups[keyfunc(item)].append(item)
        except OSError as e:
            warn(f"skipping {item}: {e}")
    return [g for g in groups.values() if len(g) > 1]


# ---------- scanning ----------

def scan(root, args):
    """Walk the folder once. Returns (sizes, tree).

    sizes: {file: size}
    tree:  {folder: {"files": [...], "dirs": [...], "pure": bool}}

    A folder is "pure" when nothing in it was skipped (hidden items, packages,
    symlinks, unreadable entries). Only folders whose whole subtree is pure can
    count as duplicate folders, so a folder is never removed because of content
    the script didn't look at.
    """
    sizes, tree = {}, {}
    recursive = not args.no_recursive

    def on_error(e):
        warn(f"cannot read {e.filename}: {e.strerror}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_error):
        d = Path(dirpath)
        node = {"files": [], "dirs": [], "bundles": [], "pure": True}
        tree[d] = node

        keep = []
        for name in dirnames:
            hidden = name.startswith(".") and not args.include_hidden
            if hidden or (d / name).is_symlink():
                node["pure"] = False
            elif is_bundle(name):
                node["pure"] = False
                node["bundles"].append(d / name)
            else:
                keep.append(name)
        dirnames[:] = sorted(keep) if recursive else []
        node["dirs"] = [d / n for n in dirnames]

        for name in sorted(filenames):
            if name in SKIP_NAMES:
                continue
            p = d / name
            if name.startswith(".") and not args.include_hidden:
                node["pure"] = False
                continue
            try:
                if p.is_symlink() or not p.is_file():
                    node["pure"] = False
                    continue
                sizes[p] = p.stat().st_size
                node["files"].append(p)
            except OSError as e:
                warn(f"skipping {p}: {e}")
                node["pure"] = False
    return sizes, tree


# ---------- empty placeholders ----------

def is_finder_busy(p):
    """Finder marks an in-progress copy with creation date 24 Jan 1984 and
    file type 'brok'; that's what makes such files appear greyed out."""
    try:
        bt = getattr(p.stat(), "st_birthtime", None)
    except OSError:
        return False
    if bt is not None and time.localtime(bt)[:3] == (1984, 1, 24):
        return True
    if sys.platform != "darwin":
        return False
    try:
        r = subprocess.run(["xattr", "-px", "com.apple.FinderInfo", str(p)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False
        return bytes.fromhex("".join(r.stdout.split()))[:4] == b"brok"
    except (OSError, ValueError):
        return False


def subtree_stats(tree, sizes):
    """Bottom-up: total bytes under each folder, and whether the whole subtree
    was fully visible to the scan."""
    total, pure = {}, {}
    for d in sorted(tree, key=lambda p: len(p.parts), reverse=True):
        node, kids = tree[d], tree[d]["dirs"]
        pure[d] = node["pure"] and all(pure.get(c, False) for c in kids)
        total[d] = sum(sizes[p] for p in node["files"]) + sum(total.get(c, 0) for c in kids)
    return total, pure


def holds_no_data(path):
    """For packages (not scanned): True if every file inside is 0 bytes."""
    ok = [True]

    def on_error(_):
        ok[0] = False

    for dp, _, fns in os.walk(path, onerror=on_error):
        for f in fns:
            fp = os.path.join(dp, f)
            try:
                if os.path.islink(fp) or os.path.getsize(fp) > 0:
                    return False
            except OSError:
                return False
    return ok[0]


def find_placeholders(tree, sizes):
    """Returns [(path, real_copy_or_None, reason)] for empty leftover files,
    folders and packages."""
    total, pure = subtree_stats(tree, sizes)
    empty_cache = {}

    def no_data(x):
        if x not in empty_cache:
            empty_cache[x] = (pure[x] and total[x] == 0) if x in tree else holds_no_data(x)
        return empty_cache[x]

    def best(cands):
        return min(cands, key=lambda q: (has_copy_suffix(q), len(q.name), q.name)) if cands else None

    found = []
    for node in tree.values():
        # files
        same = defaultdict(list)
        for p in node["files"]:
            same[normalized_name(p)].append(p)
        for p in node["files"]:
            if sizes[p] != 0:
                continue
            _, ext, copied = split_name(p.name)
            busy, broken = is_finder_busy(p), ext.lower() in NONEMPTY_EXTS
            if not (busy or copied or broken):
                continue                      # an ordinary empty file: leave it
            real = [q for q in same[normalized_name(p)] if q != p and sizes[q] > 0]
            if busy:
                why = "unfinished Finder copy"
            elif copied and (real or not broken):
                why = "empty copy"
            else:
                why = f"empty {ext.lower()} can't be a valid file"
            found.append((p, best(real), why))

        # folders and packages (apps etc.) with a copy number and no data inside
        subs = node["dirs"] + node["bundles"]
        same = defaultdict(list)
        for x in subs:
            same[normalized_name(x)].append(x)
        for x in subs:
            if not has_copy_suffix(x) or not no_data(x):
                continue
            real = [y for y in same[normalized_name(x)] if y != x and not no_data(y)]
            kind = "empty copied app/package" if x in node["bundles"] else "empty copied folder"
            found.append((x, best(real), kind))
    return sorted(found, key=lambda e: str(e[0]))


# ---------- duplicate folders ----------

def folder_summaries(tree, sizes):
    """Bottom-up pass: cheap signature (names + sizes), file count, total bytes."""
    sig, nfiles, nbytes = {}, {}, {}
    for d in sorted(tree, key=lambda p: len(p.parts), reverse=True):
        node, kids = tree[d], tree[d]["dirs"]
        if not node["pure"] or any(sig.get(c) is None for c in kids):
            sig[d] = None
            continue
        entries = sorted([("f", p.name, sizes[p]) for p in node["files"]] +
                         [("d", c.name, sig[c]) for c in kids])
        sig[d] = sha(entries)
        nfiles[d] = len(node["files"]) + sum(nfiles[c] for c in kids)
        nbytes[d] = sum(sizes[p] for p in node["files"]) + sum(nbytes[c] for c in kids)
    return sig, nfiles, nbytes


def content_signature(d, tree, memo):
    """Same as the cheap signature, but with file content hashes instead of sizes."""
    if d not in memo:
        node = tree[d]
        entries = sorted([("f", p.name, hash_file(p)) for p in node["files"]] +
                         [("d", c.name, content_signature(c, tree, memo)) for c in node["dirs"]])
        memo[d] = sha(entries)
    return memo[d]


def find_duplicate_folders(root, tree, sizes, fast):
    sig, nfiles, nbytes = folder_summaries(tree, sizes)
    by_sig = defaultdict(list)
    for d, s in sig.items():
        if s is not None and d != root and nfiles[d] > 0:   # ignore empty folders
            by_sig[s].append(d)
    groups = [g for g in by_sig.values() if len(g) > 1]
    if groups and not fast:
        print(f"Verifying {len(groups)} candidate folder groups by content...")
        memo = {}
        groups = [g2 for g in groups
                  for g2 in group_by(g, lambda d: content_signature(d, tree, memo))]
    return groups, nfiles, nbytes


def plan_folder_removals(groups, keep, prefer):
    """Decide top-down which folders to remove.

    Groups are handled shallowest first, and folders already inside a folder
    being removed are dropped, so only top-most duplicates are reported.
    Copies inside a folder that is being kept are preferred as keepers (so kept
    folders stay intact), and copies nested inside other duplicates are avoided.
    """
    all_dup = {d for g in groups for d in g}
    plan, removed, kept = [], set(), set()

    def nest_rank(d):
        if any(a in kept for a in d.parents):
            return 0        # inside a folder we're keeping: keep it intact
        if any(a in all_dup for a in d.parents):
            return 2        # inside another duplicate folder that may be removed
        return 1

    for g in sorted(groups, key=lambda g: (min(len(d.parts) for d in g), str(min(g)))):
        live = [d for d in g if not inside(d, removed)]
        if len(live) < 2:
            continue
        live.sort(key=lambda d: keeper_key(d, keep, prefer, nested=nest_rank(d)))
        plan.append((live[0], live[1:]))
        kept.add(live[0])
        removed.update(live[1:])
    return plan, removed


# ---------- duplicate files ----------

def find_duplicate_files(sizes, match, fast):
    if match == "name":
        candidates = group_by(sizes, lambda p: (sizes[p], normalized_name(p)))
    elif match == "exact-name":
        candidates = group_by(sizes, lambda p: (sizes[p], p.name))
    else:
        candidates = group_by(sizes, lambda p: sizes[p])

    if fast:
        return candidates

    n = sum(len(g) for g in candidates)
    if n:
        print(f"Hashing {n} candidate files...")
    result = []
    for group in candidates:
        for g2 in group_by(group, lambda p: hash_file(p, PARTIAL)):
            if sizes[g2[0]] <= PARTIAL:      # partial hash already covered whole file
                result.append(g2)
            else:
                result.extend(group_by(g2, hash_file))
    return result


# ---------- choosing what to keep ----------

def keeper_key(p, strategy, prefer, nested=False):
    in_preferred = 0 if (prefer and (p == prefer or prefer in p.parents)) else 1
    try:
        c = created(p)
    except OSError:
        c = float("inf")
    if strategy == "oldest":
        rank = (c,)
    elif strategy == "newest":
        rank = (-c,)
    else:  # auto: avoid "copy"/"(1)" names, prefer shorter names, then oldest
        rank = (has_copy_suffix(p), len(p.name), c)
    return (in_preferred, nested, *rank, len(p.parts), str(p))


# ---------- removal ----------

# Reads the list of paths from a file, so any number of items is trashed in
# one Finder operation (no command-line length limit, one Trash sound).
TRASH_SCRIPT = """on run argv
    set listText to read (POSIX file (item 1 of argv)) as «class utf8»
    set theItems to {}
    repeat with p in paragraphs of listText
        set p to contents of p
        if length of p > 0 then set end of theItems to (POSIX file p) as alias
    end repeat
    with timeout of 3600 seconds
        tell application "Finder" to delete theItems
    end timeout
end run"""


def move_to_trash_fallback(p):
    trash = Path.home() / ".Trash"
    dest, i = trash / p.name, 1
    while dest.exists():
        dest = trash / f"{p.stem} {i}{p.suffix}"
        i += 1
    shutil.move(str(p), str(dest))


def remove(paths, permanent):
    removed, failed = [], []
    present = [p for p in paths if p.exists() or p.is_symlink()]
    removed.extend(p for p in paths if p not in set(present))   # already gone

    if permanent:
        for p in present:
            try:
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                removed.append(p)
            except OSError as e:
                failed.append((p, e))
        return removed, failed

    # one Finder operation for everything
    listable = [p for p in present if "\n" not in str(p)]
    print(f"Moving {len(present)} items to the Trash in one operation...")
    ok = False
    if listable:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt",
                                         delete=False) as f:
            f.write("\n".join(map(str, listable)))
            listfile = f.name
        try:
            r = subprocess.run(["osascript", "-e", TRASH_SCRIPT, listfile],
                               capture_output=True, text=True)
            ok = r.returncode == 0
            if not ok:
                warn(f"Finder couldn't trash the items ({r.stderr.strip()}); "
                     f"moving them to ~/.Trash directly instead")
        except OSError:
            pass                               # osascript not available
        finally:
            os.unlink(listfile)

    leftovers = [p for p in present if not ok or p not in set(listable)]
    if ok:
        removed.extend(p for p in present if p in set(listable))
    for p in leftovers:
        if not (p.exists() or p.is_symlink()):
            removed.append(p)
            continue
        try:
            move_to_trash_fallback(p)
            removed.append(p)
        except OSError as e:
            failed.append((p, e))
    return removed, failed


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(
        description="Find and remove duplicate folders and files (dry run unless --delete).")
    ap.add_argument("folder", help="folder to scan")
    ap.add_argument("--match", choices=["content", "name", "exact-name"], default="content",
                    help="how FILES are matched. content: identical bytes, any name (default); "
                         "name: also similar names ('x.jpg' = 'x copy.jpg' = 'x (1).jpg'); "
                         "exact-name: also identical names")
    ap.add_argument("--fast", action="store_true",
                    help="trust names+sizes and skip content hashing (risky); "
                         "for files requires --match name/exact-name")
    ap.add_argument("--no-folders", action="store_true", help="don't look for duplicate folders")
    ap.add_argument("--folders-only", action="store_true",
                    help="only look for duplicate folders, not individual files")
    ap.add_argument("--no-empty", action="store_true",
                    help="don't look for empty placeholder files")
    ap.add_argument("--empty-orphans", action="store_true",
                    help="also remove placeholders that have no real original next to them")
    ap.add_argument("--keep", choices=["auto", "oldest", "newest"], default="auto",
                    help="which copy to keep (default: auto)")
    ap.add_argument("--prefer", metavar="DIR",
                    help="always keep the copy inside this folder when there is one")
    ap.add_argument("--no-recursive", action="store_true",
                    help="don't scan subfolders (disables folder comparison)")
    ap.add_argument("--include-hidden", action="store_true", help="include dotfiles/folders")
    ap.add_argument("--min-size", type=int, default=1, metavar="BYTES",
                    help="ignore FILES smaller than this (default 1 = skip empty files)")
    ap.add_argument("--delete", action="store_true", help="actually remove duplicates")
    ap.add_argument("--permanent", action="store_true",
                    help="delete permanently instead of moving to Trash")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--log", metavar="CSV", help="write a CSV report of all groups")
    args = ap.parse_args()

    root = Path(args.folder).expanduser().resolve()
    if not root.is_dir():
        ap.error(f"not a folder: {root}")
    if args.fast and args.match == "content" and not args.folders_only:
        ap.error("--fast requires --match name or exact-name (or --folders-only)")
    if args.no_folders and args.folders_only:
        ap.error("--no-folders and --folders-only can't be combined")
    if args.folders_only and args.no_recursive:
        ap.error("--folders-only needs subfolders; drop --no-recursive")
    prefer = Path(args.prefer).expanduser().resolve() if args.prefer else None

    print(f"Scanning {root} ...")
    sizes, tree = scan(root, args)
    print(f"Found {len(sizes)} files in {len(tree)} folders.")

    # Step 0: empty placeholders
    empty_plan, empty_orphans = [], []
    if not args.no_empty and not args.folders_only:
        print("Looking for empty placeholders...")
        found = find_placeholders(tree, sizes)
        # items inside a placeholder folder are covered by that folder
        ph_dirs = {p for p, _, _ in found if p not in sizes}
        found = [e for e in found if not any(a in ph_dirs for a in e[0].parents)]
        for entry in found:
            (empty_plan if entry[1] or args.empty_orphans else empty_orphans).append(entry)
        # placeholders being removed shouldn't affect the folder/file comparison
        drop = {p for p, _, _ in empty_plan}
        drop_dirs = {p for p in drop if p not in sizes}
        for k in [k for k in tree if inside(k, drop_dirs)]:
            del tree[k]
        for node in tree.values():
            node["files"] = [p for p in node["files"] if p not in drop]
            node["dirs"] = [c for c in node["dirs"] if c not in drop]
            node["bundles"] = [c for c in node["bundles"] if c not in drop]
        sizes = {p: s for p, s in sizes.items()
                 if p not in drop and not inside(p.parent, drop_dirs)}

    # Step 1: duplicate folders
    folder_plan, removed_dirs, nfiles, nbytes = [], set(), {}, {}
    if not args.no_folders and not args.no_recursive:
        print("Comparing folders...")
        groups, nfiles, nbytes = find_duplicate_folders(root, tree, sizes, args.fast)
        folder_plan, removed_dirs = plan_folder_removals(groups, args.keep, prefer)

    # placeholders inside folders that are removed anyway need no separate entry
    empty_plan = [e for e in empty_plan if not inside(e[0].parent, removed_dirs)]
    empty_orphans = [e for e in empty_orphans if not inside(e[0].parent, removed_dirs)]

    # Step 2: duplicate files (outside folders already being removed)
    file_plan = []
    if not args.folders_only:
        print("Comparing files...")
        eligible = {p: s for p, s in sizes.items()
                    if s >= args.min_size and not inside(p.parent, removed_dirs)}
        for g in find_duplicate_files(eligible, args.match, args.fast):
            g.sort(key=lambda p: keeper_key(p, args.keep, prefer))
            file_plan.append((g[0], g[1:]))
        file_plan.sort(key=lambda kd: str(kd[0]))

    if not (folder_plan or file_plan or empty_plan or empty_orphans):
        print("No duplicates found.")
        return

    def rel(p):
        try:
            return str(p.relative_to(root))
        except ValueError:
            return str(p)

    folders_to_remove, files_to_remove = [], []
    folder_bytes = file_bytes = 0

    if empty_plan or empty_orphans:
        print("\n=== Empty placeholder files (0 bytes) ===")
        def show(p):
            return rel(p) + ("/" if p.is_dir() else "")
        for p, orig, why in empty_plan:
            note = f"real copy: {orig.name}" if orig else "no real copy found"
            print(f"   REMOVE  {show(p)}   ({why}; {note})")
        for p, orig, why in empty_orphans:
            print(f"   KEEP    {show(p)}   ({why}; no real copy found here - it may never "
                  f"have finished copying; --empty-orphans removes it)")

    if folder_plan:
        print("\n=== Duplicate folders ===")
        for keeper, dups in folder_plan:
            print(f"[{nfiles[keeper]} files, {human(nbytes[keeper])}]")
            print(f"   KEEP    {rel(keeper)}/")
            for d in dups:
                print(f"   REMOVE  {rel(d)}/")
            folders_to_remove.extend(dups)
            folder_bytes += nbytes[keeper] * len(dups)

    if file_plan:
        print("\n=== Duplicate files ===")
        for keeper, dups in file_plan:
            print(f"[{human(sizes[keeper])}]")
            print(f"   KEEP    {rel(keeper)}")
            for d in dups:
                print(f"   REMOVE  {rel(d)}")
            files_to_remove.extend(dups)
            file_bytes += sizes[keeper] * len(dups)

    if args.log:
        with open(Path(args.log).expanduser(), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["kind", "group", "action", "size_bytes", "path"])
            for p, orig, busy in empty_plan:
                w.writerow(["empty", "", "remove", 0, p])
            for p, orig, busy in empty_orphans:
                w.writerow(["empty", "", "keep (no original)", 0, p])
            for i, (keeper, dups) in enumerate(folder_plan, 1):
                w.writerow(["folder", i, "keep", nbytes[keeper], keeper])
                for d in dups:
                    w.writerow(["folder", i, "remove", nbytes[d], d])
            for i, (keeper, dups) in enumerate(file_plan, 1):
                w.writerow(["file", i, "keep", sizes[keeper], keeper])
                for d in dups:
                    w.writerow(["file", i, "remove", sizes[d], d])
        print(f"\nReport written to {args.log}")

    print()
    if empty_plan:
        print(f"Empty:   {len(empty_plan)} placeholders to remove.")
    if empty_orphans:
        print(f"Warning: {len(empty_orphans)} empty items have no real copy next to them "
              f"(listed as KEEP above).")
    if folder_plan:
        print(f"Folders: {len(folder_plan)} groups, {len(folders_to_remove)} folders to remove "
              f"({human(folder_bytes)}).")
    if file_plan:
        print(f"Files:   {len(file_plan)} groups, {len(files_to_remove)} files to remove "
              f"({human(file_bytes)}).")
    print(f"Total to reclaim: {human(folder_bytes + file_bytes)}")

    if not args.delete:
        print("Dry run - nothing was changed. Re-run with --delete to remove them.")
        return

    empties_to_remove = [p for p, _, _ in empty_plan]
    to_remove = folders_to_remove + files_to_remove + empties_to_remove
    if not to_remove:
        print("Nothing to remove.")
        return
    where = "PERMANENTLY DELETE" if args.permanent else "move to Trash"
    if not args.yes:
        answer = input(f"\n{where} {len(folders_to_remove)} folders and "
                       f"{len(files_to_remove)} files and {len(empties_to_remove)} empty placeholders? "
                       f"Type 'yes' to continue: ")
        if answer.strip().lower() != "yes":
            print("Cancelled.")
            return

    removed, failed = remove(to_remove, args.permanent)
    print(f"\nDone: {len(removed)} items removed.")
    for p, e in failed:
        warn(f"could not remove {p}: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted.")
