# SAGE

SAGE (Storage Analysis and Guidance Engine) is a Windows-only NTFS storage
prototype. It reads `$MFT` sequentially from the volume (the same approach as
WizTree): names, parents, and logical/allocated sizes come from one pass, so
the tree is usable with sizes as soon as the MFT scan finishes. After that
scan, the assistant lists specific, rule-matched items you can delete on that
drive, each with a confirmation button in the chat. The right pane also uses a
local model (`qwen3.5:4b`) for other scan-grounded questions. No API key or
cloud service is required.

The prominent bar beneath the scan controls shows estimated `$MFT` read
progress, then tree-building activity. Folder and file icons distinguish items
in the tree. Size columns are filled from the MFT records; a directory-walk
size pass runs only if a record had no `$DATA` size.

## Requirements

- Windows 10 or 11
- Python 3.11 or newer
- An NTFS fixed drive
- Administrator approval for raw volume access

The runtime and automated tests use only the Python standard library.
Conversational answers additionally need Ollama running locally with
`qwen3.5:4b` downloaded. Scanning and basic evidence answers still work if
Ollama is unavailable.

## Cleanup suggestions and permanent delete

When a scan finishes, the assistant lists specific items that are safe to
delete on that drive, with exact paths and sizes, largest first. Only items
over 20 MB that match known rules are listed: Windows Update and Delivery
Optimization caches, Windows and user temp folders, crash dumps and error
reports, browser and Electron app caches (Chrome, Edge, Brave, Firefox,
Discord, Teams, VS Code, …), GPU shader caches, pip/npm/Yarn/NuGet/Gradle/Go
caches, project `node_modules` folders (only beside a `package.json`, never
inside installed apps, `AppData`, or `resources` folders), installers (`.exe`, `.msi`, `.msix`, `.iso`) in
Downloads, the Recycle Bin, and `Windows.old` upgrade leftovers. Documents,
photos, Program Files, and generic AppData folders are not listed. Nested
matches are collapsed so the same space is never counted twice.

Each item has a button directly in the chat. Pressing it asks for confirmation,
then deletes **permanently** (no Recycle Bin). Cache folders keep the folder and
delete its contents; files that are in use are skipped and reported. The
Recycle Bin item empties the bin through Windows. The drive is rescanned
afterward. Delete buttons only ever come from these rules; a model reply can
add a button only for a path that is already in the verified list.

Questions such as "which files are safe to delete" or "free up space" are
answered from these rules immediately, without the model. Other questions go to
the model together with the verified list, and it is instructed to cite full
paths and sizes instead of generic advice. Asking about a verified item, even
one under `C:\Windows`, shows its delete button under the reply.

When a drive or a named folder has no rule-matched item, SAGE answers locally
with the largest files and recognized caches there as review candidates (no
delete buttons), or with the folder's largest items and the right removal route
(for example, uninstalling apps through Installed apps), and says plainly that
finding no candidate does not mean nothing can be removed.

SAGE marks Windows and application locations as protected for cleanup advice,
except the specific caches in the verified cleanup list. Unknown paths are
review candidates, not confirmed safe deletions. Every deletion requires the
user to press a button and confirm.

## Local AI

Install Ollama separately and download the model with `ollama pull qwen3.5:4b`.
SAGE connects only to `127.0.0.1:11434`; it does not send scan data to a cloud
API. Questions run in a background worker so the tree remains responsive.
The Ollama application/server must be running; SAGE does not start it.
You do not need a separate `ollama run` chat session. SAGE allows up to 180
seconds per model request and requests that the model stay loaded for 10 minutes
between questions. SAGE starts loading the model in a background worker when
the app opens, so loading can overlap scanning. Cold loading and partial CPU
execution can still take time.

Greetings, count/total-size questions, and drive-wide cleanup questions answer
immediately from local logic. A cleanup question that names a folder or path
(for example "what can I delete in Program Files (x86)?") is scoped to that
folder instead of repeating the drive-wide list.

Every model request includes a compact summary of the whole scanned drive:
used space, top-level folders by size, the largest files, and the biggest
leaf folders. Named folders also get their largest direct children and any
verified cleanup items inside them, so answers can cite exact subfolders and
sizes.

Select text anywhere in the assistant pane to ask about it, like quoting in a
chat app. An "Asking about" bar appears above the input; the selected text is
sent with the next question (or cleared with **Clear**), and any paths in it are
resolved against the scan.

Other questions first resolve mentioned folder/file names in the current scan,
then send only relevant evidence and recent conversation to one model request. A folder-name index and cached largest-item rankings avoid repeated
work. Duplicate names are supplied as multiple paths for clarification.
Retrieval uses the existing in-memory scan, not another MFT pass or file reads.
The model receives no file-operation tools or file contents.

### In-depth item analysis

When a question is about one file or folder ("should I delete this?"), SAGE
builds a profile of that item and gives it to the model, so the answer cites
facts instead of generic advice:

- From the scan: size, last-modified date, attributes (hidden, system, link,
  cloud/online-only), file and folder counts, which file types fill it, and how
  much of it has not changed for 30 days or a year.
- From Windows, read on demand for that item only: the installed app that owns
  it (publisher, version, install date from the uninstall registry), the
  version information of its main program, its creation date, and where a
  downloaded file came from (its `Zone.Identifier`).
- From a built-in knowledge list of about 60 well-known locations (Steam
  libraries and caches, browser profiles, WinSxS, Windows.old, `node_modules`,
  WSL and Docker disks, and so on): what the location is and the supported way
  to remove or shrink it.

When several items share a name, for example "should I delete steam?", SAGE
lists each match with its size and role (for example "1.5 GB, part of Steam"
or "30.0 MB, Steam's built-in web browser cache") and asks which one you mean.
Protected items get a local reply built from the same profile: who owns the
item, what fills it, how stale it is, and the supported removal route.

No database is used; profiles are built from the in-memory scan and cached
until the next scan.

Replies stream into the assistant pane as text arrives. Basic Markdown bold,
italics, headings, bullets, and inline code render with Tk text styles. A spinner
shows actual retrieval/generation stages and elapsed time. Scanning again
invalidates old assistant events.

The model uses a 4,096-token context (to fit the drive summary) and a 320-token
response cap. Conversation history is bounded. Larger explanations
can require a follow-up; instant model answers are not guaranteed on CPU or a
GPU that cannot hold the whole model.

Run the read-only smoke evaluation on a synthetic scan with:

```powershell
$env:PYTHONPATH = "src"
python scripts/evaluate_local_ai.py
```

Measure retrieval separately from the model with synthetic data:

```powershell
$env:PYTHONPATH = "src"
python scripts/benchmark_local_ai.py --records 600000
python scripts/benchmark_local_ai.py --records 600000 --live
```

The live benchmark reports model loading, first text, and completion times.
It changes no files on the scanned drive.

The model cannot know every application dependency. Model replies can contain
errors, including incorrect size conversions. Check the measured sizes in the
tree before acting on a cleanup suggestion.

## Run from source

The quickest route needs no installation:

```powershell
python run_sage.py
```

To install the optional `sage` command instead:

```powershell
python -m pip install -e .
sage
```

SAGE requests UAC elevation on launch. A cancelled UAC prompt ends the launch.

## Validate

```powershell
python -m compileall -q src tests
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

## Architecture

- `platform/windows/mft.py` reads `$MFT` from the raw volume, applies the
  NTFS update-sequence fixup, and parses FILE records for names, parents,
  unnamed `$DATA` sizes, modified time, and attributes. Only junctions and
  symlinks keep the link attribute; OneDrive placeholders and compressed files
  do not.
- `platform/windows/file_operations.py` handles Windows Shell copy/move/rename
  and Recycle Bin actions, plus permanent delete and emptying the Recycle Bin
  from chat cleanup buttons.
- `services/hierarchy.py` reconstructs parents and aggregates counts and folder
  totals in memory.
- `platform/windows/sizes.py` is the fallback directory-walk size collector,
  used only when MFT records did not already supply sizes.
- `services/cleanup.py` holds the cleanup rules and builds the specific
  safe-to-delete list from the finished scan.
- `services/evidence.py` produces deterministic fallback answers.
- `services/retrieval.py` retrieves bounded scan evidence and cleanup-risk labels.
- `services/profile.py` builds the per-item profile: scan metadata, owning
  installed app, program version information, and download source.
- `services/known_folders.py` is the knowledge list of well-known Windows
  locations and their supported cleanup routes.
- `services/local_ai.py` handles fast replies, cleanup-intent answers, relevant
  evidence, bounded history, and streaming from the local model.
- `ui/assistant_panel.py` renders chat replies and embeds a delete button per
  verified cleanup item.
- `ui/markdown.py` converts assistant formatting into native Tk text styles.
- `ui/` contains the two-pane Tkinter interface and background-job coordinator.

The scan coordinator assigns every scan a generation number and cancels older
workers. UI updates travel through a queue owned by the Tk main thread, so stale
results from a previous drive cannot replace a newer scan.

## Working with files

Right-click a file or folder in the left tree. SAGE can open it, reveal it in
Explorer, copy its path, copy or cut and paste within the tree, copy or move it
to a chosen folder, rename it, or send it to the Recycle Bin. Keyboard shortcuts
include Ctrl+C, Ctrl+X, Ctrl+V, F2, and Delete. Verified cleanup items in the
assistant pane can be deleted permanently after confirmation. File changes
trigger a fresh scan so the displayed evidence stays current. The drive root
cannot be moved, renamed, or recycled from the tree. Recycle and permanent
delete actions require confirmation.

## Prototype boundaries

- The hierarchy scan is MFT-backed and never falls back to `os.walk()`.
- File sizes are taken from unnamed `$DATA` on each in-use MFT record during
  the sequential `$MFT` read. Folder totals are summed in memory afterward.
  A Windows directory metadata walk remains only as fallback for records that
  had no MFT size. Scanning is I/O-bound on `$MFT` (typically hundreds of MB),
  not on opening every folder on the volume. Python is still slower than a
  native C++ tool such as WizTree, but it no longer scales with directory-walk
  syscalls. `$MFT` mapping pairs are read from record 0; volumes that store
  those runs only in `$ATTRIBUTE_LIST` are not handled.
- Folder allocation totals sum contained files. NTFS directory metadata overhead
  is not included.
- Active MFT records are analyzed. Deleted-record forensics, alternate data
  streams, hard-link name expansion, SQLite persistence, duplicate detection,
  deletion without user confirmation, and file-content indexing are out of scope.
- Live filesystems can change during a scan, so a small number of unreadable
  paths is expected and reported rather than treated as a fatal error.
