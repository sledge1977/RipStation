import subprocess
import sys

# Keep console windows of child processes hidden on Windows.
if sys.platform == "win32":
    CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW
else:
    CREATE_NO_WINDOW = 0

# MakeMKV and MKVToolNix write UTF-8. Pass it explicitly (Windows would default
# to the ANSI code page) and use errors="replace" so a stray byte cannot abort a rip.
OUTPUT_ENCODING = "utf-8"


def ensure_process_stopped(
    proc: subprocess.Popen[str] | None, timeout: float = 10
) -> None:
    """Terminate and, if necessary, kill a child that did not exit normally."""
    if proc is None or getattr(proc, "returncode", None) is not None:
        return
    try:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    except OSError:
        return
