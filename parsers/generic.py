"""Generic fallback parser — no format knowledge, passes lines through as-is."""

from parsers import BaseParser, make_group
import re

_PROMPT_RE = re.compile(r'^[^{]*>\s*$')


class GenericParser(BaseParser):
    name = "generic"

    def detect(self, line: str) -> bool:
        return False

    def feed(self, line: str) -> list[dict]:
        stripped = line.strip()
        if _PROMPT_RE.match(stripped):
            return self.flush()
        return []

    def flush(self) -> list[dict]:
        return []

    def parse_line(self, line: str) -> dict:
        stripped = line.strip()
        t = "prompt" if _PROMPT_RE.match(stripped) else "unknown"
        return {"type": t, "cmd": None, "fields": {}, "raw": line, "display": line}

    def is_response_complete(self, lines: list[str]) -> bool:
        return False
