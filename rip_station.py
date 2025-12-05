#!/usr/bin/env python3
import subprocess
import os
import time
import threading
import sys
import csv
import shlex
import select

# --- CONFIGURATION ---
BASE_OUTPUT_DIR = "/home/jens/Videos"
MIN_LENGTH = "3600"
MAKEMKV_CMD = "makemkvcon"
REFRESH_INTERVAL = 5  # seconds between UI refreshes when nothing changes

# Global list of drives
drives = []

def fetch_drive_info():
    """Runs makemkvcon once and returns parsed drive tuples (id, name, label, device)."""
    cmd = [MAKEMKV_CMD, "-r", "--cache=1", "info", "disc:9999"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=20)

    drives_info = []
    for line in result.stdout.splitlines():
        if line.startswith("DRV:"):
            clean_line = line[4:]
            reader = csv.reader([clean_line])
            row = list(reader)[0]

            # Expected layout: ID,?, ?, ?, "Name", "Label", "/dev/srX"
            if len(row) >= 6:
                d_id = int(row[0])
                d_name = row[4][:14]
                d_label = row[5]
                d_dev = row[6] if len(row) >= 7 else ""
                if d_dev:
                    drives_info.append((d_id, d_name, d_label, d_dev))
    return drives_info

class Drive:
    def __init__(self, mkv_id, device_path, label, name):
        self.mkv_id = mkv_id          # e.g., 0 (for disc:0)
        self.device_path = device_path # e.g., /dev/sr1
        self.label = label            # e.g., "COUCHGEFLUESTER"
        self.status = "IDLE"      # IDLE, RIPPING, COMPLETED, ERROR
        self.current_job = ""         # Name of the movie currently being ripped
        self.progress = 0             # Rip progress in %
        self.needs_rescan = False     # Flag for automatic rescan
        self.name = name              # For easier identification

def scan_drives():
    """Scans the drives once using makemkvcon."""
    global drives
    print("Scanning drives... please wait (this may take a moment)...")
    
    try:
        drives_info = fetch_drive_info()
        drives = [Drive(d_id, d_dev, d_label, d_name) for d_id, d_name, d_label, d_dev in drives_info]
    except subprocess.CalledProcessError as e:
        print(f"Error scanning drives: {e.stderr}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

def update_drive_label(drive):
    """Quickly rescans the label of a single drive."""
    try:
        cmd = [MAKEMKV_CMD, "-r", "--cache=1", "info", f"disc:{drive.mkv_id}"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=15)
        
        new_label = ""
        # Find the matching DRV line for the drive
        for line in result.stdout.splitlines():
            if line.startswith(f"DRV:{drive.mkv_id},"):
                clean_line = line[4:]
                reader = csv.reader([clean_line])
                row = list(reader)[0]
                if len(row) >= 6:
                    new_label = row[5]
                break # We found the info for our drive
        
        # Update status
        if drive.label != new_label:
            drive.label = new_label
            # If the label has changed, a new disc is inserted.
            # Set the status to IDLE so it can be ripped again.
            if drive.status == "COMPLETED":
                drive.status = "IDLE"
                drive.current_job = "" # Remove old job name
                
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        # This error often occurs when no disc is inserted.
        # We set the label to empty and the status to IDLE.
        drive.label = ""
        if drive.status == "COMPLETED":
            drive.status = "IDLE"
            drive.current_job = ""
    except Exception as e:
        print(f"\nError updating {drive.device_path}: {e}")
        time.sleep(2)

def rescan_idle_drives():
    """Rescans all drives once and updates labels without interrupting active rips."""
    print("Rescanning drives and updating labels...")
    try:
        drives_info = fetch_drive_info()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"Rescan failed: {e}")
        time.sleep(1)
        return
    except Exception as e:
        print(f"Unexpected error during rescan: {e}")
        time.sleep(1)
        return

    info_by_device = {d_dev: (d_id, d_name, d_label) for d_id, d_name, d_label, d_dev in drives_info}

    # Update existing drives in-place so ripping threads keep their references.
    for drive in drives:
        info = info_by_device.pop(drive.device_path, None)
        if info:
            new_id, new_name, new_label = info

            # Update the makemkv index in case it changed.
            drive.mkv_id = new_id
            drive.name = new_name

            if drive.label != new_label:
                drive.label = new_label
                if drive.status in ["IDLE", "COMPLETED", "ERROR", "ERROR (META)"]:
                    drive.status = "IDLE"
                    drive.current_job = ""
        else:
            # Drive missing from output; treat as no disc present.
            drive.label = ""
            if drive.status in ["COMPLETED", "ERROR", "ERROR (META)"]:
                drive.status = "IDLE"
                drive.current_job = ""

    # Add any newly detected drives.
    for d_dev, (d_id, d_name, d_label) in info_by_device.items():
        drives.append(Drive(d_id, d_dev, d_label, d_name))

    time.sleep(1) # Short pause so the message is visible


def rip_worker(drive, movie_name):
    """This is the background worker for a drive."""
    drive.status = "Analyzing"
    drive.current_job = movie_name
    drive.progress = 0
    drive.needs_rescan = False

    target_dir = os.path.join(BASE_OUTPUT_DIR, movie_name)
    os.makedirs(target_dir, exist_ok=True)

    cmd = [
        MAKEMKV_CMD,
        "-r", # Robot mode
        "--progress=-same", # Redirect progress to the same output stream
        f"--minlength={MIN_LENGTH}",
        "mkv",
        f"disc:{drive.mkv_id}",
        "all",
        target_dir
    ]

    ripping = False

    log_file = os.path.join(target_dir, "rip.log")
    with open(log_file, "a") as f, subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1) as proc:
        for line in proc.stdout:
            f.write(line) # Write to log file

            if line.startswith("PRGC:5017"):
                ripping = True
                drive.status = "RIPPING"

            # Parse progress (PRGV: current, total, max)
            if line.startswith("PRGV:") and ripping:
                try:
                    parts = line.split(":")[1].strip().split(",")
                    current, total = int(parts[0]), int(parts[2])
                    drive.progress = int((current / total) * 100)
                except (ValueError, IndexError, ZeroDivisionError):
                    pass # Ignore parsing errors
    
    if proc.returncode == 0:
        drive.status = "COMPLETED"
        drive.progress = 100
        
        # --- POST-PROCESSING: ADJUST FILENAME AND METADATA ---
        try:
            with open(log_file, "a") as f:
                f.write("\n--- Post-processing ---\n")
                
                # 1. Find the largest MKV file
                mkv_files = [os.path.join(target_dir, fname) for fname in os.listdir(target_dir) if fname.endswith(".mkv")]
                if not mkv_files:
                    f.write("WARNING: No MKV files found in the target directory.\n")
                else:
                    main_movie_file = max(mkv_files, key=os.path.getsize)
                    f.write(f"Main movie file identified: {main_movie_file}\n")
                    
                    # 2. Rename file
                    new_filename = f"{movie_name}.mkv"
                    new_filepath = os.path.join(target_dir, new_filename)
                    os.rename(main_movie_file, new_filepath)
                    f.write(f"File renamed to: {new_filepath}\n")

                    # 3. Adjust metadata title with mkvpropedit
                    mkvpropedit_cmd = [
                        "mkvpropedit",
                        new_filepath,
                        "--edit", "info",
                        "--set", f"title={movie_name}"
                    ]
                    prop_proc = subprocess.run(mkvpropedit_cmd, capture_output=True, text=True)
                    if prop_proc.returncode == 0:
                        f.write("Metadata title set successfully.\n")
                    else:
                        f.write(f"ERROR with mkvpropedit: {prop_proc.stderr}\n")
                        # Output hint for the user in the main program
                        drive.status = "ERROR (META)"

        except FileNotFoundError:
             with open(log_file, "a") as f:
                f.write("ERROR: 'mkvpropedit' not found. Please install MKVToolNix.\n")
             drive.status = "ERROR (META)"
        except Exception as e:
            with open(log_file, "a") as f:
                f.write(f"ERROR during post-processing: {e}\n")
            drive.status = "ERROR (META)"


        # --- COMPLETION ---
        try:
            subprocess.run(["eject", drive.device_path], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            print(f"Warning: Ejecting {drive.device_path} failed: {e.stderr}")
    else:
        drive.status = "ERROR"

    # Job is finished, state is displayed in the UI
    # Status reset and rescan are no longer handled here
    # drive.current_job = ""
    # drive.progress = 0
    # drive.needs_rescan = True
    drive.progress = 0 # Only reset the progress bar
    time.sleep(5)
    
def print_ui():
    """Draws the menu."""
    # Screen clear (works on Linux)
    os.system('clear')
    
    print("=" * 75)
    print("                   MULTI-DRIVE RIPPING STATION (PYTHON)")
    print("=" * 75)
    print(f"{'ID':<5} | {'Device':<10} | {'Name':<15} | {'Status':<15} | {'Disc / Job'}")
    print("-" * 75)
    
    for d in drives:
        status_text = d.status
        name_text = d.name
        if d.status == "RIPPING":
            status_text = f"RIPPING ({d.progress}%)"
            
        info_text = d.label
        if d.status in ["RIPPING", "COMPLETED", "ERROR"]:
             info_text = d.current_job if d.current_job else d.label
        elif d.status == "ERROR (META)":
            info_text = f"{d.current_job} (Post-processing failed)"

        print(f"{d.mkv_id:<5} | {d.device_path:<10} | {name_text:<15} | {status_text:<15} | {info_text}")
        
    print("-" * 75)
    print("Options:")
    print(" [0-9] Enter ID to rip")
    print(" [r]   Rescan (Rescan drives / update labels)")
    print(" [q]   Quit")
    print("-" * 75)

def main():
    scan_drives()
    
    # Buffer for user input
    input_buffer = ""
    last_snapshot = None
    last_render = 0
    force_render = True
    
    while True:
        # Decide whether to redraw the UI (state change or timed refresh)
        snapshot = tuple(
            (d.mkv_id, d.device_path, d.label, d.status, d.progress, d.current_job)
            for d in drives
        )
        now = time.time()
        if force_render or snapshot != last_snapshot or (now - last_render) >= REFRESH_INTERVAL:
            print_ui()
            print(f"Command: {input_buffer}", end="", flush=True)
            last_snapshot = snapshot
            last_render = now
            force_render = False

        # 3. Wait for input (with a timeout of 1 second)
        # sys.stdin is the standard input we want to monitor
        readable, _, _ = select.select([sys.stdin], [], [], 1.0)

        # 4. Process input, if any
        if readable:
            # Read one line (reacts to Enter)
            line = sys.stdin.readline()
            if line:
                user_input = line.strip()
                
                if user_input.lower() == 'q':
                    print("Exiting program...")
                    # Wait for running rip threads? Not for now.
                    sys.exit(0)
                    
                elif user_input.lower() == 'r':
                    rescan_idle_drives()
                    force_render = True
                    
                elif user_input.isdigit():
                    selected_drive = next((d for d in drives if d.mkv_id == int(user_input)), None)
                    
                    if selected_drive:
                        if selected_drive.status == "RIPPING":
                            # Temporary message instead of stopping the loop
                            print("\n!!! This drive is currently busy. Please wait.")
                            time.sleep(2)
                        else:
                            # The UI is redrawn immediately, the prompt
                            # therefore appears below the updated UI.
                            movie_name = input(f"\nMovie name for {selected_drive.device_path} (Disc: {selected_drive.label}): ")
                            if movie_name:
                                t = threading.Thread(target=rip_worker, args=(selected_drive, movie_name))
                                t.daemon = True
                                t.start()
                            force_render = True
                    else:
                        print("\nDrive ID not found.")
                        time.sleep(1)
                        force_render = True
                
                # Clear input buffer for the next iteration
                input_buffer = ""
        
        # If no input was received, the loop is repeated
        # and the UI is updated with the latest progress.

if __name__ == "__main__":
    main()
