"""
run_scraper.py
──────────────
Entrypoint for the Docker container. Changes into WORK_DIR (the mounted
Dropbox folder) so all relative paths in your existing scripts work
identically to running them on your Windows PC.

Calls the same pipeline as you run manually:
  1. housemartin_scraper.py  — login, scrape balances, download CSVs
  2. irr5.py                 — XIRR + HTML report + snapshot
"""

import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime

WORK_DIR = os.environ.get("WORK_DIR", "/dropbox_data")

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def run(script: str, *args):
    """Run a Python script in WORK_DIR, streaming output live."""
    cmd = [sys.executable, "-u", script, *args]
    log(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=WORK_DIR)
    if result.returncode != 0:
        log(f"✗  {script} exited with code {result.returncode}")
        sys.exit(result.returncode)
    log(f"✓  {script} finished OK")

def main():
    log(f"=== Housemartin scraper starting (pid {os.getpid()}) ===")
    log(f"Working directory: {WORK_DIR}")

    work = Path(WORK_DIR)
    if not work.exists():
        log(f"✗  WORK_DIR does not exist: {WORK_DIR}")
        log("    Check that the Docker volume mount is correct.")
        sys.exit(1)

    os.chdir(WORK_DIR)
    log(f"cwd set to: {os.getcwd()}")

    # ── Step 1: scrape ────────────────────────────────────────────────────────
    #run("housemartin_scraper.py")
    run("run.py")

    # ── Step 2: XIRR + report ─────────────────────────────────────────────────
    # irr5.py is your existing XIRR / HTML report / snapshot script.
    # It reads the CSVs written by the scraper and produces the HTML report.
    #run("irr5.py")

    # Write a sentinel file in the root of the synced folder.
    # Synology Cloud Sync watches the root reliably via inotify — touching a file
    # here triggers a rescan of subdirectories including reports/ and snapshots/.
    import time
    time.sleep(3)
    sentinel = work / ".last_run"
    sentinel.write_text(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    log(f"Sentinel updated: {sentinel}")
    log("=== All done! Files written to Dropbox folder — will sync shortly. ===")

if __name__ == "__main__":
    main()
