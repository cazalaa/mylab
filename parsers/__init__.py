"""
mylab parser system.

A parser turns a stream of complete lines into *display blocks*:

    {"kind": "cmd",   "cmd": "rx 1", "lines": [...], "ts": "..."}
    {"kind": "event", "lines": [...],                "ts": "..."}
    {"kind": "boot",  "lines": [...],                "ts": "..."}

Parser API:
    detect(line)                  -> bool          (used by AutoParser)
    notify_cmd(cmd, ts, echo)     -> list[block]   (announce an outgoing command)
    feed(line, ts)                -> list[block]   (one complete line in)
    flush()                       -> list[block]   (close everything, on disconnect)
    is_response_complete(lines)   -> bool          (used by board.cli())

format_block(block) (see parsers.pretty) turns a block into the ready-to-render
dict sent to the browser.
"""
from __future__ import annotations

PARSERS: dict[str, type["BaseParser"]] = {}


class _Meta(type):
    def __init__(cls, name, bases, ns):
        super().__init__(name, bases, ns)
        if bases and getattr(cls, "name", "base") != "base":
            PARSERS[cls.name] = cls


class BaseParser(metaclass=_Meta):
    name = "base"

    def detect(self, line: str) -> bool:
        return False

    def notify_cmd(self, cmd: str, ts: str = "", echo: bool = True) -> list[dict]:
        return []

    def feed(self, line: str, ts: str = "") -> list[dict]:
        s = line.strip()
        return [{"kind": "event", "lines": [s], "ts": ts}] if s else []

    def flush(self) -> list[dict]:
        return []

    def is_response_complete(self, lines: list[str]) -> bool:
        return False


class AutoParser(BaseParser):
    """Thin wrapper that locks onto the RAILtest parser (the only protocol we
    speak today) and forwards everything to it."""
    name = "auto"

    def __init__(self):
        self._active: BaseParser = PARSERS.get("railtest", PARSERS["generic"])()

    @property
    def active_name(self):
        return self._active.name

    def detect(self, line):
        return self._active.detect(line)

    def notify_cmd(self, cmd, ts="", echo=True):
        return self._active.notify_cmd(cmd, ts=ts, echo=echo)

    def feed(self, line, ts=""):
        return self._active.feed(line, ts=ts)

    def flush(self):
        return self._active.flush()

    def is_response_complete(self, lines):
        return self._active.is_response_complete(lines)


def get_parser(name: str = "auto") -> BaseParser:
    name = (name or "auto").strip().lower()
    if name == "auto":
        return AutoParser()
    cls = PARSERS.get(name) or PARSERS.get("generic")
    return cls()


# Register concrete parsers
from parsers import generic, railtest  # noqa: E402,F401
from parsers.pretty import format_block  # noqa: E402,F401
