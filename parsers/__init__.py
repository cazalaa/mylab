"""
mylab parser system — pluggable firmware protocol parsers.

Each parser handles one firmware format (RAILtest, custom app, …).
Registration is automatic: subclass BaseParser and it appears in PARSERS.

Usage:
    from parsers import get_parser

    parser = get_parser("auto")      # auto-detect from stream
    parser = get_parser("railtest")  # force a specific parser
    parser = get_parser("generic")   # raw fallback

    # Stateful feed interface (preferred):
    groups = parser.feed(line)   # returns list of Group dicts when ready
    groups = parser.flush()      # force-flush on prompt or disconnect

    # Low-level line interface (still usable):
    parsed = parser.parse_line(line)
    done   = parser.is_response_complete(accumulated_lines)

A Group dict:
    {
        "type"     : "event" | "response" | "boot" | "unknown",
        "category" : "rx" | "tx" | "reset" | "mode" | "error" | "info" | None,
        "color"    : "default" | "error" | "warning" | "success" | "boot" | None,
        "summary"  : str,           # one-line title
        "detail"   : str | None,    # multi-line body (None = not expandable)
        "raw"      : list[str],     # original lines
    }
"""

from __future__ import annotations
import re
from typing import Optional

# ── Registry ──────────────────────────────────────────────────────────────────

PARSERS: dict[str, type["BaseParser"]] = {}


class _Meta(type):
    def __init__(cls, name, bases, ns):
        super().__init__(name, bases, ns)
        if bases and cls.name != "base":
            PARSERS[cls.name] = cls


# ── Group helper ──────────────────────────────────────────────────────────────

def make_group(
    type_: str,
    summary: str,
    detail: str | None = None,
    raw: list[str] | None = None,
    category: str | None = None,
    color: str | None = None,
    cmd: str | None = None,
) -> dict:
    return {
        "type":     type_,
        "category": category,
        "color":    color or "default",
        "summary":  summary,
        "detail":   detail,
        "raw":      raw or [],
        "cmd":      cmd,
    }


# ── Base class ────────────────────────────────────────────────────────────────

class BaseParser(metaclass=_Meta):
    name = "base"

    def detect(self, line: str) -> bool:
        """Return True if line is a strong signal this parser matches."""
        return False

    def feed(self, line: str) -> list[dict]:
        """
        Feed one complete line.  Returns a (possibly empty) list of Group dicts
        that are ready to display.  Groups that span multiple lines are buffered
        internally and returned when complete (e.g. at prompt).
        """
        parsed = self.parse_line(line)
        if parsed["type"] == "prompt":
            return self.flush()
        if parsed["display"] != line:
            return [make_group("event", parsed["display"], raw=[line])]
        return []

    def flush(self) -> list[dict]:
        """Force-flush any buffered group (called at prompt or disconnect)."""
        return []

    def parse_line(self, line: str) -> dict:
        return {"type": "unknown", "cmd": None, "fields": {}, "raw": line, "display": line}

    def is_response_complete(self, lines: list[str]) -> bool:
        return False

    def format_line(self, parsed: dict) -> str:
        return parsed["display"]


# ── Factory ───────────────────────────────────────────────────────────────────

def get_parser(name: str = "auto") -> "BaseParser":
    if name == "auto":
        return AutoParser()
    cls = PARSERS.get(name) or PARSERS.get("generic")
    return cls()


# ── AutoParser ────────────────────────────────────────────────────────────────

class AutoParser(BaseParser):
    """Detects the firmware format from the stream and delegates to it."""
    name = "auto"

    def __init__(self):
        self._active: BaseParser = PARSERS["generic"]()
        self._locked = False

    @property
    def active_name(self) -> str:
        return self._active.name

    def _try_detect(self, line: str):
        if self._locked:
            return
        for pname, cls in PARSERS.items():
            if pname in ("generic", "auto"):
                continue
            probe = cls()
            if probe.detect(line):
                # Re-create the real instance (probe was just for detection)
                self._active = cls()
                self._locked = True
                return

    def detect(self, line: str) -> bool:
        return False

    def feed(self, line: str) -> list[dict]:
        self._try_detect(line)
        return self._active.feed(line)

    def flush(self) -> list[dict]:
        return self._active.flush()

    def parse_line(self, line: str) -> dict:
        self._try_detect(line)
        return self._active.parse_line(line)

    def is_response_complete(self, lines: list[str]) -> bool:
        return self._active.is_response_complete(lines)

    def format_line(self, parsed: dict) -> str:
        return self._active.format_line(parsed)


# ── import submodules so they self-register ───────────────────────────────────
from parsers import generic, railtest  # noqa: E402, F401
