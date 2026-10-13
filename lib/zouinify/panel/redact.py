"""Redaction of captured screens and log tails before they are stored or served. Label-based (the value
after `aws_secret_access_key =`, `"SecretAccessKey": "..."`, `passcode:`, `--password x`, `PGPASSWORD=x`),
shape-based (AWS key ids and 40-char secrets, API keys of several vendors, bearer/basic credentials, JWTs,
long base64 blobs), credentials in URLs and `curl -u`, and PEM private-key blocks. The panel never reads
credential files; this is for what a pane shows."""
from __future__ import annotations

import re

LABELS = (r"aws_secret_access_key|secret_access_key|aws_session_token|session_token|aws_access_key_id|access_key_id|"
          r"SecretAccessKey|SessionToken|AccessKeyId|web_token|api[_-]?key|access[_-]?token|refresh[_-]?token|"
          r"client[_-]?secret|secret|passcode|password|passwd|authorization|token")
VALUE = r"\"[^\"]*\"|'[^']*'|\S+"
SCHEMES = r"bearer|basic|digest|negotiate|ntlm|hoba|mutual|aws4-hmac-sha256"
# `label = value`, `label: value`, `"label": "value"`, `label='value'` (an auth scheme word is left to SCHEME_RE)
LABEL_RE = re.compile(rf"(?i)(\b(?:{LABELS})\b[\"']?\s*[=:]\s*)(?!(?:{SCHEMES})\b)({VALUE})")
# `--password hunter2`, `--token=x`
FLAG_RE = re.compile(rf"(?i)(--?[a-z_-]*(?:password|passcode|token|secret)[a-z_-]*)(\s+|=)({VALUE})")
# `PGPASSWORD=hunter2`, `PASSCODE abc`, `MY_API_TOKEN: x` (an env-style name in capitals, then its value)
ENV_RE = re.compile(rf"\b([A-Z0-9_]*(?:PASSCODE|PASSWORD|PASSWD|TOKEN|SECRET|APIKEY|API_KEY)[A-Z0-9_]*)(\s*[=:]\s*|\s+)({VALUE})")
# https://user:pw@host, postgres://user:pw@host, ...
URL_CRED_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s]+)@")
# curl -u user:pw, --user user:pw, --user=user:pw
CURL_USER_RE = re.compile(r"(?i)(\s(?:-u|--user)(?:\s+|=))([^\s:]+):(\S+)")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78]")
PROMPT_RE = re.compile(r"(?i)((?:Password|Passphrase|Passcode|Token|Secret)\s+for\s+'[^']*':|Enter (?:passphrase|password)[^:]*:)[ \t]*(\S+)?")
WRAP_RE = re.compile(r"^[ \t]*[A-Za-z0-9/+=_.\-]{8,}")
MARKER_RE = re.compile(r"<redacted-next-line>")
PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S)
PEM_OPEN_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*\Z", re.S)
SHAPES = [
    (re.compile(rf"(?i)\b({SCHEMES})\s+[A-Za-z0-9._~+/=-]{{4,}}(?:,\s*[a-z]+=\S+)*"), lambda m: f"{m.group(1)} <redacted>"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "<redacted>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "<redacted>"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<redacted>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "<redacted>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "<redacted>"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), "<redacted>"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), "<redacted>"),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"), "<redacted>"),
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
    out = ANSI_RE.sub("", text)
    out = PEM_RE.sub("<redacted private key>", out)
    out = PEM_OPEN_RE.sub("<redacted private key>", out)
    out = URL_CRED_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}:<redacted>@", out)
    out = CURL_USER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}:<redacted>", out)
    out = LABEL_RE.sub(lambda m: f"{m.group(1)}<redacted>", out)
    out = FLAG_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", out)
    out = ENV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", out)
    out = PROMPT_RE.sub(lambda m: f"{m.group(1)} <redacted>" if m.group(2) else f"{m.group(1)} <redacted-next-line>", out)
    for rx, rep in SHAPES:
        out = rx.sub(rep, out)
    return wrapped_lines(out)


def wrapped_lines(text: str) -> str:
    """A secret that was cut by the pane width continues on the next line: when a redaction ends a line (or a
    password prompt ended one), the leading token of the next line goes too."""
    lines = text.split("\n")
    for i in range(len(lines) - 1):
        cur = lines[i].rstrip()
        if cur.endswith("<redacted>") or cur.endswith("<redacted-next-line>"):
            if WRAP_RE.match(lines[i + 1]):
                lines[i + 1] = WRAP_RE.sub(lambda m: m.group(0)[: len(m.group(0)) - len(m.group(0).lstrip())] + "<redacted>", lines[i + 1], count=1)
    return MARKER_RE.sub("<redacted>", "\n".join(lines))
