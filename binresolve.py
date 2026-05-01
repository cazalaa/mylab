from __future__ import annotations

import os
import sys
import shutil
from pathlib import Path

SILABS_ROOT = Path.home() / ".silabs"
IS_WINDOWS = sys.platform == "win32"

# Noms d'exécutables selon la plateforme
_SDM_NAME = "sdm.exe" if IS_WINDOWS else "sdm"
_COMMANDER_NAME = "commander.exe" if IS_WINDOWS else "commander"

# Chemins par défaut (fallback legacy)
DEFAULT_SDM = str(Path.home() / ".silabs/slt/installs/archive/sdm-darwin-arm64/sdm")
DEFAULT_COMMANDER = str(Path.home() / ".silabs/slt/installs/archive/Commander.app/Contents/MacOS/commander")


def _is_exec(p: str) -> bool:
    pp = Path(p)
    if not pp.exists():
        return False
    if IS_WINDOWS:
        return True  # os.access X_OK non fiable sur Windows
    return os.access(str(pp), os.X_OK)


def _find_in_silabs(name: str) -> str | None:
    if not SILABS_ROOT.exists():
        return None
    candidates = sorted(SILABS_ROOT.rglob(name), key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in candidates:
        if _is_exec(str(candidate)):
            print(f"[INFO] {name} trouvé via ~/.silabs : {candidate}")
            return str(candidate)
    return None


def resolve_sdm(path: str | None) -> str | None:
    if path and _is_exec(path):
        return path
    envp = os.environ.get("SDM_BIN")
    if envp and _is_exec(envp):
        return envp
    if _is_exec(DEFAULT_SDM):
        return DEFAULT_SDM
    p = shutil.which("sdm")
    if p and _is_exec(p):
        return p
    return _find_in_silabs(_SDM_NAME)


def resolve_commander(path: str | None) -> str | None:
    if path and _is_exec(path):
        return path
    envp = os.environ.get("COMMANDER_BIN")
    if envp and _is_exec(envp):
        return envp
    if _is_exec(DEFAULT_COMMANDER):
        return DEFAULT_COMMANDER
    p = shutil.which("commander")
    if p and _is_exec(p):
        return p
    return _find_in_silabs(_COMMANDER_NAME)