# Multi-Drive Ripping Station

This Python script manages multiple drives to efficiently rip movies and TV series in parallel. It provides an interactive command-line interface to control the ripping process and performs automatic post-processing.

## Platform Support

The script is designed to be cross-platform and has been tested on **Linux** and **Windows**. It automatically adapts paths and commands for the operating system it's running on.

## Features

- **Parallel control** of multiple DVD or Blu-ray drives.
- **Safe parallel job handling**:
  - A drive is marked busy before its worker starts and released only after cleanup.
  - The physical `dev:` source is preferred; `disc:` remains a tested compatibility fallback.
  - Each job keeps the successful MakeMKV source captured at startup, even during rescans.
  - Planned output files are reserved so two drives cannot produce the same episode or movie.
  - MakeMKV writes each active track into an isolated staging directory to prevent raw filename collisions.
  - Unexpected worker errors or cancellations terminate and, if necessary, kill MakeMKV before releasing the drive.
  - Cancel active rips on specific drives (`[c]`) cleanly at any time without terminating the station.
  - Graceful shutdown on Ctrl+C (`SIGINT`), cleanly terminating all running MakeMKV child processes.
- **Cross-platform** support for Linux and Windows:
  - Automatic detection of `makemkvcon` and `mkvpropedit` executables (including Windows standard directories).
  - Automatic disc ejection on both Linux (`eject`) and Windows (PowerShell COM object).
- **Differentiated ripping modes** for movies and TV series:
  - Smart default name suggestions derived from disc labels (e.g. `BREAKING_BAD_S01D02` -> `Breaking Bad`).
  - Consistent German interactive prompts with `[q]` cancellation supported at every step.
  - **For Movies**:
    - Automatically finds all titles longer than 60 minutes (configurable via CLI).
    - Recommends the longest title while still showing all candidates for confirmation.
  - **For TV Series**:
    - Groups tracks with similar runtimes and recommends the most likely episode block.
    - Marks probable duplicate playlists instead of silently ripping them twice.
    - Detects common episode labels such as `S01E05`, `1x05`, `Episode 5`, and `Folge 5`.
    - Shows every track from one minute onward, so short-form series remain selectable.
    - Lets you confirm or change selection and track order (including reverse order).
    - Supports exact episode lists and gaps (for example `5-8` or `5,6,9,10`).
    - Robust episode numbering continuing after existing files (matching `.S01E05`, ` - S01E05`, `_S01E05`, etc.).
    - Supports season `00` for specials and names files such as `My Series.S00E03.mkv`.
    - Rejects mixed-season selections so detected `S01` and `S02` tracks cannot be mislabeled.
    - Refuses to overwrite an existing episode file.
  - Sets the metadata title in the MKV file using `mkvpropedit`.
  - Bounded timeouts for metadata updates and drive ejection.
- **Responsive terminal UI**:
  - Full table with progress bars on wide terminals.
  - Reduced table on medium widths and a compact card layout on narrow terminals.
  - Adapts to terminal resizing and clips long Unicode labels without breaking columns.
  - Limits visible rows on short terminals and reports how many drives are hidden.
  - Dashboard commands react immediately without requiring Enter and support multi-digit drive IDs.
- **Non-blocking operation** while multiple drives are ripping.
- **Efficient rescan mode** that only checks drives that are idle or completed.
- **Clean modular architecture**:
  - `ripstation/` package with dedicated modules for `config`, `models`, `analyzer`, `hardware`, `worker`, `prompts`, and `ui`.

## Requirements

- Python 3.8+
- **MakeMKV**: The `makemkvcon` (or `makemkvcon64.exe`) command-line tool must be available.
- **MKVToolNix**: The `mkvpropedit` command-line tool is optional but recommended for setting metadata titles.
- **Linux**: The `eject` command (usually preinstalled).
- **Windows**: PowerShell (preinstalled on modern Windows) for automatic drive ejection.

## Installation

Ensure the required tools are installed and accessible from your system's PATH (or installed in standard directories).

- **MakeMKV**: Download from [makemkv.com](https://www.makemkv.com). On Windows, standard installation paths are detected automatically. On Linux, ensure `makemkvcon` is in your PATH.
- **MKVToolNix**: Download from [mkvtoolnix.download](https://mkvtoolnix.download/downloads.html) or install via your system's package manager.
  - *Linux (Debian/Ubuntu)*: `sudo apt update && sudo apt install mkvtoolnix`
  - *Windows*: Standard installation paths (e.g. `C:\Program Files\MKVToolNix`) are detected automatically.

## Usage

1. Run the script in your terminal:
   ```bash
   python3 rip_station.py
   ```
   Or with optional command-line arguments:
   ```bash
   python3 rip_station.py -o /path/to/media --no-eject --min-movie-len 45
   ```
2. The script first scans all available drives.
3. The UI appears and lists all detected drives.
4. Use these commands:
    - `[ID]`: Enter the drive ID. Unambiguous IDs react immediately; ambiguous prefixes briefly wait for another digit.
      - You will be prompted to choose **Film** or **Serie**.
      - Disc labels are cleaned and offered as default name suggestions (`[Enter]`).
      - Type `q` at any prompt to cancel and return to the main dashboard.
      - **If Movie**: All candidates are displayed. The longest is recommended, but you can select a different title.
      - **If Series**: You will be shown the analyzed track list and can confirm selection, order, season, and exact episode numbers.
    - `[c]`: Cancel an ongoing rip on a specific drive.
    - `[r]`: Triggers a quick rescan. Only drives with status `IDLE` or `COMPLETED` are checked for new discs.
    - `[q]`: Quits the program (running rips can be aborted or allowed to finish).

## CLI Arguments

- `-o`, `--output-dir PATH`: Root folder where ripped files are stored (default: user's `Videos` directory).
- `--no-eject`: Do not automatically eject the disc after ripping.
- `--makemkv PATH`: Custom path to the `makemkvcon` executable.
- `--mkvpropedit PATH`: Custom path to the `mkvpropedit` executable.
- `--min-movie-len MIN`: Minimum duration in minutes for movie candidate tracks (default: 60).

## Tests

The episode detection, numbering logic, and CLI handlers can be tested without a disc or MakeMKV:

```bash
python3 -m unittest -v
```

---

### Note on AI usage

Parts of this script and this README were created, modified, and translated with the help of artificial intelligence. The AI assisted with debugging, adding features, optimization, and translating code comments and output.
