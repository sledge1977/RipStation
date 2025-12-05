# Jens' Multi-Drive Ripping Station

This Python script manages multiple DVD drives to rip movies efficiently in parallel. It provides an interactive command-line interface to control ripping and performs automatic post-processing. Blu-ray discs and TV series support are planned for later.

## Features

- **Parallel control** of multiple DVD drives.
- Uses `makemkvcon` for ripping movie titles.
- **Automatic post-processing** after a successful rip (movies):
  - Identifies the largest MKV file as the main movie.
  - Renames the file based on the movie name you enter.
  - Sets the metadata title in the MKV with `mkvpropedit`.
  - Ejects the disc automatically after finishing.
- **Interactive CLI** for easy operation.
- **Efficient rescan mode** that only checks drives that are idle or completed so the system stays responsive.

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
    - `[0-9]`: Enter the ID of the drive you want to rip and press Enter. You will be prompted for a movie name (DVD movies only for now).
    - `[r]`: Triggers a quick rescan. Only drives with status `IDLE` or `COMPLETED` are checked for new discs.
    - `[q]`: Quits the program.

## Configuration

At the top of `rip_station.py` you can adjust some constants:

- `BASE_OUTPUT_DIR`: The root folder where ripped movies are stored. Each movie gets its own subfolder.
- `MIN_LENGTH`: Minimum title length in seconds that `makemkvcon` should keep. Helps ignore extras and trailers.
- `MAKEMKV_CMD`: Path to the `makemkvcon` command if it is not on your system PATH.

---

### Note on AI usage

Parts of this script and this README were created, modified, and translated with the help of artificial intelligence. The AI assisted with debugging, adding features, optimization, and translating code comments and output. Blu-ray and TV series handling will be added later.
