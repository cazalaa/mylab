"""Generic fallback — passes lines through as-is."""
import re
from parsers import BaseParser, _parsed, _prompt

_PROMPT_RE = re.compile(r'^[^{]*>\s*$')
_ECHO_RE   = re.compile(r'^>\s+\S')


class GenericParser(BaseParser):
    name = "generic"

    def detect(self, line): return False

    def feed(self, line):
        s = line.strip()
        if _PROMPT_RE.match(s): return [_prompt(line)]
        if _ECHO_RE.match(s):
            cmd = s[1:].strip()
            return [_parsed("cmd", cmd, line, cmd=cmd.split()[0])]
        if s: return [_parsed("unknown", s, line)]
        return []

    def is_response_complete(self, lines): return False
