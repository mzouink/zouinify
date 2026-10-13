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
FLAG_RE = re.compile(rf"(?i)(--?[a-z_-]*(?:password|passcode|token|secret|cookie)[a-z_-]*)(\s+|=)({VALUE})")
# `mysql -pS3cret`: a value glued to a lowercase -p (combined short flags such as `-pv` are shorter than 4 characters;
# a glued path or option such as `-p/tmp` is left alone)
GLUED_P_RE = re.compile(r"(?<![\w/.~-])-p(?![/~.-])(\S{4,})")
# `Cookie: a=b; c=d`, `Set-Cookie: sid=...; Path=/; HttpOnly`: every cookie VALUE goes, the names and the standard
# attributes stay (so `zouinify_token=<redacted>` still says which cookie it was, as T-0165's test pins)
COOKIE_RE = re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)([^\n]+)")
COOKIE_PAIR_RE = re.compile(r"([^=;\s]+)=([^;]*)")
COOKIE_ATTRS = {"path", "domain", "expires", "max-age", "samesite", "priority"}
# another project's screen (T-0175): e-mail addresses (a URL's user part, `//alice@host`, is not one), absolute or
# `~` paths of two or more segments, dotted host names of three or more labels that do not end in a file extension,
# IPv4 addresses
EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+/-])[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b")
PATH_RE = re.compile(r"(?<![\w.:/~-])(?:~|/[A-Za-z0-9._@+%-]+)(?:/[A-Za-z0-9._@+%-]+)+/?")
FILE_EXT = r"(?:js|mjs|cjs|py|md|txt|json|ya?ml|html?|css|sh|log|csv|pem|tf|tfvars|tsx?|jsx|lock|toml|ini|cfg|conf|xml|svg|png|jpe?g|gif|pdf|zip|tar|gz|whl|egg|so|example|tmpl|bak|orig)"
HOST_RE = re.compile(rf"(?i)\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.){{2,}}(?!{FILE_EXT}\b)[a-z]{{2,}}\b")
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# after redaction: a 32+ character base64-like run with upper case, lower case and a digit (a 40-hex sha has no upper
# case; shorter ids are not secrets), or a credential label whose value was not redacted
SUSPECT_RE = re.compile(r"(?<![A-Za-z0-9+/=])(?=[A-Za-z0-9+/=]{32,}(?![A-Za-z0-9+/=]))(?=[A-Za-z0-9+/=]*[A-Z])(?=[A-Za-z0-9+/=]*[a-z])"
                        r"(?=[A-Za-z0-9+/=]*[0-9])[A-Za-z0-9+/=]{32,}")
LABEL_LEFT_RE = re.compile(rf"(?i)\b(?:{LABELS})\b[\"']?\s*[=:]\s*(?!<redacted)[^\s<]")
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
    # the panel's own browser token (T-0165): the `#t=` address printed by `panel token rotate`, the cookie
    (re.compile(r"((?:(?:[#&?]|%23|%26|%3[Ff])t|zouinify_token)(?:=|%3[Dd]))[A-Za-z0-9_-]{32,256}"), r"\1<redacted>"),
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
    out = GLUED_P_RE.sub("-p<redacted>", out)
    out = COOKIE_RE.sub(lambda m: m.group(1) + cookie_pairs(m.group(2)), out)
    out = ENV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", out)
    out = PROMPT_RE.sub(lambda m: f"{m.group(1)} <redacted>" if m.group(2) else f"{m.group(1)} <redacted-next-line>", out)
    for rx, rep in SHAPES:
        out = rx.sub(rep, out)
    return wrapped_lines(out)


def cookie_pairs(header_value: str) -> str:
    """`sid=abc; Path=/; zouinify_token=x` -> `sid=<redacted>; Path=/; zouinify_token=<redacted>`."""
    return COOKIE_PAIR_RE.sub(lambda p: p.group(0) if p.group(1).lower() in COOKIE_ATTRS else f"{p.group(1)}=<redacted>", header_value)


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


def redact_foreign(text: str, keep_emails=()) -> str:
    """Another project's screen (T-0175): everything `redact` does, plus every e-mail address not in `keep_emails`
    (the human's own, from `panel.keep_emails`), absolute and `~` paths, dotted host names and IPv4 addresses."""
    if not text:
        return text
    keep = {k.lower() for k in keep_emails}
    out = redact(text)
    out = EMAIL_RE.sub(lambda m: m.group(0) if m.group(0).lower() in keep else "<redacted-email>", out)
    out = PATH_RE.sub("<path>", out)
    out = HOST_RE.sub("<host>", out)
    return IP_RE.sub("<ip>", out)


def suspicious(text: str) -> str:
    """After redaction: the reason a screen should be withheld when a secret-shaped string is still there, else ''."""
    if not text:
        return ""
    if SUSPECT_RE.search(text):
        return "a token-shaped string the redactor is not sure about"
    if LABEL_LEFT_RE.search(text):
        return "a credential label whose value the redactor did not catch"
    return ""
