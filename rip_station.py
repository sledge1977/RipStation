#!/usr/bin/env python3
import subprocess
import os
import time
import threading
import sys
import csv
import select

# Windows-specific import
if os.name == 'nt':
    import msvcrt

# --- CONFIGURATION ---

# Unterscheidung der Pfade und Befehle je nach Betriebssystem
if os.name == 'nt':
    # WINDOWS
    # Passe diesen Pfad an, falls MakeMKV woanders installiert ist
    BASE_OUTPUT_DIR = os.path.join(os.environ['USERPROFILE'], "Videos")
    possible_paths = [
        r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe",
        r"C:\Program Files (x86)\MakeMKV\makemkvcon.exe",
        r"C:\Program Files\MakeMKV\makemkvcon64.exe"
    ]
    # Nimm den ersten Pfad, der existiert, sonst Standard
    MAKEMKV_CMD = next((p for p in possible_paths if os.path.exists(p)), "makemkvcon")
else:
    # LINUX
    BASE_OUTPUT_DIR = "/home/jens/Videos"
    MAKEMKV_CMD = "makemkvcon"

REFRESH_INTERVAL = 5  # Sekunden

# Global list of drives
drives = []

# --- HELPER FUNCTIONS ---

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def eject_drive(device_path):
    """Versucht das Laufwerk je nach OS auszuwerfen."""
    try:
        if os.name == 'nt':
            # Einfacher PowerShell Befehl zum Auswerfen (funktioniert meistens für das erste Laufwerk)
            # Für spezifische Laufwerksbuchstaben ist es unter Windows komplexer ohne externe Tools.
            # Wir geben hier nur eine Meldung aus, um Abstürze zu vermeiden.
            print(f"Hinweis: Bitte Laufwerk {device_path} manuell auswerfen (Windows Eject nicht implementiert).")
        else:
            subprocess.run(["eject", device_path], check=True, capture_output=True)
            print(f"Laufwerk {device_path} ausgeworfen.")
    except Exception as e:
        print(f"Fehler beim Auswerfen: {e}")

# --- CORE LOGIC ---

def fetch_drive_info():
    """Runs makemkvcon once and returns parsed drive tuples."""
    try:
        cmd = [MAKEMKV_CMD, "-r", "--cache=1", "info", "disc:9999"]
        # creationflags verhindern auf Windows, dass ein leeres CMD-Fenster aufpoppt (optional)
        creation_flags = 0
        if os.name == 'nt':
            creation_flags = 0x08000000 # CREATE_NO_WINDOW
            
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120, creationflags=creation_flags)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"FATAL: makemkvcon failed to run. Path: {MAKEMKV_CMD}")
        print(f"Error: {e}")
        sys.exit(1)

    drives_info = []
    for line in result.stdout.splitlines():
        if line.startswith("DRV:"):
            clean_line = line[4:]
            reader = csv.reader([clean_line])
            row = list(reader)[0]

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
        self.mkv_id = mkv_id          
        self.device_path = device_path 
        self.label = label            
        self.status = "IDLE"      
        self.current_job = ""         
        self.progress = 0             
        self.name = name              

def scan_drives():
    global drives
    print("Scanning drives... please wait...")
    try:
        drives_info = fetch_drive_info()
        drives = [Drive(d_id, d_dev, d_label, d_name) for d_id, d_name, d_label, d_dev in drives_info]
    except Exception as e:
        print(f"Error during scan: {e}")
        drives = []

def rescan_idle_drives():
    print("Rescanning drives...")
    try:
        drives_info = fetch_drive_info()
    except Exception:
        return

    info_by_device = {d_dev: (d_id, d_name, d_label) for d_id, d_name, d_label, d_dev in drives_info}

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
            drive.label = ""
            if drive.status in ["COMPLETED", "ERROR", "ERROR (META)"]:
                drive.status = "IDLE"

    for d_dev, (d_id, d_name, d_label) in info_by_device.items():
        if not any(d.device_path == d_dev for d in drives):
            drives.append(Drive(d_id, d_dev, d_label, d_name))
    time.sleep(1)

def parse_duration_to_seconds(duration_str):
    parts = duration_str.split(':')
    seconds = 0
    try:
        if len(parts) == 3: seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2: seconds = int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 1: seconds = int(parts[0])
    except (ValueError, IndexError): return 0
    return seconds

def get_disc_tracks(drive, min_len_seconds):
    cmd = [MAKEMKV_CMD, "-r", "--cache=1", "info", f"disc:{drive.mkv_id}"]
    try:
        creation_flags = 0x08000000 if os.name == 'nt' else 0
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=600, creationflags=creation_flags)
    except Exception as e:
        print(f"Error getting track info: {e}")
        return []

    tracks = {}
    for line in result.stdout.splitlines():
        if not line.startswith("TINFO:"): continue
        try:
            parts = line.split(":", 1)[1].split(",", 3)
            track_id = int(parts[0])
            attribute_id = int(parts[1])
            value = parts[3].strip('"')

            if track_id not in tracks: tracks[track_id] = {'id': track_id}

            if attribute_id == 9: 
                tracks[track_id]['duration_str'] = value
                tracks[track_id]['duration_seconds'] = parse_duration_to_seconds(value)
            elif attribute_id == 11: tracks[track_id]['size_bytes'] = int(value)
            elif attribute_id == 27: tracks[track_id]['output_filename'] = value
        except: continue

    valid_tracks = [t for t in tracks.values() if t.get('duration_seconds', 0) >= min_len_seconds]
    return sorted(valid_tracks, key=lambda x: x['id'])

def parse_track_selection(selection_str):
    selection = set()
    selection_str = ''.join(selection_str.split())
    if not selection_str: return []
    try:
        for part in selection_str.split(','):
            if not part: continue
            if '-' in part:
                start, end = map(int, part.split('-'))
                if start > end: start, end = end, start
                selection.update(range(start, end + 1))
            else:
                selection.add(int(part))
    except ValueError: return []
    return sorted(list(selection))

def rip_jobs_worker(drive, jobs):
    drive.status = "Starting..."
    drive.progress = 0
    total_jobs = len(jobs)
    original_job_name = os.path.splitext(os.path.basename(jobs[0][1] if jobs else "Unknown Job"))[0]

    for i, (track_id, final_output_path, source_mkv_filename) in enumerate(jobs):
        job_progress_text = f"({i+1}/{total_jobs})"
        drive.status = f"Ripping {job_progress_text}"
        drive.current_job = os.path.basename(final_output_path)

        target_dir = os.path.dirname(final_output_path)
        os.makedirs(target_dir, exist_ok=True)
        
        cmd = [MAKEMKV_CMD, "-r", "--progress=-same", "mkv", f"disc:{drive.mkv_id}", str(track_id), target_dir]
        
        log_file = os.path.join(os.path.dirname(jobs[0][1]), "rip.log")

        creation_flags = 0x08000000 if os.name == 'nt' else 0

        with open(log_file, "a") as f:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, creationflags=creation_flags)
            f.write(f"\n--- Starting Job {job_progress_text}: Rip Track {track_id} ---\n")
            
            for line in proc.stdout:
                f.write(line)
                if line.startswith("PRGV:"):
                    try:
                        parts = line.split(":")[1].strip().split(",")
                        current, total = int(parts[0]), int(parts[2])
                        drive.progress = int((current / total) * 100)
                    except: pass
            
            proc.wait()
            drive.status = f"Processing {job_progress_text}"

            if proc.returncode == 0:
                try:
                    created_file = os.path.join(target_dir, source_mkv_filename)
                    if not os.path.exists(created_file):
                        raise FileNotFoundError(f"Missing: {created_file}")

                    if os.path.exists(final_output_path):
                        base, ext = os.path.splitext(final_output_path)
                        final_output_path = f"{base}_{int(time.time())}{ext}"

                    os.rename(created_file, final_output_path)
                    
                    # Metadata title (requires mkvpropedit in PATH)
                    try:
                        mkv_cmd = "mkvpropedit" # Assumed in PATH
                        subprocess.run([mkv_cmd, final_output_path, "--edit", "info", "--set", f"title={os.path.splitext(os.path.basename(final_output_path))[0]}"], capture_output=True, creationflags=creation_flags)
                    except FileNotFoundError:
                        drive.status = "WARN (META)"
                except Exception as e:
                    f.write(f"ERROR: {e}\n")
                    drive.status = "ERROR (Post)"
                    break 
            else:
                drive.status = "ERROR (Rip)"
                break 

    if not drive.status.startswith("ERROR"):
        drive.status = "COMPLETED"
        drive.current_job = original_job_name 
        eject_drive(drive.device_path)
    
    drive.progress = 0
    time.sleep(3)

def print_ui():
    clear_screen()
    print("=" * 75)
    print("                   MULTI-DRIVE RIPPING STATION (CROSS-PLATFORM)")
    print("=" * 75)
    print(f"{'ID':<5} | {'Device':<10} | {'Name':<15} | {'Status':<15} | {'Disc / Job'}")
    print("-" * 75)
    
    for d in drives:
        status_text = d.status
        if d.status.startswith("Ripping"):
            status_text = f"{d.status} ({d.progress}%)"
        info_text = d.current_job if d.current_job and d.status not in ["IDLE", ""] else d.label
        print(f"{d.mkv_id:<5} | {d.device_path:<10} | {d.name:<15} | {status_text:<15} | {info_text}")
        
    print("-" * 75)
    print(" [0-9] Rip ID  |  [r] Rescan  |  [q] Quit")
    print("-" * 75)

def main():
    scan_drives()
    
    last_snapshot = None
    last_render = 0
    force_render = True
    
    while True:
        snapshot = tuple((d.mkv_id, d.device_path, d.label, d.status, d.progress, d.current_job) for d in drives)
        now = time.time()
        
        if force_render or snapshot != last_snapshot or (now - last_render) >= REFRESH_INTERVAL:
            print_ui()
            print("Command: ", end="", flush=True)
            last_snapshot = snapshot
            last_render = now
            force_render = False

        user_input = None
        
        # --- INPUT HANDLING CROSS PLATFORM ---
        if os.name == 'nt':
            # WINDOWS: Polling loop
            end_wait = time.time() + 1.0
            while time.time() < end_wait:
                if msvcrt.kbhit():
                    try:
                        # Lese ein Zeichen (für Menübefehle)
                        char = msvcrt.getwche()
                        if char in ['\r', '\n']: 
                            print(); break
                        user_input = char
                        # Warte kurz, damit User Enter drücken kann falls gewohnt, aber hier reagieren wir direkt
                        time.sleep(0.2) 
                        # Wenn noch mehr im Buffer ist (z.B. Enter gedrückt), leeren
                        while msvcrt.kbhit(): msvcrt.getwch()
                        break
                    except: pass
                time.sleep(0.05)
        else:
            # LINUX: Select
            readable, _, _ = select.select([sys.stdin], [], [], 1.0)
            if readable:
                line = sys.stdin.readline()
                if line: user_input = line.strip()

        if user_input:
            user_input = str(user_input).strip()
            
            if user_input.lower() == 'q':
                sys.exit(0)
            elif user_input.lower() == 'r':
                rescan_idle_drives()
                force_render = True
            elif user_input.isdigit():
                selected_drive = next((d for d in drives if d.mkv_id == int(user_input)), None)
                if selected_drive and selected_drive.status in ["IDLE", "COMPLETED", "ERROR", "ERROR (META)", "", "WARN (META)"]:
                    
                    clear_screen()
                    print(f"--- Setup Drive {selected_drive.mkv_id} ---")
                    media_type = input("Movie (m) or Series (s)? ").lower()
                    
                    min_len = 3600 if media_type == 'm' else 1200
                    if media_type not in ['m', 's']:
                        force_render = True; continue

                    print("Scanning tracks...")
                    tracks = get_disc_tracks(selected_drive, min_len)
                    if not tracks:
                        print("No tracks found."); time.sleep(2); force_render=True; continue
                    
                    jobs = []
                    if media_type == 's':
                        print(f"\nTracks (> {min_len//60} min):")
                        for t in tracks:
                            print(f"ID {t['id']:<3} | {t.get('duration_str','?'):<8} | {t.get('output_filename','')}")
                        
                        sel = input("Tracks (e.g. 1,3-5): ")
                        t_ids = parse_track_selection(sel)
                        final_tracks = [t for t in tracks if t['id'] in t_ids]
                        
                        if final_tracks:
                            s_name = input("Series Name: ")
                            s_num = int(input("Season: "))
                            ep_start = int(input("Start Ep: "))
                            
                            for i, tr in enumerate(final_tracks):
                                fname = f"{s_name}.S{s_num:02d}E{ep_start+i:02d}.mkv"
                                out = os.path.join(BASE_OUTPUT_DIR, s_name, f"Season {s_num:02d}", fname)
                                jobs.append((tr['id'], out, tr.get('output_filename', '')))
                    else:
                        longest = max(tracks, key=lambda t: t.get('duration_seconds', 0))
                        print(f"Selected: {longest.get('duration_str')} ({longest.get('output_filename')})")
                        m_name = input("Movie Name: ")
                        if m_name:
                            out = os.path.join(BASE_OUTPUT_DIR, m_name, f"{m_name}.mkv")
                            jobs.append((longest['id'], out, longest.get('output_filename', '')))

                    if jobs:
                        t = threading.Thread(target=rip_jobs_worker, args=(selected_drive, jobs))
                        t.daemon = True
                        t.start()
                    force_render = True
                else:
                    print("Invalid drive or busy."); time.sleep(1); force_render=True
            force_render = True

if __name__ == "__main__":
    main()
    