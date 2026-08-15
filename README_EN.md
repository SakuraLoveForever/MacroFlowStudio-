<div align="center">

[简体中文](./README.md) | **English**

# 🖱️ MacroFlow Studio

**Keyboard & Mouse Macro Recording and Automation Tool**

Record or edit keyboard/mouse actions into scripts, use **image matching / text recognition (OCR)** as decision conditions, and organize multiple scripts into **workflows** that run automatically for a set number of times.

![Version](https://img.shields.io/badge/Version-v1.0.0-4B8BBE?style=flat-square)
![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078D6?style=flat-square)
![Language](https://img.shields.io/badge/Language-zh--CN-EA4335?style=flat-square)
![OCR](https://img.shields.io/badge/OCR-PaddleOCR%20Offline-FF6F00?style=flat-square)
![Build](https://img.shields.io/badge/Build-PyInstaller%206.20-8A2BE2?style=flat-square)

</div>

---

## ✨ Highlights

| | |
|---|---|
| 🎮 **Smart track recording** | Records raw relative mouse movement while the cursor is locked at the window center (first-person view turning, sampled at ≤16 ms); automatically falls back to desktop coordinates when the cursor leaves the window |
| 🧩 **Rich action set** | 14+ action types: keyboard / mouse / wheel / image match / OCR / jump / script reference / app launch & close / special modules |
| 🔍 **Image match + OCR dual engines** | Template matching for fixed patterns; offline PaddleOCR reads changing text (accurate Chinese & digits) |
| 🗂️ **Module object repository** | Reusable recognition settings: switch / global / special modules, code segments, fallback recognition, number reading |
| ⚙️ **Workflow orchestration** | Run multiple scripts by repeat count, global modules keep scanning, breakpoint resume, test mode |
| 🛡️ **Focus lock mode** | System-level input lock + automatic English input method; releases held keys on abnormal exit |

---

## 📦 Installation & Usage

1. Download the latest release: **[MacroFlowStudio_v1.0.0_win64.zip](https://github.com/SakuraLoveForever/MacroFlowStudio-/releases)** (~230 MB)
2. Extract — **copy the whole folder** (`paddle_ocr` must stay in the same directory as the exe)
3. Run `MacroFlowStudio.exe`; settings are stored in `app_settings.json` next to the exe

> ⚠️ `paddle_ocr` (offline OCR engine & models) is loaded on demand the first time you use "Recognize Text". **OCR will not work without it**; it contains all runtime dependencies of paddleocr — do not remove anything.

## 🚀 Quick Start

| Step | Action |
|---|---|
| 1️⃣ Bind window | If the target app is a game, click "Select Window" first (relative tracks are then treated as in-game view turning) |
| 2️⃣ Start recording | Press `F8`; the main window hides automatically during recording (configurable) |
| 3️⃣ Run script | Press `F9`; the target window is brought to the foreground automatically |
| 4️⃣ Emergency stop | Press `F12` at any time |

## 🎮 Recording & Playback

- **Smart track recording**: records raw relative movement while the cursor is locked at the window center (game view turning, sampled at ≤16 ms); desktop coordinates resume when the cursor leaves the target window (default interval 20 ms, configurable 10–500 ms)
- Automatically switches the target window to English (US) input method before recording; `F8` always starts a fresh recording detached from the currently open script, saved under a new non-conflicting name
- **Playback**: start from a specific line, repeat counts, breakpoint resume, multi-click, click at current cursor position

## 🧩 Script Editor

Insert the following actions on the Script Editor page; double-click an action to edit, hold Shift/Ctrl to multi-select:

| Action | Description |
|---|---|
| ⏱️ Delay | Wait a fixed number of milliseconds |
| ⌨️ Keyboard / key press | Press / release / tap keys; key can be auto-detected on insert |
| 📝 Text | Type text |
| 🖱️ Mouse move / button / click / multi-click | Pick coordinates on screen; left / right / middle button |
| 🎡 Wheel | Horizontal / vertical scroll at a given position |
| 🖼️ Image match | Template matching (see below) |
| 🔤 OCR text | Offline region text recognition (see below) |
| 💬 Floating notice / comment | On-screen reminder / note text |
| 🔗 Script reference | Load and run another script in place (embedded execution, no jump) |
| 🚀 Open / close app | Launch a program (with args) / gracefully or forcefully terminate a process |
| 🧭 Jump | Jump to a target action line, script start, or script end |
| 📌 Foreground window | Bring the bound window to the foreground before execution |
| ⭐ Special modules | Restart workflow / end the innermost script and continue / jump to the actual last line |
| 🧰 Module reference | Insert a saved module object (see "Module Objects") |

Editing: undo / redo, duplicate down (multi-select supported), run from here (trial run skipping earlier actions), clear undo history after save.

## 🔍 Image Matching & OCR

**Template matching** (for fixed patterns):

- **Region**: full screen / bound window / custom region (drag-select with the left button)
- **Threshold**: minimum similarity; enable "ignore background" when the template background changes color to match strokes only
- **Timeout**: wait timeout in ms + polling interval; on timeout: continue / jump to a line / stop
- **On match**: continue / click (region center or custom coordinates) / jump to a line / end the current script and move to the next workflow step
- **Secondary match**: optionally confirm with another template after a hit
- The target must be visible in real time; minimum polling interval 50 ms

**OCR text recognition** (reads changing text, complementary to image matching): powered by PaddleOCR (fully offline, accurate Chinese & digits)

- Drag-select a region (empty = full screen), or bind a target window
- Expected text matched by "contains / equals"; empty = any recognized text hits
- Polling with timeout, then continue / jump / stop; results shown in the status bar and the execution mini window

## 🗂️ Module Objects

The Module Object Manager stores recognition settings as reusable objects, categorized as **Switch / Workflow Global / Script Global / Special**.

- **Unique identity**: referenced by ID rather than image path; one image can back multiple modules without overwriting
- **Recognition settings**: template, region, threshold, hold duration, recognition mode (template image, text, read number, or no recognition)
- **Success action + post-action code segment**: the action executed on a hit, optionally followed by extra actions (jumps, foreground window, etc.)
- **Timeout code segment**: actions run when "continuous miss" exceeds the configured limit
- Repository: pinyin sorting, bulk add / bulk remove from scripts, double-click edit, undo removal

### 🔢 Reading Numbers Module

Create a Switch module, set the recognition mode to **Read Number**, and drag-select the region containing the digits. At runtime, OCR digit boxes are concatenated left-to-right into an integer (e.g. `0`, `0`, `7` → `7`).

When inserting the module into a script, configure the **compare number** and two result branches: **equal / not-equal-or-not-read**. Both branches can continue to the next line, jump to a stable line object, or end the innermost script; when no number is read, it retries per the module's blocking / timeout settings, and a non-blocking timeout falls to the failure branch.

## ⚙️ Workflows

The Workflow page organizes multiple scripts in order; each step supports:

| Setting | Description |
|---|---|
| 📄 Script | Double-click to change |
| 🔁 Repeat count | Remaining-count system (no decrement on failure or manual stop, shows "done" at 0); 0 or **unlimited** (once per round); optional **start from a specific line from the 2nd run** |
| ⏳ Wait before run | Milliseconds to wait before executing |
| ⏱️ Repeat interval | Gap between consecutive runs of the same script, default 1000 ms |
| ✅ Enable / disable | Disabled rows are greyed out and skipped without decrementing |
| 🌐 Global modules | Attach workflow-global modules to a step |

Other operations: select a row to **run from the selected line**; **bulk-set parameters** (counts / wait / interval); step deletion is undoable; rows whose script file is missing are highlighted red with a reason at runtime; **scheduled start** accepts a `YYYY-MM-DD HH:MM:SS` time (empty = run immediately; the app must keep running while waiting).

Workflows are plain files (JSON under `workflows/`): New / Open / Save. The UI shows the file content; saving overwrites it directly.

### 🌐 Global Detection

During workflow execution, enabled global modules keep scanning (sharing one screenshot per round, no repeated captures) and trigger when their condition is met:

- **Interrupt & resume**: on trigger, pauses the current script and saves a breakpoint, runs the module's success action, then resumes the original script
- **Click**: clicks the region center or custom coordinates; ensures the target window is in the foreground first
- **Restart workflow**: can restart the whole workflow on trigger

## 🛠️ Execution & Settings

- **Focus lock mode** (off by default): switches to the English input method + system-level mouse/keyboard lock (`BlockInput`) to block misoperation; `F12` remains the emergency stop
- **Foreground window**: independent toggle, brings the target window to the front before execution
- **Mini windows**: always-on-top mini window for execution / recording (independent toggles) showing progress and the action log
- **System tray**: closing to tray keeps it running; the tray menu restores the window or quits
- **Hotkey sounds**: distinct tones for record start/end, run, complete, and emergency stop; testable
- **Read current coordinates**: shows the live cursor position and the window under it for manual coordinate entry
- **Resolution scaling**: scripts store the recording machine's virtual screen size; coordinates are scaled automatically on another machine (legacy scripts assumed 1920×1080)

## 💾 Backup & Logs

- **Timed backup**: fixed intervals `1h` / `1 day` / `1 week`; scans the configured script directories into `backups/scripts/`; each script maps to one target file (no history copies)
- **Auto start on boot**: writes the current Windows user's startup entry; unchecking removes it
- **Start to tray / auto-run workflow at startup**: can be combined; after an auto workflow finishes it stays in the tray, double-click the tray icon to restore
- **Run logs**: written to `logs/YYYY-MM-DD/MacroFlow_<start time>_<pid>.log`, one file per launch; "Open log directory" on the log page

## 🗄️ File Layout

Created next to the exe on first launch:

```
MacroFlowStudio/
├── MacroFlowStudio.exe   # Main program (~70 MB)
├── paddle_ocr/           # OCR engine + models + all runtime dependencies (loaded on demand)
├── scripts/              # Scripts
├── workflows/            # Workflows
├── images/               # Recommended location for image-match templates
├── backups/scripts/      # Timed backups
├── logs/                 # Run logs (by date / session)
└── app_settings.json     # All sidebar settings and window binding
```

The "Open" button can load JSON scripts from other directories; put templates under `images` and use relative paths (e.g. `images\start_button.png`) in image-match actions for easy migration.

## 📸 Screenshots

<!-- Add screenshots here: main window, script editor, module object manager, workflow page, execution mini window -->
(To be added)

## 🏗️ Run from Source & Build

```powershell
python -m pip install -r requirements.txt
python app.py          # run from source
.\build.ps1            # package: outputs dist\MacroFlowStudio.exe + dist\paddle_ocr\
```

Running from source requires Python 3.13 + PaddleOCR dependencies (see run.bat); "OCR Text" requires the PaddleOCR model directory (`paddle_models`).

## 🛡️ Safety Limits

- Track sampling interval ≥ 10 ms; image-match polling interval ≥ 50 ms; max 200,000 actions per recording
- Held keys and mouse buttons are released on stop or abnormal exit whenever possible
- Respect the target software's terms of service; some games prohibit any form of macro or automation

---

## 📈 v1.0.0 Feature Overview

First public release, organized by feature area:

### Recording & Playback

- Smart track recording: raw relative movement while the cursor is locked at the window center (game view turning, sampled at ≤16 ms); desktop coordinates resume outside the window (default 20 ms, 10–500 ms configurable)
- Relative view turning has zero extra playback overhead: the window is only activated when the target window changes
- Automatically switches to English (US) input method before recording; F8 detaches new recordings from the open script
- Playback supports starting from a specific line, repeat counts, breakpoint resume, and multi-click

### Script Editing

- Action types: delay, keyboard, text, mouse move / click / multi-click, wheel, image match, OCR, notice, comment, script reference, open / close app, jump, foreground window, special modules, module reference
- Multi-select editing, undo / redo, duplicate down, run from here; script insertion as a live reference or a full copy (IDs rebuilt, jump references remapped)
- Jump targets use stable line-object IDs and stay valid after script edits

### Recognition & Module Objects

- Template matching: region, threshold, ignore background, timeout branches, secondary confirmation
- Offline PaddleOCR: loaded on demand; real text-box coordinates, pixel offsets, wait-until-text-gone, number reading
- Module repository: switch / workflow-global / script-global / special; enable / disable, bulk add / remove, post-action & timeout code segments, fallback recognition, multi-click, pre-recognition delay
- Window auto-hiding while picking coordinates on screen so the tool never blocks the target page

### Workflows

- Steps: repeat count, unlimited, wait before run, repeat interval, enable state, start delay, unified restart target, start-from-line from the 2nd run
- Workflow-global modules: hold-duration trigger, timeout code segments, end current script / restart workflow, breakpoint resume
- Test mode, run from selected line, single-run test; remaining counts and jump lines are strictly consistent

### System & Stability

- Focus lock mode with a system-level input lock; held keys are released on abnormal exit
- Execution mini window never steals focus; run logs are written per date / session
- Timed backup, auto start on boot, start to tray, auto-run a workflow at startup
- OCR engine externalized in the packaged build (~70 MB main exe); the build pipeline verifies version, symbols, and input locks

---

## 🤝 AI Collaboration Rules

- **DeepSeek**: no need to launch the app to test after changes — no program launches, no GUI-driving scripts to reproduce bugs, no visual checks, no packaged smoke tests. Acceptance = `python test_core.py` all green + successful build + `verify_build.py` passing; bug fixes are located via code reading and unit tests, and the user runs the app to verify afterwards.
- **GPT**: visual and smoke checks as needed — UI layout / button changes require launching the app to confirm; pure logic changes can be delivered directly; packaged DPI smoke tests on demand.

---

<div align="center">

**MacroFlow Studio** · [Changelog](CHANGELOG.md) · [Releases](https://github.com/SakuraLoveForever/MacroFlowStudio-/releases)

</div>
