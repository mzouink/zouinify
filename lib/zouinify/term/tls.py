"""Self-signed HTTPS for the terminal page on the intranet (`terminal.tls: auto`): one certificate per project under
.ops/state/, made once with the `openssl` binary; without it the process refuses to bind the LAN unless `terminal.tls:
off` was set knowingly. The only module of the package that may run a program other than tmux (check_term_guard.sh)."""
from __future__ import annotations

import os
import shutil
import ssl
import subprocess

from zouinify import ZouinifyError
from zouinify import panelstate as state

NO_OPENSSL = ("terminal.tls is auto and `openssl` is not on PATH: the terminal page refuses to serve the LAN in clear text. Install "
              "openssl (the self-signed certificate is made once), or set `terminal.tls: off` in .ops/config.yaml knowingly: the "
              "passphrase then crosses the LAN in clear once per browser session and the unlock cookie on every request.")


def cert_paths(cfg) -> tuple:
    d = state.state_dir(cfg)
    return os.path.join(d, "term-cert.pem"), os.path.join(d, "term-key.pem")


def ensure_cert(cfg, host: str, names) -> tuple:
    """(cert, key): the project's self-signed pair, made now when missing (RSA 2048, 825 days, CN and SAN = the LAN names)."""
    cert, key = cert_paths(cfg)
    if os.path.isfile(cert) and os.path.isfile(key):
        return cert, key
    if shutil.which("openssl") is None:
        raise ZouinifyError(NO_OPENSSL)
    dns = sorted({str(n) for n in names if n and not str(n).replace(".", "").isdigit()} | {host})
    ips = sorted({str(n) for n in names if n and str(n).replace(".", "").isdigit()})
    san = ",".join([f"DNS:{n}" for n in dns] + [f"IP:{ip}" for ip in ips]) or f"DNS:{host}"
    os.makedirs(os.path.dirname(cert), exist_ok=True)
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "825", "-keyout", key, "-out", cert,
                        "-subj", f"/CN={host}", "-addext", f"subjectAltName={san}"], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise ZouinifyError(f"openssl could not make the terminal certificate: {(r.stderr or r.stdout).strip()[:300]}")
    os.chmod(key, 0o600)
    os.chmod(cert, 0o600)
    return cert, key


def context(cert: str, key: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx
