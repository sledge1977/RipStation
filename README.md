# Multi-Drive Ripping Station

This Python script manages multiple drives to efficiently rip movies and TV series in parallel. It provides an interactive command-line interface to control the ripping process and performs automatic post-processing.

## Features

- **Parallel control** of multiple DVD or Blu-ray drives.
- **Differentiated ripping modes** for movies and TV series.
- Uses `makemkvcon` for robust ripping.
- **Automatic post-processing** after a successful rip:
  - **For Movies**:
    - Automatically finds all titles longer than 60 minutes.
    - Selects the longest title as the main feature.
  - **For TV Series**:
    - Finds all titles longer than 20 minutes.
    - Displays a list of all potential episodes for you to select.
    - Supports flexible selection (e.g., `1,3-5,8`).
    - Prompts for Series Name, Season, and starting Episode number.
    - Automatically names files sequentially (e.g., `My.Series.S01E05.mkv`, `My.Series.S01E06.mkv`, etc.).
    - Includes a "reverse order" option for discs where tracks are authored backwards.
  - Sets the metadata title in the MKV file using `mkvpropedit`.
  - Ejects the disc automatically after the entire queue is finished.
- **Interactive CLI** for easy operation.
- **Efficient rescan mode** that only checks drives that are idle or completed.

## Requirements

- Python 3
- MakeMKV (`makemkvcon` CLI tool)
- MKVToolNix (`mkvpropedit` CLI tool)
- The `eject` command (usually preinstalled on most Linux distributions).

## Installation

Ensure the required tools are installed. On Debian-based distributions (such as Ubuntu) you can install them like this:

- **MakeMKV** often needs to be downloaded from the official site or a PPA. See instructions on [makemkv.com](https://www.makemkv.com/forum/viewtopic.php?f=3&t=224).
- **MKVToolNix** can be installed via the package manager:
  ```bash
  sudo apt update && sudo apt install mkvtoolnix
  ```

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

- `BASE_OUTPUT_DIR`: The root folder where ripped files are stored. Each movie or series gets its own subfolder.
- `MAKEMKV_CMD`: Path to the `makemkvcon` command if it is not on your system PATH.

---

### Note on AI usage

Parts of this script and this README were created, modified, and translated with the help of artificial intelligence. The AI assisted with debugging, adding features, optimization, and translating code comments and output.
