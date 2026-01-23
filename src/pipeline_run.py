#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys
from pathlib import Path

from env_bootstrap import load_repo_env

load_repo_env()

ROOT = Path(__file__).resolve().parent.parent

def run(cmd):
    print(">>", " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT, text=True)
    if r.returncode != 0:
        raise SystemExit(r.returncode)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.getenv("XAIO_CONFIG_PATH", "config.yaml"))
    args = ap.parse_args()
    cfg = args.config

    py = sys.executable  # venv python when run from systemd
    run([py, "src/agent.py", "--config", cfg])
    run([py, "src/condense_queue.py", "--config", cfg])
    run([py, "src/ai_queue.py", "--config", cfg])
    run([py, "src/wp_upload_queue.py", "--config", cfg])

if __name__ == "__main__":
    main()
