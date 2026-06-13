# CHDconvert

Converts a directory of archives (`.gz`, `.7z`, `.zip`) or loose disc images (`.iso`, `.cue`/`.bin`) into `.chd` files, for use with console emulators.

---

# Requirements

- [`Python`](https://www.python.org/downloads/)
- [`Git`](https://git-scm.com/download/win) (for cloning)
- `chdman` — included on Windows (`chdman.exe`). On Linux/Mac, install via your package manager (see below).

Optional:
```
pip install psutil   # CPU/disk monitoring, live stats, core affinity
pip install py7zr    # .7z archive support
```

---

# Installation

### Method A: Clone

```
git clone https://github.com/nickheyer/CHDconvert
cd C:\Where\you\cloned\this\repo
pip install -r requirements.txt
```

### Method B: Download Release

Download and unzip from:
```
https://github.com/uqKami/CHDconvert/releases/latest
```
Then:
```
cd C:\Where\you\unzipped\the\release
pip install -r requirements.txt
```

---

# Linux / Mac

Install `chdman` via your package manager:
```
sudo apt install mame-tools       # Debian/Ubuntu
sudo pacman -S mame-tools         # Arch
brew install rom-tools            # macOS
```

---

# GUI Usage

![CHD Convert GUI](screenshot.png)

Launch the graphical interface:
```
python chdconvert_gui.py
```

### Input Directories

Use the **Add…** button to add one or more input directories. The file list populates automatically with every convertible file found — archives and loose disc images, including subdirectories. Use **Remove** to deselect directories.

### Supported Formats

| Type | Extensions |
|---|---|
| Archives | `.zip`, `.gz`, `.7z` |
| Disc images | `.iso`, `.cue` + `.bin` track files, orphan `.bin` (auto-generates a `.cue`) |

### Output Directory

Leave the output directory blank to write each CHD alongside its source file. Enter or browse to a custom path to send all output there instead.

### Options

| Option | Description |
|---|---|
| Delete temp directory after conversion | Removes the `_tmp` extraction folder when done |
| Replace original archives | Outputs CHDs into the same input directory and deletes the originals (implies delete temp; disables custom output dir) |
| Delete original archive/ISO/CUE/BIN after successful conversion | Removes the source file once the CHD has been created successfully |
| Name output CHD after parent folder | Names the CHD after the containing subfolder — e.g. `Sonic The Hedgehog (USA)/track01.cue` → `Sonic The Hedgehog (USA).chd` |
| Skip if output CHD already exists | Skips files where a `.chd` of the same name is already present; uncheck to overwrite |

### Concurrency & Resource Throttling

| Setting | Description |
|---|---|
| Concurrent jobs | Number of ISO/CUE/BIN files to convert in parallel (1–16). Archives (.zip/.gz/.7z) always run one at a time to avoid thrashing disk I/O during extraction. |
| CPU limit % | Pause starting new jobs when system CPU exceeds this threshold (0 = no limit) |
| Disk write limit MB/s | Pause starting new jobs when disk write speed exceeds this threshold (0 = no limit) |
| Stop if output drive exceeds % full | Halt all new conversions when the destination drive reaches this fill level (0 = disabled) |

### Progress

- **Per-file grid** — each file shows its own status (Pending / Converting / Done / Error / Skipped / Stopped) and a live progress bar
- **Overall bar** — tracks completion across all files
- **Live resource stats** — real-time CPU% and disk read/write speeds (requires `psutil`)
- **Log output** — scrollable log of all chdman output and conversion events

---

# Shoutouts

Thanks CHDMAN!
