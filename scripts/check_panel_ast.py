#!/usr/bin/env python3
"""AST allowlist for lib/zouinify/panel/*.py -- the merge gate of the panel tickets (desk, T-0160).

A regex denylist cannot stop string tricks (`sys.modules['o'+'s']`, `getattr(mod, 'sys'+'tem')`), so the
panel package is checked structurally: every Python file under it may only
  - import the allowlisted modules (http, http.server, json, pathlib, os [os.path only], time, datetime,
    re, html, hashlib, threading, urllib.parse, zouinify.readonly, zouinify.panelstate, and its own package);
  - never call getattr/setattr/delattr/__import__/exec/eval/compile/globals/locals/vars/breakpoint/input;
  - never open a file for writing (open()/.open() with a non-read or non-literal mode);
  - never touch an attribute that starts with `_` (so `readonly._tmux` is impossible), nor one named
    os/subprocess/sys/ticket_sh/kill/pthread_kill/system/popen/spawn*/exec*/modules/write_text/...;
  - never use `os.` beyond `os.path` / `os.sep`.
With no way to spawn, import dynamically or write, a click cannot become a launch, a kill, a keystroke,
a ticket move or a deploy, whatever string the code builds.

Usage: scripts/check_panel_ast.py [dir ...]   (default: lib/zouinify/panel); exit 1 on any violation.
"""
from __future__ import annotations

import ast
import os
import sys

ALLOWED_MODULES = {
    "__future__", "http", "http.server", "json", "pathlib", "os", "os.path", "time", "datetime", "re", "html",
    "hashlib", "threading", "urllib.parse", "zouinify.readonly", "zouinify.panelstate",
}
ALLOWED_PACKAGE_PREFIXES = ("zouinify.panel",)
BANNED_CALLS = {"getattr", "setattr", "delattr", "__import__", "exec", "eval", "compile", "globals", "locals", "vars",
                "breakpoint", "input", "exit", "quit", "help", "memoryview"}
BANNED_ATTRS = {
    "os", "subprocess", "sys", "ticket_sh", "kill", "pthread_kill", "raise_signal", "system", "popen", "spawn", "spawnl",
    "spawnle", "spawnlp", "spawnv", "spawnve", "spawnvp", "exec", "execl", "execle", "execlp", "execv", "execve", "execvp",
    "execvpe", "posix_spawn", "fork", "forkpty", "modules", "write_text", "write_bytes", "unlink", "rmdir", "rename",
    "chmod", "chown", "mkdir", "makedirs", "touch", "symlink", "link", "socket", "connect", "connect_ex", "send_keys",
    "new_session", "kill_session", "launch", "import_module", "load_module", "builtins", "run_path", "run_module",
    "interact", "startfile", "truncate", "environ", "putenv", "unsetenv", "fdopen", "openpty", "sendfile",
}
ZOUINIFY_NAMES = {"readonly", "panelstate"}   # the only `from zouinify import ...` names
ALLOWED_DUNDERS = {"__name__", "__file__", "__doc__", "__future__", "__init__", "__enter__", "__exit__", "__str__", "__repr__",
                   "__eq__", "__hash__", "__iter__", "__len__", "__contains__", "__getitem__", "__setitem__", "__call__"}
OS_ALLOWED = {"path", "sep", "linesep", "pathsep", "curdir", "pardir", "extsep"}
READ_MODES = {"r", "rb", "rt", "br", "tr"}


def module_allowed(name: str, level: int) -> bool:
    if level > 0:
        return True  # a relative import inside the package (its target is checked as a file of its own)
    if name in ALLOWED_MODULES:
        return True
    return any(name == p or name.startswith(p + ".") for p in ALLOWED_PACKAGE_PREFIXES)


class Checker(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.problems: list = []

    def flag(self, node, why: str) -> None:
        self.problems.append(f"{self.path}:{getattr(node, 'lineno', 0)}: {why}")

    def visit_Import(self, node):
        for a in node.names:
            if not module_allowed(a.name, 0):
                self.flag(node, f"`import {a.name}` is not on the panel allowlist")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        mod = node.module or ""
        if mod == "zouinify" and node.level == 0:
            for a in node.names:
                if a.name not in ZOUINIFY_NAMES:
                    self.flag(node, f"`from zouinify import {a.name}`: the panel may import only {sorted(ZOUINIFY_NAMES)}")
        elif not module_allowed(mod, node.level):
            self.flag(node, f"`from {'.' * node.level}{mod} import ...` is not on the panel allowlist")
        else:
            for a in node.names:
                if a.name == "*":
                    self.flag(node, "star import")
                if a.name.startswith("_") and a.name not in ALLOWED_DUNDERS:
                    self.flag(node, f"import of a private name {a.name}")
                if mod == "os" and a.name not in OS_ALLOWED:
                    self.flag(node, f"`from os import {a.name}`: only os.path is allowed")
                if a.name in BANNED_ATTRS:
                    self.flag(node, f"import of a banned name {a.name}")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr.startswith("_") and node.attr not in ALLOWED_DUNDERS:
            self.flag(node, f"private attribute .{node.attr}")
        if node.attr in BANNED_ATTRS:
            self.flag(node, f"attribute .{node.attr} is banned in the panel")
        if isinstance(node.value, ast.Name) and node.value.id == "os" and node.attr not in OS_ALLOWED:
            self.flag(node, f"os.{node.attr}: only os.path is allowed")
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id.startswith("__") and node.id not in ALLOWED_DUNDERS:
            self.flag(node, f"dunder name {node.id}")
        if node.id in ("subprocess", "sys", "importlib", "socket", "signal", "ctypes", "runpy", "code", "webbrowser", "pty",
                       "shlex", "tempfile", "glob", "builtins"):
            self.flag(node, f"name {node.id} is banned in the panel")
        if node.id in BANNED_CALLS:
            # a reference, not only a call: `threading.Thread(target=exec, ...)` or `f = getattr` would pass otherwise
            self.flag(node, f"reference to {node.id} is banned")
        self.generic_visit(node)

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Name):
            if f.id == "open":
                self.check_open(node)
        elif isinstance(f, ast.Attribute) and f.attr == "open":
            self.check_open(node, method=True)
        self.generic_visit(node)

    def check_open(self, node, method: bool = False) -> None:
        mode = node.args[1] if len(node.args) >= 2 and not method else (node.args[0] if method and node.args else None)
        for kw in node.keywords:
            if kw.arg == "mode":
                mode = kw.value
        if mode is None:
            return
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and mode.value in READ_MODES:
            return
        self.flag(node, "open() with a mode that is not a literal read mode")

    def visit_With(self, node):
        self.generic_visit(node)


def check_file(path: str) -> list:
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        return [f"{path}:{e.lineno}: syntax error: {e.msg}"]
    c = Checker(path)
    c.visit(tree)
    return c.problems


def check_dir(d: str) -> tuple:
    problems, files = [], 0
    for root, _dirs, names in os.walk(d):
        for n in sorted(names):
            if n.endswith(".py"):
                files += 1
                problems += check_file(os.path.join(root, n))
    return problems, files


def main(argv) -> int:
    dirs = argv or ["lib/zouinify/panel"]
    rc = 0
    for d in dirs:
        if not os.path.isdir(d):
            print(f"panel ast: {d} does not exist, nothing to check")
            continue
        problems, files = check_dir(d)
        if problems:
            print(f"panel ast FAIL: {len(problems)} violation(s) in {d}:")
            for p in problems:
                print("  " + p)
            rc = 1
        else:
            print(f"panel ast ok: {files} file(s) under {d} use only the allowlisted imports, no dynamic import, no exec, no write, no private attribute")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
