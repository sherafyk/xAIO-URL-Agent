from __future__ import annotations
from pathlib import Path

def load_repo_env() -> None:
    """
    Load environment variables from repo-local .runtime/xaio-url-agent.env if it exists.
    This makes manual runs + systemd runs deterministic.
    """
    try:
        from dotenv import load_dotenv
    except Exception:
        return

    repo_root = Path(__file__).resolve().parents[1]
    env_path = repo_root / ".runtime" / "xaio-url-agent.env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
