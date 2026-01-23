#!/usr/bin/env python3
"""src/env_bootstrap.py

Single, predictable place to load runtime configuration.

**Canonical local configuration directory** (gitignored):

  .runtime/
    .env                          # all environment variables & secrets
    config.yaml                   # runtime config
    secrets/service_account.json  # Google service account credentials

The intent is that you only ever edit files inside `.runtime/`.

This loader keeps backward compatibility with older env file locations, but
` .runtime/.env ` is the primary source of truth going forward.

Env loading order (highest precedence first):
  1) $XAIO_ENV_FILE                 (if set)
  2) <runtime>/.env                 (recommended)
  3) <repo_root>/.env               (legacy)
  4) <runtime>/xaio-url-agent.env   (legacy)
  5) ~/.config/xaio-url-agent.env   (legacy)

Later files only fill in missing variables (they do NOT override values that are
already set).

Additionally, this module sets sane defaults for:
  - XAIO_PROJECT_DIR
  - XAIO_RUNTIME_DIR
  - XAIO_CONFIG_PATH
  - GOOGLE_SERVICE_ACCOUNT_JSON

Call `load_repo_env()` near the top of every entrypoint.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

_ENV_LOADED = False


def repo_root() -> Path:
    """Return the repository root (parent of `src/`)."""
    return Path(__file__).resolve().parent.parent


def runtime_dir() -> Path:
    """Return the runtime directory (defaults to <repo>/.runtime)."""
    raw = (os.getenv("XAIO_RUNTIME_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return repo_root() / ".runtime"


def _home_config_env() -> Path:
    return Path.home() / ".config" / "xaio-url-agent.env"


def _iter_env_candidates() -> Iterable[Path]:
    root = repo_root()
    rt = runtime_dir()

    override = (os.getenv("XAIO_ENV_FILE") or "").strip()
    if override:
        yield Path(override).expanduser().resolve()

    yield rt / ".env"
    yield root / ".env"  # legacy
    yield rt / "xaio-url-agent.env"  # legacy
    yield _home_config_env()  # legacy


def _load_env_file(path: Path, *, override: bool = False) -> bool:
    """Load KEY=VALUE pairs into os.environ.

    If python-dotenv is available we use it; otherwise we fall back to a very
    small parser.

    - override=False means do not overwrite variables already set.
    - override=True means overwrite existing vars.
    """
    if not path.exists() or not path.is_file():
        return False

    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(dotenv_path=str(path), override=override)
        return True
    except Exception:
        # Minimal fallback parser: KEY=VALUE (no export, no multiline).
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if not key:
                continue
            if override:
                os.environ[key] = val
            else:
                os.environ.setdefault(key, val)
        return True


def _set_default(name: str, value: str) -> None:
    if not (os.getenv(name) or "").strip():
        os.environ[name] = value


def load_repo_env(*, force: bool = False) -> None:
    """Load the environment exactly once per process."""
    global _ENV_LOADED

    if _ENV_LOADED and not force:
        return

    root = repo_root()
    rt = runtime_dir()

    # Ensure these are always present.
    _set_default("XAIO_PROJECT_DIR", str(root))
    _set_default("XAIO_RUNTIME_DIR", str(rt))

    # Load env files. The explicit override file (XAIO_ENV_FILE) is allowed to
    # override existing values; everything else only fills in missing values.
    override_path = (os.getenv("XAIO_ENV_FILE") or "").strip()
    if override_path:
        _load_env_file(Path(override_path).expanduser().resolve(), override=True)

    # Fill missing values from known locations.
    for candidate in _iter_env_candidates():
        if override_path and candidate == Path(override_path).expanduser().resolve():
            continue
        _load_env_file(candidate, override=False)

    # Default config path.
    if not (os.getenv("XAIO_CONFIG_PATH") or "").strip():
        rt_cfg = rt / "config.yaml"
        root_cfg = root / "config.yaml"
        if rt_cfg.exists():
            os.environ["XAIO_CONFIG_PATH"] = str(rt_cfg)
        elif root_cfg.exists():
            os.environ["XAIO_CONFIG_PATH"] = str(root_cfg)

    # Default Google service account path.
    if not (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip():
        rt_sa = rt / "secrets" / "service_account.json"
        root_sa = root / "secrets" / "service_account.json"
        if rt_sa.exists():
            os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = str(rt_sa)
        elif root_sa.exists():
            os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = str(root_sa)

    _ENV_LOADED = True


def require_env(var_name: str) -> str:
    """Fetch an env var or raise a clear error."""
    val = (os.getenv(var_name) or "").strip()
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {var_name}. "
            f"Set it in .runtime/.env (recommended) or via XAIO_ENV_FILE."
        )
    return val


def path_from_env(var_name: str, *, must_exist: bool = False) -> Optional[Path]:
    raw = (os.getenv(var_name) or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    if must_exist and not p.exists():
        raise RuntimeError(f"{var_name} points to missing path: {p}")
    return p
