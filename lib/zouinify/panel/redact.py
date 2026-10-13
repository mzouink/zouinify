"""Redaction of captured screens and log tails before they are stored or served. Label-based (the value
after `aws_secret_access_key =`, `"SecretAccessKey": "..."`, `passcode:`, `--password x`, `PASSCODE x`) plus
shape-based (AWS key ids and 40-char secrets, API keys, bearer tokens, JWTs, long base64 blobs). The panel never
reads credential files; this is for what a pane shows."""
from __future__ import annotations

import re

LABELS = (r"aws_secret_access_key|secret_access_key|aws_session_token|session_token|aws_access_key_id|access_key_id|"
          r"SecretAccessKey|SessionToken|AccessKeyId|web_token|api[_-]?key|access[_-]?token|refresh[_-]?token|"
          r"client[_-]?secret|secret|passcode|password|passwd|authorization|token")
VALUE = r"\"[^\"]*\"|'[^']*'|\S+"
# `label = value`, `label: value`, `"label": "value"`, `label='value'`
LABEL_RE = re.compile(rf"(?i)(\b(?:{LABELS})\b[\"']?\s*[=:]\s*)(?!bearer\b)({VALUE})")
# `--password hunter2`, `--token=x`, `-p x` is left alone (too generic)
FLAG_RE = re.compile(rf"(?i)(--?[a-z_-]*(?:password|passcode|token|secret)[a-z_-]*)(\s+|=)({VALUE})")
# `PASSCODE abc`, `WEB_TOKEN abc` (an env-style name in capitals followed by its value)
ENV_RE = re.compile(rf"\b([A-Z0-9_]*(?:PASSCODE|PASSWORD|TOKEN|SECRET)[A-Z0-9_]*)(\s+)({VALUE})")
SHAPES = [
    (re.compile(r"(?i)\bBearer\s+\S+"), "Bearer <redacted>"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "<redacted>"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<redacted>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "<redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "<redacted>"),
    # a 40-char AWS-secret shape: mixed case plus a digit or /+ (so a 40-hex git sha is not touched)
    (re.compile(r"(?<![A-Za-z0-9/+=])(?=[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=]))(?=[A-Za-z0-9/+]{0,39}[A-Z])(?=[A-Za-z0-9/+]{0,39}[a-z])"
                r"(?=[A-Za-z0-9/+]{0,39}[0-9/+])[A-Za-z0-9/+]{40}"), "<redacted>"),
    # long base64 blobs (session tokens are hundreds of characters)
    (re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+]{80,}={0,2}(?![A-Za-z0-9/+=])"), "<redacted>"),
]


def redact(text: str) -> str:
    if not text:
        return text
    out = LABEL_RE.sub(lambda m: f"{m.group(1)}<redacted>", text)
    out = FLAG_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", out)
    out = ENV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", out)
    for rx, rep in SHAPES:
        out = rx.sub(rep, out)
    return out
