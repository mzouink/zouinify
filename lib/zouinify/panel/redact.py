"""Redaction of captured screens and log tails before they are stored or served. Label-based (the value
after `aws_secret_access_key =`, `passcode:`, `WEB_TOKEN=` ...) plus shape-based (API keys, access key
ids, bearer tokens, JWTs). The panel never reads credential files; this is for what a pane shows."""
from __future__ import annotations

import re

LABEL_RE = re.compile(
    r"(?i)\b(aws_secret_access_key|aws_session_token|aws_access_key_id|web_token|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|secret|passcode|password|passwd|authorization|token)\b(\s*[=:]\s*)(?!bearer\b)(\S+)")
SHAPES = [
    (re.compile(r"(?i)\bBearer\s+\S+"), "Bearer <redacted>"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "<redacted>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "<redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "<redacted>"),
]


def redact(text: str) -> str:
    if not text:
        return text
    out = LABEL_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    for rx, rep in SHAPES:
        out = rx.sub(rep, out)
    return out
