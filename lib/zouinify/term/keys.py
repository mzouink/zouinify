"""The keystroke allowlist of the terminal page: printable text and a short list of named keys; nothing else can be sent.

A batch is `{"ops": [{"t": "text"} | {"k": "Enter"}, ...]}`, at most 50 ops; text is at most 2000 characters with no
control character (so no raw escape sequence, no zero-width character); named keys are exactly NAMED_KEYS -- never
C-d (ends the session), C-z, C-\\, a tmux prefix or an arbitrary key name."""
from __future__ import annotations

NAMED_KEYS = frozenset({"Enter", "Escape", "Up", "Down", "Left", "Right", "Tab", "BSpace", "Home", "End", "PageUp", "PageDown",
                        "Space", "C-c", "C-u"})
TEXT_MAX = 2000
OPS_MAX = 50


def parse_ops(payload) -> list:
    """The validated ops of a batch as [("t", text) | ("k", KeyName)], or ValueError with the reason."""
    if not isinstance(payload, dict) or not isinstance(payload.get("ops"), list):
        raise ValueError("ops: a list of {t: text} or {k: KeyName} items is required")
    ops = payload["ops"]
    if not ops:
        raise ValueError("ops: empty")
    if len(ops) > OPS_MAX:
        raise ValueError(f"ops: at most {OPS_MAX} items per batch")
    out = []
    prev = None
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or len(op) != 1:
            raise ValueError(f"ops[{i}]: exactly one of t or k")
        if "t" in op:
            t = op["t"]
            if not isinstance(t, str) or not t:
                raise ValueError(f"ops[{i}].t: a non-empty string")
            if len(t) > TEXT_MAX:
                raise ValueError(f"ops[{i}].t: at most {TEXT_MAX} characters")
            if not t.isprintable() or any(ord(c) < 32 or ord(c) == 127 for c in t):
                raise ValueError(f"ops[{i}].t: printable text only, no control or zero-width characters")
            if prev == ("k", "Escape") and t[0] in "[O":
                raise ValueError(f"ops[{i}].t: text starting with '{t[0]}' right after Escape would form an escape sequence")
            out.append(("t", t))
        elif "k" in op:
            k = op["k"]
            if not isinstance(k, str) or k not in NAMED_KEYS:
                raise ValueError(f"ops[{i}].k: not an allowed key; allowed: {' '.join(sorted(NAMED_KEYS))}")
            out.append(("k", k))
        else:
            raise ValueError(f"ops[{i}]: exactly one of t or k")
        prev = out[-1]
    return out
