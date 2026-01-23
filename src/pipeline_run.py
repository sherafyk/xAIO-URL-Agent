#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def run(cmd):
    print(">>", " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT, text=True)
    if r.returncode != 0:
        raise SystemExit(r.returncode)

def main():
    py = sys.executable  # venv python when run from systemd
    run([py, "src/agent.py"])
    run([py, "src/condense_queue.py"])
    run([py, "src/ai_queue.py"])
    run([py, "src/wp_upload_queue.py"])

if __name__ == "__main__":
    main()

