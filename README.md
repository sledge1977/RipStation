# Multi-Drive Ripping Station

This Python script manages multiple drives to efficiently rip movies and TV series in parallel. It provides an interactive command-line interface to control the ripping process and performs automatic post-processing.

## Platform Support

The script is designed to be cross-platform and has been tested on **Linux** and **Windows**. It automatically adapts paths and commands for the operating system it's running on.

## Features

- **Parallel control** of multiple DVD or Blu-ray drives.
- **Cross-platform** support for Linux and Windows.
- **Differentiated ripping modes** for movies and TV series.
- Uses `makemkvcon` for robust ripping.
- **Automatic post-processing** after a successful rip:
  - **For Movies**:
    - Automatically finds all titles longer than 60 minutes.
    - Selects the longest title as the main feature.
  - **For TV Series**:
    - Groups tracks with similar runtimes and recommends the most likely episode block.
    - Marks probable duplicate playlists instead of silently ripping them twice.
    - Detects common episode labels such as `S01E05`, `1x05`, `Episode 5`, and `Folge 5`.
    - Shows every track from one minute onward, so short-form series remain selectable.
    - Lets you confirm or change selection and track order (including reverse order).
    - Supports exact episode lists and gaps (for example `5-8` or `5,6,9,10`).
    - Continues numbering after existing files in the season folder by default.
    - Supports season `00` for specials and names files such as `My Series.S00E03.mkv`.
    - Refuses to overwrite an existing episode file.
  - Sets the metadata title in the MKV file using `mkvpropedit`.
  - Ejects the disc automatically after the entire queue is finished (Linux only).
- **Responsive terminal UI**:
  - Full table with progress bars on wide terminals.
  - Reduced table on medium widths and a compact card layout on narrow terminals.
  - Adapts to terminal resizing and clips long Unicode labels without breaking columns.
  - Limits visible rows on short terminals and reports how many drives are hidden.
  - Dashboard commands react immediately without requiring Enter.
- **Non-blocking operation** while multiple drives are ripping.
- **Efficient rescan mode** that only checks drives that are idle or completed.

## Requirements

- Python 3
- **MakeMKV**: The `makemkvcon` (or `makemkvcon64.exe`) command-line tool must be available.
- **MKVToolNix**: The `mkvpropedit` command-line tool is optional but recommended for setting metadata titles.
- **Linux**: The `eject` command (usually preinstalled).
- **Windows**: No special requirements for ejecting, but you may need to do so manually if the script cannot.

## Installation

Ensure the required tools are installed and accessible from your system's PATH.

- **MakeMKV**: Download from [makemkv.com](https://www.makemkv.com). On Windows, the script will attempt to find the executable in standard installation directories. On Linux, ensure `makemkvcon` is in your PATH.
- **MKVToolNix**: Download from [mkvtoolnix.download](https://mkvtoolnix.download/downloads.html) or install via your system's package manager.
  - *Linux (Debian/Ubuntu)*: `sudo apt update && sudo apt install mkvtoolnix`
  - *Windows*: Use the installer from the website.

## Usage

1. Run the script in your terminal:
   ```bash
   python3 rip_station.py
   ```
2. The script first scans all available drives.
3. The UI appears and lists all detected drives.
4. Use these commands:
    - `[0-9]`: Enter the ID of the drive you want to rip and press Enter.
      - You will be prompted to choose **Movie** or **Series**.
      - **If Movie**: The script automatically selects the longest track. You will only be asked for the movie's name.
      - **If Series**: You will be shown a list of all tracks matching the minimum length for an episode. You can select the tracks to rip, and will then be asked for the series name, season, and starting episode number.
    - `[r]`: Triggers a quick rescan. Only drives with status `IDLE` or `COMPLETED` are checked for new discs.
    - `[q]`: Quits the program.

## Configuration

At the top of `rip_station.py` you can adjust some constants:

- `BASE_OUTPUT_DIR`: The root folder where ripped files are stored. It defaults to the current user's `Videos` folder on Linux and Windows. Each movie or series gets its own subfolder.
- `MAKEMKV_CMD`: Path to the `makemkvcon` command. On Windows, this is detected automatically. On Linux, it's assumed to be in the PATH. You can hardcode a specific path here if needed.

## Tests

The episode detection and numbering logic can be tested without a disc or MakeMKV:

```bash
python3 -m unittest -v
```

---

### Note on AI usage

Parts of this script and this README were created, modified, and translated with the help of artificial intelligence. The AI assisted with debugging, adding features, optimization, and translating code comments and output.
