# SAGE

SAGE (Storage Analysis and Guidance Engine) is a Windows-only NTFS storage
prototype. It renders the MFT hierarchy first, then gathers logical and
allocated file sizes in a separate background phase. The right pane uses a
local model (`qwen3.5:4b`) for conversational, scan-grounded answers. No API
key or cloud service is required.

The prominent bar beneath the scan controls shows estimated MFT progress,
tree-building activity, then exact file-size progress. Folder and file icons distinguish items in the tree.
The size columns update as evidence arrives; a `+` suffix means the displayed
folder total is a lower bound until the size pass finishes.

## Requirements

- Windows 10 or 11
- Python 3.11 or newer
- An NTFS fixed drive
- Administrator approval for raw volume access

The runtime and automated tests use only the Python standard library.
Conversational answers additionally need Ollama running locally with
`qwen3.5:4b` downloaded. Scanning and basic evidence answers still work if
Ollama is unavailable.

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

Greetings and simple count/total-size questions answer immediately from local
logic. Other questions first resolve mentioned folder/file names in the current
scan, then send only relevant evidence and recent conversation to one model
request. A folder-name index and cached largest-item rankings avoid repeated
work. Duplicate names and partial matches trigger an immediate numbered
clarification before an assessment. Reply with the number or full path.
Only a resolved conversational target is remembered; tree highlighting and
navigation never supply assistant context. Explicit drive/folder scopes replace
earlier targets, and unresolved paths invalidate an old follow-up target.
Retrieval uses the existing in-memory scan, not another MFT pass or file reads.
The model receives no file-operation tools or file contents.

Replies stream into the assistant pane as text arrives. Basic Markdown bold,
italics, headings, bullets, and inline code render with Tk text styles. A spinner
shows actual retrieval/generation stages and elapsed time. Scanning again
invalidates old assistant events.

The model uses a 2,048-token context and a 240-token response cap to reduce
latency and memory demand. Conversation history is bounded. Larger explanations
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

Deletion/relocation assessments and scoped cleanup recommendations run in local
engine logic, without waiting for the language model. SAGE distinguishes
protected containers, application-managed cleanup, conditional personal-data
candidates, and missing evidence. Personal data is never declared disposable
merely because it does not belong to Windows. Unknown items get a specific
question about their owner or purpose, not a blanket manual-review response.
Cleanup candidates are ranked by measured allocation within the requested scope;
parent/child candidates are not double-counted. No candidate found under these
rules is not proof that an entire drive has nothing removable. Unreadable sizes
do not imply a cleanup method.

The small built-in policy recognizes conventional pip cache and user Temp paths.
For pip, verify `python -m pip cache dir` matches the assessed path before using
`python -m pip cache purge`; SAGE never runs purge automatically. See
[pip caching guidance](https://pip.pypa.io/en/stable/topics/caching/) and
[pip cache](https://pip.pypa.io/en/stable/cli/pip_cache/).
Path rules are heuristics, not verified installation ownership or dependency
analysis. Focused RAG and content inspection are not implemented in this release.
The model receives metadata only; explanations can still be inaccurate.
Tree file actions remain separate from AI and require direct user interaction.
Model replies can contain errors, including incorrect size conversions. Check
the measured sizes in the tree before acting on a cleanup suggestion.

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

- `platform/windows/mft.py` performs native `FSCTL_ENUM_USN_DATA` enumeration.
- `platform/windows/file_operations.py` handles Windows Shell file operations.
- `services/hierarchy.py` reconstructs parents and aggregates counts in memory.
- `platform/windows/sizes.py` batches file size and allocation metadata by
  directory, with exact per-file queries only for entries that could not be batched.
- `services/evidence.py` produces deterministic fallback answers.
- `services/retrieval.py` retrieves bounded scan evidence and cleanup-risk labels.
- `services/local_ai.py` handles fast replies, relevant evidence, bounded history,
  and streaming from the local model.
- `ui/markdown.py` converts assistant formatting into native Tk text styles.
- `ui/` contains the two-pane Tkinter interface and background-job coordinator.

The scan coordinator assigns every scan a generation number and cancels older
workers. UI updates travel through a queue owned by the Tk main thread, so stale
results from a previous drive cannot replace a newer scan.

## Working with files

Right-click a file or folder in the left tree. SAGE can open it, reveal it in
Explorer, copy its path, copy or cut and paste within the tree, copy or move it
to a chosen folder, rename it, or send it to the Recycle Bin. Keyboard shortcuts
include Ctrl+C, Ctrl+X, Ctrl+V, F2, and Delete. File changes trigger a fresh
scan so the displayed evidence stays current. The drive root cannot be moved,
renamed, or recycled from the tree. Normal navigation has no selection highlight;
use the context menu to explicitly choose an action target. Selection keyboard
shortcuts require an explicitly chosen target; ordinary navigation does not select one.
The context menu also offers an explicit deletion/relocation assessment and
Windows Installed apps / Storage settings links (these open settings, not an
application-specific uninstaller). See [Windows Settings URI documentation](https://learn.microsoft.com/en-us/windows/apps/develop/launch/launch-settings).

Move, rename and recycle show the affected paths and consequences, then recheck
live existence and ancestor attributes before invoking the Windows Shell.
Protected/managed-cleanup paths, system-marked items and reparse-point ancestors
are blocked. These preflight checks do not atomically lock filesystem identity
across a Shell operation; a filesystem replacement race remains a prototype
limitation. Directory contents are not recursively audited for dependencies.
Recycle Bin contents still occupy drive space until permanently removed. The
Shell may not support recycling an item; SAGE keeps Windows' warning enabled.

## Prototype boundaries

- The hierarchy scan is MFT-backed and never falls back to `os.walk()`.
- Size collection queries many files at once through Windows directory metadata,
  with four concurrent workers and a bounded task queue. Inaccessible or missing
  entries use per-file fallback. Very large volumes can still take time; this is
  not a direct bulk `$MFT` size parser. Partial folder sizes are marked until completion.
- Folder allocation totals sum contained files. NTFS directory metadata overhead
  is not included.
- Active MFT records are analyzed. Deleted-record forensics, alternate data
  streams, hard-link name expansion, SQLite persistence, duplicate detection,
  automated deletion workflows, and file-content indexing are out of scope.
- Live filesystems can change during a scan, so a small number of unreadable
  paths is expected and reported rather than treated as a fatal error.
