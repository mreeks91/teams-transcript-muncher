# teams-transcript

Extract the full transcript from a Microsoft Teams meeting recap — even when your org has disabled the native download button.

The transcript is already visible in the browser DOM; this tool scrolls through the virtual list programmatically and saves everything to a text file, bypassing the Teams-level policy entirely.

## How it works

Teams only renders the visible portion of the transcript at any time (virtual scrolling). The tool opens the recap page in Edge, scrolls through the transcript panel step by step, collects entries as they appear, deduplicates them, and writes the sorted result to a file.

A standalone JavaScript snippet (`devtools_snippet.js`) is also included as a zero-install alternative.

---

## Setup

Requires Python 3.11+ and Microsoft Edge.

```powershell
cd C:\path\to\teams-transcript
pip install -e .
```

No additional browser download is needed — the tool drives your already-installed Edge.

**One-time sign-in** (saves your Teams session so you don't have to log in each time):

```powershell
teams-transcript --login
```

An Edge window will open. Sign in to Microsoft Teams, complete MFA if prompted, then press Enter in the terminal.

---

## Usage

```powershell
# Extract to a file
teams-transcript "https://teams.microsoft.com/..." -o transcript.txt

# Print to stdout (pipe-friendly)
teams-transcript "https://teams.microsoft.com/..."

# Group consecutive lines from the same speaker into blocks
teams-transcript "https://teams.microsoft.com/..." --group-by-speaker -o transcript.txt
```

Output format (default):
```
[00:01:23] Alice Johnson: First sentence of the transcript.
[00:02:45] Bob Smith: Response here.
```

Output format (`--group-by-speaker`):
```
[00:01:23] Alice Johnson:
  First sentence.
  Continuation from the same speaker.

[00:02:45] Bob Smith:
  Response here.
```

### All options

```
teams-transcript [url] [options]

  url                        Teams recap page URL
  -o, --output PATH          Write to file instead of stdout
  --login                    One-time sign-in, then exit
  --profile-dir PATH         Browser profile location
                             (default: ~/.teams-transcript/playwright-profile)
  --use-edge-profile         Use your live Edge profile instead
                             (Edge must be fully closed first)
  --browser {edge,chrome,chromium}
                             Browser to use (default: edge)
  --group-by-speaker         Merge consecutive lines per speaker into blocks
  --debug                    Print selector probes and per-step entry counts;
                             save scroll screenshots to current directory
  --scroll-pause MS          Pause between scroll steps (default: 700ms)
  --timeout SECS             Page load timeout (default: 60s)
```

---

## DevTools snippet (zero-install alternative)

If you don't want to install Python, paste `devtools_snippet.js` into the browser console:

1. Open the Teams meeting recap in your browser and navigate to the **Transcript** tab.
2. Press **F12** to open DevTools and click the **Console** tab.
3. Paste the contents of `devtools_snippet.js` and press **Enter**.
4. `teams-transcript.txt` downloads automatically.

This uses a browser-native blob download, which is not subject to the Teams org policy that blocks the native download button.

---

## Troubleshooting

### "Could not find the transcript panel"

Teams updates its DOM periodically and the selector values can change. Run with `--debug` to see which selectors were tried:

```powershell
teams-transcript --debug "https://teams.microsoft.com/..."
```

The output will show which `data-tid` selectors matched. Open DevTools on the recap page, inspect the transcript panel, find the current attribute values, and add them to the front of the relevant list in `src/teams_transcript/selectors.py`.

### Transcript tab isn't visible at the URL

Some recap URLs land on an overview page rather than the transcript tab directly. The tool will attempt to click into the transcript tab automatically. If that fails, navigate to the Transcript tab manually in the browser window that opens, then re-run.

### Edge won't open / profile errors

If Edge is already running when you use `--use-edge-profile`, the profile will be locked. Either close Edge first, or use the default dedicated profile (omit `--use-edge-profile`) — it's always free regardless of whether Edge is open.

### Org Conditional Access policies

The tool uses `channel="msedge"`, which drives your real installed Edge binary. Conditional Access policies that check the browser binary or user-agent will see genuine Edge and should pass through normally.
