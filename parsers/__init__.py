"""
mylab parser system.

Each parser implements feed(line) -> list[ParsedLine].

A ParsedLine dict:
    {
        "role"    : "cmd" | "response" | "event" | "boot" | "unknown" | "prompt",
        "cmd"     : str | None,      # RAILtest command name (rx, tx, rxPacket, ...)
        "display" : str,             # human-readable text
        "color"   : str,             # "default" | "error" | "warning" | "success" | "boot" | "script"
        "detail"  : str | None,      # expandable body (multi-response table, payload, boot details)
        "raw"     : str,             # original line
        "boot_id" : str | None,      # shared across boot sequence lines
        "boot_first": bool,          # True for first line of boot group
    }
"""

from __future__ import annotations
import re

PARSERS: dict[str, type["BaseParser"]] = {}


class _Meta(type):
    def __init__(cls, name, bases, ns):
        super().__init__(name, bases, ns)
        if bases and cls.name != "base":
            PARSERS[cls.name] = cls


class BaseParser(metaclass=_Meta):
    name = "base"

    def detect(self, line: str) -> bool:
        return False

    def feed(self, line: str) -> list[dict]:
        """Feed one complete line, return list of ParsedLine dicts."""
        return [_unknown(line)]

    def is_response_complete(self, lines: list[str]) -> bool:
        return False


def _parsed(role, display, raw, cmd=None, color="default",
            detail=None, boot_id=None, boot_first=False) -> dict:
    return {
        "role": role, "cmd": cmd, "display": display, "color": color,
        "detail": detail, "raw": raw, "boot_id": boot_id, "boot_first": boot_first,
    }

def _unknown(raw): return _parsed("unknown", raw.strip(), raw)
def _prompt(raw):  return _parsed("prompt",  raw.strip(), raw)


class AutoParser(BaseParser):
    name = "auto"

    def __init__(self):
        self._active: BaseParser = PARSERS["generic"]()
        self._locked = False

    @property
    def active_name(self): return self._active.name

    def _try_detect(self, line):
        if self._locked: return
        for pname, cls in PARSERS.items():
            if pname in ("generic", "auto"): continue
            if cls().detect(line):
                self._active = cls()
                self._locked = True
                return

    def feed(self, line):
        self._try_detect(line)
        return self._active.feed(line)

    def is_response_complete(self, lines):
        return self._active.is_response_complete(lines)


def get_parser(name="auto"):
    if name == "auto": return AutoParser()
    cls = PARSERS.get(name) or PARSERS.get("generic")
    return cls()


from parsers import generic, railtest  # noqa
