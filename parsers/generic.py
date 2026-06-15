"""Generic fallback parser — no protocol knowledge.

Same command/response split as the RAILtest parser: a command is shown the moment
it is announced, its reply is buffered and emitted as one indented block on the
next prompt.
"""
import re
from parsers import BaseParser

_PROMPT_RE = re.compile(r'^[^{]*>\s*$')


class GenericParser(BaseParser):
    name = "generic"

    def __init__(self):
        self._cmd = None   # {"cmd","ts","resp":[]}

    def detect(self, line):
        return False

    def notify_cmd(self, cmd, ts="", echo=True):
        cmd = (cmd or "").strip()
        if not cmd:
            return []
        return self._open(cmd, ts)

    def feed(self, line, ts=""):
        s = line.strip()
        if not s:
            return []
        if _PROMPT_RE.match(s):
            return self._flush()
        if self._cmd is not None:
            self._cmd["resp"].append(s)
            return []
        return [{"kind": "event", "lines": [s], "ts": ts}]

    def _open(self, cmd, ts):
        out = self._flush()
        self._cmd = {"cmd": cmd, "ts": ts, "resp": []}
        out.append({"kind": "cmd", "cmd": cmd, "ts": ts})
        return out

    def _flush(self):
        out = []
        if self._cmd is not None:
            if self._cmd["resp"]:
                out.append({"kind": "response", "cmd": self._cmd["cmd"],
                            "lines": self._cmd["resp"], "ts": self._cmd["ts"]})
            self._cmd = None
        return out

    def flush(self):
        return self._flush()

    def is_response_complete(self, lines):
        return False
