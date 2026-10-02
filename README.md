# file-delete

A macOS command-line script that finds and removes duplicate files, duplicate
folders, and the empty "Zero bytes" leftovers that an interrupted copy,
Migration Assistant run or iCloud sync leaves behind (the greyed-out
`photo 6.jpeg`, `Dentsu 6`, `….torrent 6` items in Finder).

It is safe by default: it only reports until you add `--delete`, removed items
go to the Trash in a single Finder operation (so **Put Back** works), and it
asks for confirmation first.

## Requirements

- macOS with `python3` (if it's missing, macOS offers to install the Command
  Line Tools the first time you run `python3`)
- No third-party packages

## Usage

```bash
python3 dedupe.py ~/Desktop                    # preview only, nothing changes
python3 dedupe.py ~/Desktop --delete           # move everything listed to the Trash
python3 dedupe.py ~/Desktop --log report.csv   # also save the report as CSV
```

The report has three sections:

1. **Empty placeholder files** – 0-byte leftovers of an unfinished copy.
2. **Duplicate folders** – folders with identical contents (same files, same
   names, same layout); only the top-most duplicate is listed.
3. **Duplicate files** – files with identical contents (checked with SHA-256).

## What counts as an empty placeholder

- a 0-byte file whose name has a copy number (`x 6.pdf`, `x (1).pdf`,
  `x.torrent 6`), that Finder marked as an unfinished copy, or whose type can
  never be empty (PDF, JPEG, MP4, DOCX, …)
- a folder or app with a copy number that contains no data at all

A placeholder is removed when a real (non-empty) version with the same name is
in the same folder. Otherwise it's listed as **KEEP** with a warning, because
the real item may never have finished copying; add `--empty-orphans` to
remove those too. Ordinary empty files (`notes.txt`, `__init__.py`) are never
touched.

## Options

| Option | Effect |
|---|---|
| `--delete` | Actually remove what's listed (asks to confirm) |
| `-y`, `--yes` | Skip the confirmation |
| `--permanent` | Delete permanently instead of moving to the Trash |
| `--empty-orphans` | Also remove empty placeholders that have no real copy |
| `--no-empty` | Skip the empty-placeholder step |
| `--no-folders` | Only compare individual files |
| `--folders-only` | Only look for whole duplicate folders |
| `--match content\|name\|exact-name` | How files are matched (default: content, any name) |
| `--fast` | Trust name + size, skip hashing (needs `--match name`/`exact-name`) |
| `--keep auto\|oldest\|newest` | Which copy to keep (default: avoids "copy"/"6" names) |
| `--prefer DIR` | Always keep the copy inside this folder when there is one |
| `--no-recursive` | Don't scan subfolders |
| `--include-hidden` | Include dotfiles and hidden folders |
| `--min-size BYTES` | Ignore files smaller than this in the duplicate check |
| `--log FILE.csv` | Write a CSV report |

## Safety notes

- Hidden items, symlinks and app/library packages (`.app`, `.photoslibrary`,
  …) are never deduplicated internally; a folder containing anything the
  script can't see is never removed as a whole.
- Don't run with `--delete` while a copy, migration or iCloud sync is still in
  progress: unfinished files look exactly like placeholders until they're done.
- The first time it trashes items, macOS may ask whether Terminal can control
  Finder; allow it so Put Back works. For Desktop/Documents/Downloads you may
  need to give Terminal Full Disk Access (System Settings → Privacy & Security).
