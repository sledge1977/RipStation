import os
import select
import shutil
import sys
import time
import unicodedata
from typing import Any

if sys.platform == "win32":
    import msvcrt
else:
    import termios
    import tty

from ripstation.models import Drive, DriveStatus


def clear_screen() -> None:
    if os.name == "nt":
        os.system("cls")
    elif sys.stdout.isatty():
        # One write causes noticeably less flicker than spawning `clear`
        sys.stdout.write("\033[2J\033[H")


def command_needs_more_digits(digits: str, valid_ids: list[int] | None) -> bool:
    if valid_ids is None:
        return True
    prefix = str(digits)
    return any(
        str(drive_id).startswith(prefix) and len(str(drive_id)) > len(prefix)
        for drive_id in valid_ids
    )


def read_menu_command(
    timeout: float = 1.0,
    digit_timeout: float = 0.75,
    valid_ids: list[int] | None = None,
) -> str | None:
    """Read dashboard commands immediately while accepting multi-digit IDs."""
    if sys.platform == "win32":
        return _read_windows_command(timeout, digit_timeout, valid_ids)
    else:
        return _read_posix_command(timeout, digit_timeout, valid_ids)


if sys.platform == "win32":

    def _read_windows_command(
        timeout: float, digit_timeout: float, valid_ids: list[int] | None
    ) -> str | None:
        end_wait = time.time() + timeout
        while time.time() < end_wait:
            if msvcrt.kbhit():
                try:
                    char = msvcrt.getwche()
                except (OSError, UnicodeError):
                    return None
                if char in ("\r", "\n"):
                    return None
                if not char.isdigit():
                    return char
                digits = char
                if not command_needs_more_digits(digits, valid_ids):
                    return digits
                digit_deadline = time.time() + digit_timeout
                while len(digits) < 6 and time.time() < digit_deadline:
                    if not msvcrt.kbhit():
                        time.sleep(0.02)
                        continue
                    try:
                        next_char = msvcrt.getwche()
                    except (OSError, UnicodeError):
                        break
                    if next_char in ("\r", "\n"):
                        break
                    if not next_char.isdigit():
                        break
                    digits += next_char
                    if not command_needs_more_digits(digits, valid_ids):
                        break
                    digit_deadline = time.time() + digit_timeout
                return digits
            time.sleep(0.05)
        return None

else:

    def _read_posix_command(
        timeout: float, digit_timeout: float, valid_ids: list[int] | None
    ) -> str | None:
        if not sys.stdin.isatty():
            readable, _, _ = select.select([sys.stdin], [], [], timeout)
            if readable:
                line = sys.stdin.readline()
                return line.strip() if line else "q"
            return None

        previous_settings = termios.tcgetattr(sys.stdin)
        try:
            tty.setcbreak(sys.stdin.fileno())
            readable, _, _ = select.select([sys.stdin], [], [], timeout)
            if not readable:
                return None
            char = sys.stdin.read(1)
            if not char:
                return "q"
            if not char.isdigit():
                return char
            digits = char
            if not command_needs_more_digits(digits, valid_ids):
                return digits
            while len(digits) < 6:
                readable, _, _ = select.select([sys.stdin], [], [], digit_timeout)
                if not readable:
                    break
                next_char = sys.stdin.read(1)
                if next_char in ("\r", "\n", ""):
                    break
                if not next_char.isdigit():
                    break
                digits += next_char
                if not command_needs_more_digits(digits, valid_ids):
                    break
            return digits
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, previous_settings)


def display_width(value: Any) -> int:
    """Terminal cell width, including wide and combining Unicode characters."""
    width = 0
    for char in str(value):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def fit_text(value: Any, width: int, align: str = "left") -> str:
    """Truncate and pad text to exactly `width` terminal cells."""
    if width <= 0:
        return ""
    value = str(value)
    if display_width(value) > width:
        result = ""
        available = max(0, width - 1)
        for char in value:
            char_width = display_width(char)
            if display_width(result) + char_width > available:
                break
            result += char
        value = result + ("…" if width > 0 else "")

    padding = width - display_width(value)
    if align == "right":
        return (" " * padding) + value
    if align == "center":
        left = padding // 2
        return (" " * left) + value + (" " * (padding - left))
    return value + (" " * padding)


STATUS_TEXT = {
    DriveStatus.IDLE: "Bereit",
    DriveStatus.STARTING: "Startet",
    DriveStatus.RIPPING: "Rippe",
    DriveStatus.PROCESSING: "Verarbeite",
    DriveStatus.EJECTING: "Werfe aus",
    DriveStatus.COMPLETED: "Fertig",
    DriveStatus.COMPLETED_META_WARN: "Fertig · Metadatenwarnung",
    DriveStatus.CANCELLED: "Abgebrochen",
    DriveStatus.ERROR_START: "Fehler · Programmstart",
    DriveStatus.ERROR_RIP: "Fehler · Rip",
    DriveStatus.ERROR_POST: "Fehler · Nachbearbeitung",
    DriveStatus.ERROR_WORKER: "Fehler · Verarbeitung",
}


def status_text(drive: Drive) -> str:
    text = STATUS_TEXT[drive.status]
    if drive.job_step:
        text = f"{text} ({drive.job_step})"
    return text


def drive_info_text(drive: Drive) -> str:
    if drive.current_job and drive.status is not DriveStatus.IDLE:
        text = drive.current_job
    else:
        text = drive.label or "Kein Medium"
    if drive.message:
        text = f"{text} · {drive.message}"
    return text


def progress_text(drive: Drive, width: int) -> str:
    if drive.status is not DriveStatus.RIPPING or width < 8:
        return ""
    progress = max(0, min(100, int(drive.progress)))
    bar_width = max(1, width - 7)
    filled = round(bar_width * progress / 100)
    bar = ("█" * filled) + ("░" * (bar_width - filled))
    return f"[{bar}] {progress:3d}%"


def table_row(
    values: list[Any], widths: list[int], alignments: list[str] | None = None
) -> str:
    alignments = alignments or ["left"] * len(values)
    return " │ ".join(
        fit_text(value, width, alignment)
        for value, width, alignment in zip(values, widths, alignments, strict=True)
    )


def render_table(
    drive_list: list[Drive], width: int, height: int, wide: bool
) -> list[str]:
    if wide:
        widths = [3, 16, 18, 20, 13, width - 85]
        headers = [
            "ID",
            "Gerät",
            "Laufwerk",
            "Status",
            "Fortschritt",
            "Medium / Auftrag",
        ]
    else:
        widths = [3, 14, 20, width - 46]
        headers = ["ID", "Gerät", "Status", "Medium / Auftrag"]

    max_rows = max(1, height - 7)
    clipped = len(drive_list) > max_rows
    visible_count = max_rows - 1 if clipped else max_rows
    visible_drives = drive_list[: max(0, visible_count)]

    lines = [table_row(headers, widths), "─" * width]
    for drive in visible_drives:
        if wide:
            values = [
                drive.mkv_id,
                drive.device_path,
                drive.name,
                status_text(drive),
                progress_text(drive, widths[4]),
                drive_info_text(drive),
            ]
        else:
            state = status_text(drive)
            if drive.status is DriveStatus.RIPPING:
                state = f"{state} · {drive.progress}%"
            values = [drive.mkv_id, drive.device_path, state, drive_info_text(drive)]
        lines.append(
            table_row(values, widths, ["right"] + ["left"] * (len(widths) - 1))
        )

    if clipped:
        hidden = len(drive_list) - len(visible_drives)
        lines.append(fit_text(f"… {hidden} weitere Laufwerke", width))
    elif not drive_list:
        lines.append(
            fit_text("Keine Laufwerke erkannt · R zum erneuten Scannen", width)
        )
    return lines


def render_cards(drive_list: list[Drive], width: int, height: int) -> list[str]:
    budget = max(1, height - 5)
    if not drive_list:
        return [fit_text("Keine Laufwerke erkannt", width)]

    max_cards = budget // 2
    clipped = len(drive_list) > max_cards
    if clipped:
        max_cards = max(0, (budget - 1) // 2)

    lines: list[str] = []
    for drive in drive_list[:max_cards]:
        heading = f"[{drive.mkv_id}] {drive.device_path} · {status_text(drive)}"
        detail = f"    {drive.name} · {drive_info_text(drive)}"
        if drive.status is DriveStatus.RIPPING:
            detail = f"    {drive.progress}% · {drive_info_text(drive)}"
        lines.extend((fit_text(heading, width), fit_text(detail, width)))

    if clipped:
        lines.append(
            fit_text(f"… {len(drive_list) - max_cards} weitere Laufwerke", width)
        )
    return lines


def render_ui(
    drive_list: list[Drive] | None = None,
    width: int | None = None,
    height: int | None = None,
) -> str:
    """Return a dashboard adapted to terminal width and height."""
    items = [] if drive_list is None else list(drive_list)
    terminal = shutil.get_terminal_size(fallback=(80, 24))
    width = max(20, width if width is not None else terminal.columns)
    height = max(8, height if height is not None else terminal.lines)

    title = f"RIPSTATION · {len(items)} Laufwerk{'e' if len(items) != 1 else ''}"
    lines = [fit_text(title, width, "center"), "─" * width]
    if width >= 110:
        lines.extend(render_table(items, width, height, wide=True))
    elif width >= 68:
        lines.extend(render_table(items, width, height, wide=False))
    else:
        lines.extend(render_cards(items, width, height))
    lines.append("─" * width)
    footer = "[ID] Rip starten  ·  [C] Abbrechen  ·  [R] Neu scannen  ·  [Q] Beenden"
    if width < 72:
        footer = "[ID] Rip  ·  [C] Stop  ·  [R] Scan  ·  [Q] Ende"
    lines.append(fit_text(footer, width, "center"))
    return "\n".join(lines)


def print_ui(drive_list: list[Drive] | None = None) -> None:
    clear_screen()
    print(render_ui(drive_list))
