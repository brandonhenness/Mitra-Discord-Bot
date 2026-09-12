"""Create a new private directory before writing recovery credentials."""
import csv
import os
import subprocess


def private_directory(path):
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    if os.name == "nt":
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                check=True, capture_output=True, text=True)
        sid = next(csv.reader(result.stdout.strip().splitlines()))[-1]
        if not sid.startswith("S-1-") or any(c not in "S-0123456789" for c in sid):
            raise ValueError("Could not identify the Windows account for private folder permissions")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
                       check=True, capture_output=True)
