# check_locks.py
# Run this while the staging folder still exists to see what's holding it.
# Usage: python check_locks.py "C:\path\to\staging\folder"

import sys
import psutil

def find_locks(target_path: str):
    target_path = target_path.lower()
    found = False

    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            open_files = proc.open_files()
            for f in open_files:
                if target_path in f.path.lower():
                    print(f"PID {proc.pid} ({proc.name()}) holds: {f.path}")
                    found = True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not found:
        print("No locks found on that path.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python check_locks.py <path>")
        sys.exit(1)
    find_locks(sys.argv[1])