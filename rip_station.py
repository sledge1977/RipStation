#!/usr/bin/env python3
import subprocess
import os
import time
import threading
import sys
import csv
import re
import shlex
import select

# --- CONFIGURATION ---
BASE_OUTPUT_DIR = "/home/jens/Videos"
# MIN_LENGTH is now set dynamically
MAKEMKV_CMD = "makemkvcon"
REFRESH_INTERVAL = 5  # seconds between UI refreshes when nothing changes

# Global list of drives
drives = []

def fetch_drive_info():
    """Runs makemkvcon once and returns parsed drive tuples (id, name, label, device)."""
    try:
        cmd = [MAKEMKV_CMD, "-r", "--cache=1", "info", "disc:9999"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=20)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"FATAL: makemkvcon failed to run. Is it installed and in your PATH? Error: {e}")
        sys.exit(1)


    drives_info = []
    for line in result.stdout.splitlines():
        if line.startswith("DRV:"):
            # DRV:index,visible,enabled,flags,drive_name,disc_name,device_path
            clean_line = line[4:]
            reader = csv.reader([clean_line])
            row = list(reader)[0]

            if len(row) >= 6:
                d_id = int(row[0])
                d_name = row[4][:14] # Drive Name
                d_label = row[5] # Disc Label
                d_dev = row[6] if len(row) >= 7 else ""
                if d_dev:
                    drives_info.append((d_id, d_name, d_label, d_dev))
    return drives_info

class Drive:
    def __init__(self, mkv_id, device_path, label, name):
        self.mkv_id = mkv_id          # e.g., 0 (for disc:0)
        self.device_path = device_path # e.g., /dev/sr1
        self.label = label            # e.g., "COUCHGEFLUESTER"
        self.status = "IDLE"      # IDLE, RIPPING, COMPLETED, ERROR, etc.
        self.current_job = ""         # Name of the movie/series being ripped
        self.progress = 0             # Rip progress in %
        self.name = name              # For easier identification, e.g. "HL-DT-ST"

def scan_drives():
    """Scans the drives once using makemkvcon at startup."""
    global drives
    print("Scanning drives... please wait (this may take a moment)...")
    
    try:
        drives_info = fetch_drive_info()
        drives = [Drive(d_id, d_dev, d_label, d_name) for d_id, d_name, d_label, d_dev in drives_info]
    except Exception as e:
        print(f"An unexpected error occurred during initial scan: {e}")
        drives = [] # Ensure drives is an empty list

def rescan_idle_drives():
    """Rescans all drives and updates labels without interrupting active rips."""
    print("Rescanning drives and updating labels...")
    try:
        drives_info = fetch_drive_info()
    except Exception as e:
        print(f"Unexpected error during rescan: {e}")
        return

    info_by_device = {d_dev: (d_id, d_name, d_label) for d_id, d_name, d_label, d_dev in drives_info}

    # Update existing drives in-place so ripping threads keep their references.
    for drive in drives:
        info = info_by_device.pop(drive.device_path, None)
        if info:
            new_id, new_name, new_label = info
            drive.mkv_id = new_id
            drive.name = new_name

            if drive.label != new_label:
                drive.label = new_label
                if drive.status in ["IDLE", "COMPLETED", "ERROR", "ERROR (META)", ""]:
                    drive.status = "IDLE"
                    drive.current_job = ""
        else:
            # Drive from previous scan is no longer detected.
            drive.label = ""
            if drive.status in ["COMPLETED", "ERROR", "ERROR (META)"]:
                drive.status = "IDLE"
                drive.current_job = ""

    # Add any newly detected drives.
    for d_dev, (d_id, d_name, d_label) in info_by_device.items():
        if not any(d.device_path == d_dev for d in drives):
            drives.append(Drive(d_id, d_dev, d_label, d_name))

    time.sleep(1) # Short pause so the message is visible



def parse_duration_to_seconds(duration_str):
    """Converts a duration string like "1:23:45" or "23:45" to seconds."""
    parts = duration_str.split(':')
    seconds = 0
    try:
        if len(parts) == 3:  # HH:MM:SS
            seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:  # MM:SS
            seconds = int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 1: # SS
            seconds = int(parts[0])
    except (ValueError, IndexError):
        return 0 # Return 0 if parsing fails
    return seconds

def get_disc_tracks(drive, min_len_seconds):
    """Gets all tracks from a disc longer than a certain duration by parsing makemkvcon output."""
    cmd = [MAKEMKV_CMD, "-r", "--cache=1", "info", f"disc:{drive.mkv_id}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"Error getting track info for {drive.device_path}: {e}")
        return []

    tracks = {}
    for line in result.stdout.splitlines():
        if not line.startswith("TINFO:"):
            continue

        try:
            # TINFO:track_id,attribute_id,code,"value"
            parts = line.split(":", 1)[1].split(",", 3)
            track_id = int(parts[0])
            attribute_id = int(parts[1])
            value = parts[3].strip('"')

            if track_id not in tracks:
                tracks[track_id] = {'id': track_id}

            if attribute_id == 9: # Duration String (e.g., "1:23:45")
                tracks[track_id]['duration_str'] = value
                tracks[track_id]['duration_seconds'] = parse_duration_to_seconds(value)
            elif attribute_id == 10: # Size String
                tracks[track_id]['size_str'] = value
            elif attribute_id == 11: # Size Bytes
                tracks[track_id]['size_bytes'] = int(value)
            elif attribute_id == 16: # Source Filename (e.g., 00800.mpls)
                tracks[track_id]['source_filename'] = value
            elif attribute_id == 27: # Output Filename from MakeMKV (e.g., "title00.mkv")
                tracks[track_id]['output_filename'] = value

        except (IndexError, ValueError):
            # Ignore malformed lines
            continue

    # Filter tracks by minimum length
    valid_tracks = [
        t for t in tracks.values() 
        if t.get('duration_seconds', 0) >= min_len_seconds
    ]
    
    return sorted(valid_tracks, key=lambda x: x['id'])

def parse_track_selection(selection_str):
    """Parses a track selection string like '1, 3 - 5, 8' into a sorted list of ints."""
    selection = set()
    # Remove all whitespace to simplify parsing
    selection_str = ''.join(selection_str.split())
    
    if not selection_str:
        return []

    try:
        for part in selection_str.split(','):
            if not part: continue
            if '-' in part:
                start, end = map(int, part.split('-'))
                if start > end: # Allow reverse ranges like 5-3
                    start, end = end, start
                selection.update(range(start, end + 1))
            else:
                selection.add(int(part))
    except ValueError:
        print(f"Warning: Invalid characters in track selection '{selection_str}'")
        return [] # Return empty list on parsing error
        
    return sorted(list(selection))


def rip_jobs_worker(drive, jobs):
    """
    This is the new background worker that processes a list of rip jobs.
    A job is a tuple: (track_id, final_output_path, source_mkv_filename)
    """
    drive.status = "Starting..."
    drive.progress = 0
    total_jobs = len(jobs)
    # Use the final intended name for display, which is more user-friendly
    original_job_name = os.path.splitext(os.path.basename(jobs[0][1] if jobs else "Unknown Job"))[0]

    for i, (track_id, final_output_path, source_mkv_filename) in enumerate(jobs):
        job_progress_text = f"({i+1}/{total_jobs})"
        drive.status = f"Ripping {job_progress_text}"
        drive.current_job = os.path.basename(final_output_path)

        target_dir = os.path.dirname(final_output_path)
        os.makedirs(target_dir, exist_ok=True)
        
        cmd = [
            MAKEMKV_CMD,
            "-r",
            "--progress=-same",
            "mkv",
            f"disc:{drive.mkv_id}",
            str(track_id), # Rip specific track
            target_dir
        ]
        
        # Ensure log file is placed in a consistent directory for the whole job
        log_file_dir = os.path.dirname(jobs[0][1]) if jobs else target_dir
        log_file = os.path.join(log_file_dir, "rip.log")

        # Open log file for the entire duration of this track's rip and post-processing
        with open(log_file, "a") as f:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            f.write(f"\n--- Starting Job {job_progress_text}: Rip Track {track_id} to {final_output_path} ---\n")
            
            # Read output from the subprocess in real-time
            for line in proc.stdout:
                f.write(line)
                if line.startswith("PRGV:"):
                    try:
                        parts = line.split(":")[1].strip().split(",")
                        current, total = int(parts[0]), int(parts[2])
                        drive.progress = int((current / total) * 100)
                    except (ValueError, IndexError, ZeroDivisionError):
                        pass
            
            # Wait for the process to terminate and get the return code
            proc.wait()

            # Rip is finished for this track, now post-process.
            drive.status = f"Processing {job_progress_text}"
            f.write("\n--- Post-processing ---\n")

            if proc.returncode == 0:
                try:
                    # Use the exact filename provided by makemkvcon's info scan
                    created_file = os.path.join(target_dir, source_mkv_filename)
                    if not os.path.exists(created_file):
                        raise FileNotFoundError(f"MakeMKV did not create the expected file: {created_file}")

                    # Check if the final destination file already exists
                    if os.path.exists(final_output_path):
                        base, ext = os.path.splitext(final_output_path)
                        final_output_path = f"{base}_{int(time.time())}{ext}"
                        f.write(f"WARNING: Destination file existed. Renaming to: {final_output_path}\n")

                    # 1. Rename file
                    os.rename(created_file, final_output_path)
                    f.write(f"File renamed to: {final_output_path}\n")

                    # 2. Set metadata title
                    try:
                        mkvpropedit_cmd = [
                            "mkvpropedit", final_output_path, "--edit", "info",
                            "--set", f"title={os.path.splitext(os.path.basename(final_output_path))[0]}"
                        ]
                        prop_proc = subprocess.run(mkvpropedit_cmd, capture_output=True, text=True)
                        if prop_proc.returncode == 0:
                            f.write("Metadata title set successfully.\n")
                        else:
                            error_msg = f"WARNING: mkvpropedit failed: {prop_proc.stderr.strip()}"
                            f.write(error_msg + "\n")
                            print(f"\n{error_msg}")
                            drive.status = "WARN (META)"

                    except FileNotFoundError:
                        error_msg = "ERROR: 'mkvpropedit' command not found. Please install MKVToolNix to set metadata titles."
                        f.write(error_msg + "\n")
                        print(f"\n{error_msg}")
                        drive.status = "WARN (META)"

                except FileNotFoundError as e:
                     f.write(f"ERROR: {e}\n")
                     drive.status = "ERROR (Rename)"
                     break # Stop processing further jobs for this drive
                except Exception as e:
                    f.write(f"ERROR during post-processing: {e}\n")
                    drive.status = "ERROR (Post)"
                    break # Stop processing further jobs for this drive
            else:
                drive.status = "ERROR (Rip)"
                drive.current_job = original_job_name # Show series/movie name on error
                f.write(f"ERROR: MakeMKV process failed with return code {proc.returncode}\n")
                break # Stop processing further jobs for this drive

    if not drive.status.startswith("ERROR"):
        drive.status = "COMPLETED"
        drive.current_job = original_job_name # Show series/movie name on completion
        
        # Eject disc on successful completion of all jobs
        try:
            print(f"\nEjecting {drive.device_path}...")
            subprocess.run(["eject", drive.device_path], check=True, capture_output=True)
        except FileNotFoundError:
            print(f"\nWarning: 'eject' command not found. Cannot eject disc.")
        except subprocess.CalledProcessError as e:
            print(f"\nWarning: Ejecting {drive.device_path} failed: {e.stderr.strip()}")
        except Exception as e:
            print(f"\nAn unexpected error occurred during eject: {e}")
    
    drive.progress = 0
    # Add a final, shorter sleep to ensure the "COMPLETED" status is visible before rescan
    time.sleep(3)
    
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
        # Add progress percentage to any status that starts with 'Ripping'
        if d.status.startswith("Ripping"):
            status_text = f"{d.status} ({d.progress}%)"
            
        info_text = d.label
        # For most non-idle states, show the current job name
        if d.status not in ["IDLE", ""]:
             info_text = d.current_job if d.current_job else d.label
        
        # Special text for post-processing errors
        if d.status == "ERROR (META)":
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
    
    last_snapshot = None
    last_render = 0
    force_render = True
    
    while True:
        snapshot = tuple(
            (d.mkv_id, d.device_path, d.label, d.status, d.progress, d.current_job)
            for d in drives
        )
        now = time.time()
        # Redraw UI if state changed, forced, or refresh interval passed
        if force_render or snapshot != last_snapshot or (now - last_render) >= REFRESH_INTERVAL:
            print_ui()
            print("Command: ", end="", flush=True)
            last_snapshot = snapshot
            last_render = now
            force_render = False

        # Wait for user input with a 1-second timeout
        readable, _, _ = select.select([sys.stdin], [], [], 1.0)

        if readable:
            line = sys.stdin.readline()
            if line:
                user_input = line.strip()
                
                if user_input.lower() == 'q':
                    print("Exiting program...")
                    sys.exit(0)
                    
                elif user_input.lower() == 'r':
                    rescan_idle_drives()
                    force_render = True
                    
                elif user_input.isdigit():
                    selected_drive = next((d for d in drives if d.mkv_id == int(user_input)), None)
                    
                    if selected_drive:
                        if selected_drive.status not in ["IDLE", "COMPLETED", "ERROR", "ERROR (META)", "", "WARN (META)"]:
                            print("\n!!! This drive is currently busy. Please wait.")
                            time.sleep(2)
                            force_render = True
                            continue # Skip to next loop iteration
                        
                        # --- NEW RIP FLOW ---
                        os.system('clear')
                        print(f"--- Preparing Drive {selected_drive.mkv_id} ({selected_drive.label or 'No Label'}) ---")
                        
                        media_type = input("Is this a Movie or a Series? [m/s]: ").lower()
                        
                        min_length = 0
                        if media_type == 'm':
                            min_length = 3600 # 60 minutes
                        elif media_type == 's':
                            min_length = 1200 # 20 minutes
                        else:
                            print("Invalid selection. Aborting.")
                            time.sleep(2)
                            force_render = True
                            continue

                        print("\nScanning for valid tracks... (this may take a moment)")
                        tracks = get_disc_tracks(selected_drive, min_length)

                        if not tracks:
                            print(f"\nNo tracks found matching the minimum length of {min_length // 60} minutes.")
                            time.sleep(3)
                            force_render = True
                            continue
                        
                        jobs = []
                        # --- SERIES ---
                        if media_type == 's':
                            print("\nAvailable Tracks:")
                            print(f"{'Track#':<8} | {'Duration':<12} | {'Size':<10} | {'MKV File Name'}")
                            print("-" * 60)
                            for t in tracks:
                                duration = t.get('duration_str', 'N/A')
                                size = t.get('size_str', 'N/A')
                                source = t.get('output_filename', 'N/A')
                                print(f"{t['id']:<8} | {duration:<12} | {size:<10} | {source}")
                            print("-" * 60)

                            selected_tracks_str = input("Enter tracks to rip (e.g., 1,3-5): ")
                            selected_track_ids = parse_track_selection(selected_tracks_str)
                            
                            tracks_by_id = {t['id']: t for t in tracks}
                            final_tracks = [tracks_by_id[tid] for tid in selected_track_ids if tid in tracks_by_id]

                            if not final_tracks:
                                print("No valid tracks selected. Aborting.")
                                time.sleep(2)
                                force_render = True
                                continue

                            series_name = input("Series Name: ")
                            season_num = int(input("Season Number: "))
                            start_ep = int(input("Start Episode on this Disc: "))
                            reverse_order = input("Reverse episode order? [y/N]: ").lower() == 'y'
                            
                            episode_numbers = list(range(start_ep, start_ep + len(final_tracks)))
                            if reverse_order:
                                episode_numbers.reverse()

                            for track, ep_num in zip(final_tracks, episode_numbers):
                                filename = f"{series_name}.S{season_num:02d}E{ep_num:02d}.mkv"
                                season_dir = os.path.join(BASE_OUTPUT_DIR, series_name, f"Season {season_num:02d}")
                                output_path = os.path.join(season_dir, filename)
                                mkv_filename = track.get('output_filename', f'title{track["id"]-1:02}.mkv')
                                jobs.append((track['id'], output_path, mkv_filename))
                        
                        # --- MOVIE ---
                        else:
                            longest_track = max(tracks, key=lambda t: t.get('duration_seconds', 0))
                            print(f"\nAutomatically selected longest track: #{longest_track['id']} ({longest_track.get('duration_str', 'N/A')})")
                            
                            movie_name = input("Movie Name: ")
                            if movie_name:
                                filename = f"{movie_name}.mkv"
                                output_path = os.path.join(BASE_OUTPUT_DIR, movie_name, filename)
                                mkv_filename = longest_track.get('output_filename', f'title{longest_track["id"]-1:02}.mkv')
                                jobs.append((longest_track['id'], output_path, mkv_filename))
                        
                        if jobs:
                            # Start the rip
                            t = threading.Thread(target=rip_jobs_worker, args=(selected_drive, jobs))
                            t.daemon = True
                            t.start()

                        force_render = True # Redraw UI immediately
                    else:
                        print("\nDrive ID not found.")
                        time.sleep(1)
                        force_render = True
                
                # Input has been processed, force a redraw on the next loop
                force_render = True
        
if __name__ == "__main__":
    main()
