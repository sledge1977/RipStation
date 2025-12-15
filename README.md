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
    - Finds all titles longer than 20 minutes.
    - Displays a list of all potential episodes for you to select.
    - Supports flexible selection (e.g., `1,3-5,8`).
    - Prompts for Series Name, Season, and starting Episode number.
    - Automatically names files sequentially (e.g., `My.Series.S01E05.mkv`).
  - Sets the metadata title in the MKV file using `mkvpropedit`.
  - Ejects the disc automatically after the entire queue is finished (Linux only).
- **Interactive CLI** with a responsive, non-blocking interface.
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

- `BASE_OUTPUT_DIR`: The root folder where ripped files are stored. The script sets a default based on your OS (`/home/jens/Videos` on Linux, `C:\Users\<YourUser>\Videos` on Windows). Each movie or series gets its own subfolder.
- `MAKEMKV_CMD`: Path to the `makemkvcon` command. On Windows, this is detected automatically. On Linux, it's assumed to be in the PATH. You can hardcode a specific path here if needed.

---

### Note on AI usage

Parts of this script and this README were created, modified, and translated with the help of artificial intelligence. The AI assisted with debugging, adding features, optimization, and translating code comments and output.