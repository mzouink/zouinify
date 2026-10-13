#!/usr/bin/env python3
"""AST allowlist for lib/zouinify/panel/*.py -- the merge gate of the panel tickets (desk, T-0160).

A regex denylist cannot stop string tricks (`sys.modules['o'+'s']`, `getattr(mod, 'sys'+'tem')`), aliasing
(`o = open`, `m = os`) or walks through an allowed module into a dangerous one (`http.server.shutil`,
`re.functools.partial(open, mode="w")`), so the panel package is checked structurally. Every Python file
under it may only:
  - import the allowlisted modules, and from `zouinify` only `readonly` and `panelstate`;
  - use, on an allowlisted module, only the attributes on that module's list (so `http.client`,
    `http.server.shutil`, `re.functools`, `panelstate.tempfile`, `os.getpid` are out; `os.path.*` is in);
  - never use a module as a value (no aliasing, no passing modules around);
  - never reference getattr/setattr/delattr/__import__/exec/eval/compile/globals/locals/vars/...,
    nor `open` / `print` except as the function of a direct call; never `*args` / `**kwargs` in a call;
  - never open a file for writing (open()/.open() with a non-read or non-literal mode);
  - never touch an attribute that starts with `_`, nor one named os/subprocess/sys/ticket_sh/kill/
    replace/remove/write (except the response's `wfile.write`)/client/shutil/tempfile/...
With no way to spawn, import dynamically, alias or write, a click cannot become a launch, a kill, a
keystroke, a ticket move or a deploy, whatever string the code builds.

Usage: scripts/check_panel_ast.py [dir ...]   (default: lib/zouinify/panel); exit 1 on any violation.
"""
from __future__ import annotations

import ast
import os
import sys

ALL = "*"
READONLY_API = {
    "capture_pane", "list_panes", "has_session", "model_on_screen", "model_word", "alias_of", "trunk_sha", "proc_parent",
    "path_exists", "list_agents", "find_agent", "is_ticket_id", "ticket_files", "file_of", "front_matter", "freeze", "counts",
    "tickets_dir", "now_local", "next_sitting", "creds", "registry_entries", "registry_entry", "read_ticket_sh",
    "read_human_json", "read_board_json", "fail", "version", "TERMINAL", "ROLE_SESSIONS", "READ_ONLY_TICKET_SH",
}
PANELSTATE_API = {"version", "pid", "now_iso", "url", "state_dir", "panel_json_path", "server_log_path", "same_root", "log",
                  "write_panel_json", "read_panel_json", "remove_panel_json", "hook_installed"}
MODULES = {
    "__future__": {"annotations"},
    "http": {"HTTPStatus", "server"},
    "http.server": {"BaseHTTPRequestHandler", "ThreadingHTTPServer", "HTTPServer"},
    "json": {"dumps", "loads", "dump", "load", "JSONDecodeError"},
    "pathlib": {"Path", "PurePath", "PurePosixPath"},
    "os": {"path", "sep", "linesep", "pathsep", "curdir", "pardir", "extsep"},
    "os.path": ALL,
    "time": {"time", "monotonic", "sleep", "strftime", "gmtime", "localtime", "perf_counter"},
    "datetime": {"datetime", "timedelta", "timezone", "date", "time"},
    "re": {"compile", "match", "fullmatch", "search", "sub", "subn", "findall", "finditer", "split", "escape", "I", "M", "S", "X",
           "IGNORECASE", "MULTILINE", "DOTALL", "VERBOSE", "error", "Pattern"},
    "html": {"escape", "unescape"},
    "hashlib": {"sha1", "sha256", "sha512", "md5", "blake2b"},
    "threading": {"Thread", "Lock", "RLock", "Event", "current_thread", "Timer"},
    "urllib": {"parse"},
    "urllib.parse": {"urlsplit", "urlparse", "unquote", "unquote_plus", "quote", "parse_qs", "parse_qsl", "urlencode", "urljoin"},
    "zouinify.readonly": READONLY_API,
    "zouinify.panelstate": PANELSTATE_API,
}
ZOUINIFY_NAMES = {"readonly", "panelstate"}
PACKAGE = "zouinify.panel"
BANNED_NAMES = {
    "getattr", "setattr", "delattr", "__import__", "exec", "eval", "compile", "globals", "locals", "vars", "breakpoint", "input",
    "exit", "quit", "help", "memoryview", "subprocess", "sys", "importlib", "socket", "signal", "ctypes", "runpy", "code",
    "webbrowser", "pty", "shlex", "tempfile", "glob", "builtins", "functools", "shutil", "io", "selectors", "asyncio",
    "multiprocessing", "os_", "posix",
}
CALL_ONLY = {"open", "print"}   # fine as `open(...)` / `print(...)`, never as a value
BANNED_ATTRS = {
    "os", "subprocess", "sys", "ticket_sh", "kill", "killpg", "pthread_kill", "raise_signal", "system", "popen", "spawn", "spawnl",
    "spawnle", "spawnlp", "spawnv", "spawnve", "spawnvp", "exec", "execl", "execle", "execlp", "execv", "execve", "execvp",
    "execvpe", "posix_spawn", "posix_spawnp", "fork", "forkpty", "modules", "environ", "putenv", "unsetenv", "fdopen", "openpty",
    "sendfile", "write_text", "write_bytes", "write", "unlink", "rmdir", "rename", "renames", "replace", "remove", "removedirs",
    "chmod", "lchmod", "chown", "mkdir", "makedirs", "touch", "symlink", "symlink_to", "hardlink_to", "link", "socket", "connect",
    "connect_ex", "send_keys", "new_session", "kill_session", "launch", "import_module", "load_module", "builtins", "run_path",
    "run_module", "interact", "startfile", "truncate", "client", "shutil", "tempfile", "functools", "partial", "copy", "copyfile",
    "copytree", "move", "rmtree", "request", "urlopen", "Popen", "run", "call", "check_output", "check_call", "io", "excepthook",
    "settrace", "setprofile",
}
ALLOWED_DUNDERS = {"__name__", "__file__", "__doc__", "__future__", "__init__", "__enter__", "__exit__", "__str__", "__repr__",
                   "__eq__", "__hash__", "__iter__", "__len__", "__contains__", "__getitem__", "__setitem__", "__call__"}
READ_MODES = {"r", "rb", "rt", "br", "tr"}


def module_allowed(name: str) -> bool:
    return name in MODULES or name == PACKAGE or name.startswith(PACKAGE + ".")


class Checker(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.problems: list = []
        self.modules: dict = {}      # local name -> module path (for the attribute-chain check)
        self.called_attr: set = set()
        self.called_name: set = set()

    def flag(self, node, why: str) -> None:
        line = f"{self.path}:{getattr(node, 'lineno', 0)}: {why}"
        if line not in self.problems:
            self.problems.append(line)

    # -- imports ----------------------------------------------------------------------------------------
    def visit_Import(self, node):
        for a in node.names:
            if not module_allowed(a.name):
                self.flag(node, f"`import {a.name}` is not on the panel allowlist")
                continue
            if a.name.startswith(PACKAGE):
                continue
            if a.asname:
                self.modules[a.asname] = a.name
            else:
                top = a.name.split(".")[0]
                self.modules[top] = top
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        mod = node.module or ""
        if node.level > 0 or mod == PACKAGE or mod.startswith(PACKAGE + "."):
            for a in node.names:
                if a.name == "*":
                    self.flag(node, "star import")
            return
        if mod == "zouinify":
            for a in node.names:
                if a.name not in ZOUINIFY_NAMES:
                    self.flag(node, f"`from zouinify import {a.name}`: the panel may import only {sorted(ZOUINIFY_NAMES)}")
                else:
                    self.modules[a.asname or a.name] = f"zouinify.{a.name}"
            return
        if mod not in MODULES:
            self.flag(node, f"`from {mod} import ...` is not on the panel allowlist")
            return
        allowed = MODULES[mod]
        for a in node.names:
            if a.name == "*":
                self.flag(node, "star import")
                continue
            if allowed != ALL and a.name not in allowed:
                self.flag(node, f"`from {mod} import {a.name}`: not on {mod}'s allowlist")
                continue
            sub = f"{mod}.{a.name}"
            if sub in MODULES:
                self.modules[a.asname or a.name] = sub

    # -- attributes and names ------------------------------------------------------------------------------
    def visit_Attribute(self, node):
        attr = node.attr
        if attr.startswith("_") and attr not in ALLOWED_DUNDERS:
            self.flag(node, f"private attribute .{attr}")
        if attr == "write":
            if not (isinstance(node.value, ast.Attribute) and node.value.attr == "wfile"):
                self.flag(node, "attribute .write is allowed only on the response's wfile")
        elif attr in BANNED_ATTRS:
            self.flag(node, f"attribute .{attr} is banned in the panel")
        if attr == "open" and id(node) not in self.called_attr:
            self.flag(node, ".open used as a value (only a direct read-mode call is allowed)")
        chain, root = [], node
        while isinstance(root, ast.Attribute):
            chain.append(root.attr)
            root = root.value
        if isinstance(root, ast.Name) and root.id in self.modules:
            m = self.modules[root.id]
            for a in reversed(chain):
                allowed = MODULES.get(m)
                if allowed is None or allowed == ALL:
                    break
                if a not in allowed:
                    self.flag(node, f"{m}.{a} is not on the panel allowlist")
                    break
                nxt = f"{m}.{a}"
                if nxt in MODULES:
                    m = nxt
                else:
                    break
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            if node.id in BANNED_NAMES:
                self.flag(node, f"reference to {node.id} is banned in the panel")
            if node.id in CALL_ONLY and id(node) not in self.called_name:
                self.flag(node, f"{node.id} used as a value (only a direct call is allowed)")
            if node.id in self.modules and not isinstance(getattr(node, "parent", None), ast.Attribute):
                self.flag(node, f"module {node.id} used as a value (no aliasing, no passing modules around)")
        if node.id.startswith("__") and node.id not in ALLOWED_DUNDERS:
            self.flag(node, f"dunder name {node.id}")
        self.generic_visit(node)

    # -- calls ----------------------------------------------------------------------------------------------
    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Name):
            self.called_name.add(id(f))
            if f.id == "open":
                self.check_open(node, method=False)
        elif isinstance(f, ast.Attribute):
            self.called_attr.add(id(f))
            if f.attr == "open":
                self.check_open(node, method=True)
        for a in node.args:
            if isinstance(a, ast.Starred):
                self.flag(node, "*args in a call")
        for kw in node.keywords:
            if kw.arg is None:
                self.flag(node, "**kwargs in a call")
        self.generic_visit(node)

    def check_open(self, node, method: bool) -> None:
        mode = None
        if not method and len(node.args) >= 2:
            mode = node.args[1]
        if method and len(node.args) >= 1:
            mode = node.args[0]
        for kw in node.keywords:
            if kw.arg == "mode":
                mode = kw.value
        if mode is None:
            return
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and mode.value in READ_MODES:
            return
        self.flag(node, "open() with a mode that is not a literal read mode")


def check_file(path: str) -> list:
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        return [f"{path}:{e.lineno}: syntax error: {e.msg}"]
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent
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
            print(f"panel ast ok: {files} file(s) under {d} use only allowlisted imports and attributes, no dynamic import, no alias, "
                  "no exec, no write, no private attribute")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
