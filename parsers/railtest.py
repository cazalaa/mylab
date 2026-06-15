"""
RAILtest stream parser — clean rewrite (command/response split).

Emits four block kinds:
    {"kind": "cmd",      "cmd": "rx 1",            "ts": "..."}   # emitted immediately
    {"kind": "response", "cmd": "rx 1", "lines":[...], "ts": "..."}# emitted when complete
    {"kind": "event",    "lines": ["{{...}}"],      "ts": "..."}   # spontaneous, immediate
    {"kind": "boot",     "lines": ["{{(reset)...}}"],"ts": "..."}  # grouped

Key behaviours:
  * A command is shown the instant it is recognised (board echo for VCOM, or
    notify_cmd(echo=False) for ADMIN) — so you can see it left, before any reply.
  * Its reply is buffered and emitted as ONE indented `response` block when the
    prompt that ends it arrives.
  * Spontaneous events are emitted immediately (no prompt needed).
  * {{...}} structures are brace-counted, so TCP fragmentation is harmless.
"""
from __future__ import annotations
from collections import deque
import re

from parsers import BaseParser
from parsers.pretty import struct_name

_PROMPT_RE = re.compile(r'^\w*>\s*$')           # ">", "WSTK> "
_FUSED_RE  = re.compile(r'^(\w*>)\s+(\S.*)$')    # "WSTK> {{...}}"
_BOOT_CMDS = {"reset", "radio", "system", "pti"}


class RailtestParser(BaseParser):
    name = "railtest"

    def __init__(self):
        self._pending: deque = deque()    # commands awaiting echo (vcom)
        self._cmd: dict | None  = None     # open command grouping {"cmd","ts","resp":[]}
        self._boot: dict | None = None
        self._depth = 0
        self._struct = ""
        self._struct_ts = ""

    # ── detection ────────────────────────────────────────────────────────────
    def detect(self, line: str) -> bool:
        return '{' in line

    # ── command announcement ─────────────────────────────────────────────────
    def notify_cmd(self, cmd: str, ts: str = "", echo: bool = True) -> list[dict]:
        cmd = (cmd or "").strip()
        if not cmd:
            return []
        if echo:
            self._pending.append((cmd, ts))
            return []
        # admin: no echo will come — open and show the command now
        return self._open_cmd(cmd, ts)

    # ── line feed ────────────────────────────────────────────────────────────
    def feed(self, line: str, ts: str = "") -> list[dict]:
        if self._depth > 0:                       # inside an unbalanced {{...}}
            return self._feed_struct(line, ts)

        s = line.strip()
        if not s:
            return []

        # Fused "prompt + content"
        m = _FUSED_RE.match(s)
        if m and not s.startswith('{'):
            out = self._on_prompt()
            out += self.feed(m.group(2), ts)
            return out

        # Bare prompt → ends the current command's reply
        if _PROMPT_RE.match(s):
            return self._on_prompt()

        # Command echo (vcom)
        if self._pending and not s.startswith('{'):
            cmd, cmd_ts = self._pending[0]
            if s == cmd or s.lstrip('> ').strip() == cmd:
                self._pending.popleft()
                return self._open_cmd(cmd, cmd_ts or ts)

        # Structure
        if s.startswith('{') or s.startswith('#{'):
            return self._feed_struct(s.lstrip('#'), ts)

        # Plain text → part of the open reply, else a standalone event line
        if self._cmd is not None:
            self._cmd["resp"].append(s)
            return []
        return [{"kind": "event", "lines": [s], "ts": ts}]

    # ── structure accumulation ───────────────────────────────────────────────
    def _feed_struct(self, s: str, ts: str) -> list[dict]:
        if self._depth == 0:
            self._struct_ts = ts
        out = []
        for c in s:
            if c == '{':
                self._depth += 1
            elif c == '}' and self._depth > 0:
                self._depth -= 1
            self._struct += c
            if self._depth == 0 and self._struct.strip():
                out += self._on_struct(self._struct.strip(), self._struct_ts)
                self._struct = ""
        return out

    def _on_struct(self, struct: str, ts: str) -> list[dict]:
        name = struct_name(struct)

        if name in _BOOT_CMDS:
            if self._boot is None:
                out = self._flush_resp()
                self._boot = {"kind": "boot", "lines": [struct], "ts": ts}
                return out
            self._boot["lines"].append(struct)
            return []

        out = self._flush_boot()
        if self._cmd is not None:                 # reply to the open command
            self._cmd["resp"].append(struct)
            return out
        out.append({"kind": "event", "lines": [struct], "ts": ts})  # spontaneous
        return out

    # ── command / reply lifecycle ────────────────────────────────────────────
    def _open_cmd(self, cmd: str, ts: str) -> list[dict]:
        out = self._flush_boot()
        out += self._flush_resp()
        self._cmd = {"cmd": cmd, "ts": ts, "resp": []}
        out.append({"kind": "cmd", "cmd": cmd, "ts": ts})
        return out

    def _flush_resp(self) -> list[dict]:
        out = []
        if self._cmd is not None:
            if self._cmd["resp"]:
                out.append({"kind": "response", "cmd": self._cmd["cmd"],
                            "lines": self._cmd["resp"], "ts": self._cmd["ts"]})
            self._cmd = None
        return out

    def _flush_boot(self) -> list[dict]:
        if self._boot is not None:
            blk, self._boot = self._boot, None
            return [blk]
        return []

    def _on_prompt(self) -> list[dict]:
        out = self._flush_boot()
        out += self._flush_resp()
        return out

    def flush(self) -> list[dict]:
        if self._depth > 0 and self._struct.strip():
            self._on_struct(self._struct.strip(), self._struct_ts)
            self._struct, self._depth = "", 0
        return self._on_prompt()

    def is_response_complete(self, lines) -> bool:
        return bool(lines)
