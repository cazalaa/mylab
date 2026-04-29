from __future__ import annotations

import os
import shutil
from pathlib import Path

# Keep your historical defaults (you can override via env or CLI)
DEFAULT_SDM = "/Users/jecloute/.silabs/slt/installs/archive/sdm-darwin-arm64/sdm"
DEFAULT_COMMANDER = "/Users/jecloute/.silabs/slt/installs/archive/Commander.app/Contents/MacOS/commander"


def _is_exec(p: str) -> bool:
    pp = Path(p)
    return pp.exists() and os.access(str(pp), os.X_OK)


def resolve_sdm(path: str | None) -> str | None:
    if path and _is_exec(path):
        return path
    envp = os.environ.get("SDM_BIN")
    if envp and _is_exec(envp):
        return envp
    if _is_exec(DEFAULT_SDM):
        return DEFAULT_SDM
    p = shutil.which("sdm")
    return p if p and _is_exec(p) else None


def resolve_commander(path: str | None) -> str | None:
    if path and _is_exec(path):
        return path
    envp = os.environ.get("COMMANDER_BIN")
    if envp and _is_exec(envp):
        return envp
    if _is_exec(DEFAULT_COMMANDER):
        return DEFAULT_COMMANDER
    p = shutil.which("commander")
    return p if p and _is_exec(p) else None
