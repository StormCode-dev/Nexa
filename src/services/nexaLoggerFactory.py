# This program is part of Nexa, which provides logging capabilities to the codebase, as well as plugins.
# This allows for NexaLogger objects to be instantiated by aspects of the programming language. It all funnels into one direct
# log created by main.
# Under the MIT License.

import logging
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

_initialized = False

_TIMESTAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


def _read_first_timestamp(path: Path) -> str | None:
    """
    Read the first parseable timestamp from a log file.
    Returns the timestamp string (e.g. '2026-06-17 12:57:02') or None.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                match = _TIMESTAMP_RE.match(line.strip())
                if match:
                    return match.group(1)
    except Exception:
        pass
    return None


def _archive_latest(latest: Path, log_folder: Path) -> None:
    """
    Archive the contents of the latest/ folder to a timestamped subfolder.
    Checks watchdog.log first, then active.log for a timestamp.
    If no valid timestamp is found, deletes the contents instead.
    """
    if not latest.exists():
        return
    
    if not any(latest.iterdir()):
        return

    timestamp = None
    for candidate in ("watchdog.log", "active.log"):
        ts = _read_first_timestamp(latest / candidate)
        if ts:
            timestamp = ts
            break

    # Clean up unparseable or empty files regardless of outcome
    if timestamp is None:
        for f in latest.iterdir():
            try:
                f.unlink()
            except Exception:
                pass
        return

    # Sanitize timestamp for use as a folder name (colons aren't valid on Windows)
    folder_name = timestamp.replace(":", "-")
    archive_dir = log_folder / folder_name
    archive_dir.mkdir(parents=True, exist_ok=True)

    for f in latest.iterdir():
        try:
            shutil.move(str(f), str(archive_dir / f.name))
        except Exception:
            pass


def setup(config, is_daemon: bool = False):
    """
    Configure the root Nexa logger from a NexaConfig instance.
    Call once in main.py before anything else.

    is_daemon: if True, logs to latest/watchdog.log instead of latest/active.log.
    """
    global _initialized
    if _initialized:
        return

    level_str   = config.get("logging.level",            "INFO").upper()
    enable_file = config.get("logging.enableFileLogging",  True)
    log_folder  = Path(config.get("logging.logFolder",    "logs"))
    components  = config.get("logging.components",         {})

    root_level = getattr(logging, level_str, logging.INFO)

    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)-8s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    root = logging.getLogger("Nexa")
    root.setLevel(logging.DEBUG)
    root.propagate = False

    # --- File handler ---
    if enable_file:
        latest = log_folder / "latest"

        # Archive previous session before creating new files
        if not is_daemon:
            _archive_latest(latest, log_folder)

        latest.mkdir(parents=True, exist_ok=True)

        log_filename = "active.log" if is_daemon else "watchdog.log"
        log_path = latest / log_filename

        file_handler = logging.FileHandler(log_path, encoding="utf-8", mode="a")
        file_handler.setLevel(root_level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # --- Console handler ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(root_level)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    # --- Per-component levels ---
    for component, comp_level_str in components.items():
        comp_level = getattr(logging, comp_level_str.upper(), root_level)
        logging.getLogger(f"Nexa.{component}").setLevel(comp_level)

    _initialized = True
    log_target = str(latest / log_filename) if enable_file else "disabled"
    root.info(
        "Nexa logger initialised. Level=%s, file=%s",
        level_str,
        log_target
    )


def get_logger(name: str) -> logging.Logger:
    """
    Return a named child logger under the Nexa hierarchy.

    Usage:
        logger = nexaLoggerFactory.get_logger("rcon")
        logger.debug("Sent command: /say hello")
        # → [2025-01-04 12:33:01] [DEBUG   ] [Nexa.rcon] Sent command: /say hello
    """
    return logging.getLogger(f"Nexa.{name}")