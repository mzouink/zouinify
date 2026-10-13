#!/usr/bin/env python3
"""AST allowlist for lib/zouinify/panel/*.py -- the merge gate of the panel tickets (desk, T-0160).

A regex denylist cannot stop string tricks (`sys.modules['o'+'s']`, `getattr(mod, 'sys'+'tem')`), aliasing
(`o = open`, `m = os`), walks through an allowed module into a dangerous one (`http.server.shutil`,
`re.functools.partial(open, mode="w")`) or relative imports (`from ..tickets import ticket_sh`), so the panel
package is checked structurally. Every Python file under it may only:
  - import the allowlisted modules; from `zouinify` only `readonly`, `panelstate` and `webauth`; from its own package only
    sibling modules and the names they define (relative imports are resolved to absolute names first; `import
    zouinify...` is refused: only `from zouinify.panel.<mod> import <name>`);
  - use, on an allowlisted module, only the attributes on that module's list (so `http.client`, `http.server.shutil`,
    `re.functools`, `panelstate.tempfile`, `os.getpid` are out; `os.path.*` is in); on a sibling module only the
    names it defines, and a module it imported continues the walk under that module's rules;
  - never use a module as a value (no aliasing, no passing modules around), and never assign to, delete or
    augment an attribute reached from a module (`webauth.host_allowed = len`, `del readonly.capture_pane`,
    `webauth.RATE_LIMIT += 1`): the allowlisted modules are read-only for the panel;
  - never reference getattr/setattr/delattr/__import__/exec/eval/compile/globals/locals/vars/...,
    nor `open` / `print` except as the function of a direct call; never `*args` / `**kwargs` in a call;
  - never open a file for writing (the builtin open() with a non-read or non-literal mode); `.open` on anything is banned;
  - import only at module level (an import inside a class or function would escape the attribute walk);
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
PACKAGE = "zouinify.panel"
READONLY_API = {
    "capture_pane", "list_panes", "has_session", "model_on_screen", "model_word", "alias_of", "trunk_sha", "proc_parent",
    "path_exists", "list_agents", "find_agent", "is_ticket_id", "ticket_files", "file_of", "front_matter", "freeze", "counts",
    "tickets_dir", "now_local", "creds", "registry_entries", "registry_entry", "read_ticket_sh",
    "read_human_json", "read_board_json", "fail", "version", "TERMINAL", "ROLE_SESSIONS", "READ_ONLY_TICKET_SH",
}
PANELSTATE_API = {"version", "pid", "now_iso", "url", "state_dir", "panel_json_path", "server_log_path", "same_root", "log",
                  "write_panel_json", "remove_panel_json", "hook_installed",
                  "token_matches", "token_id", "token_configured"}   # never set_token (rotation is the process side's)
                                                                    # nor read_panel_json (it returns the token hash)
WEBAUTH_API = {"LOOPBACK_PEERS", "TOKEN_RE", "PANEL_COOKIE", "RATE_LIMIT", "RATE_WINDOW_S", "NO_TOKEN_WEIGHT", "CSP",
               "unlock_csp", "host_allowed", "bearer_token", "presented_token", "RateLimiter", "authorize"}   # == webauth.__all__ (T-0174)
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
    "base64": {"b64encode"},
    "threading": {"Thread", "Lock", "RLock", "Event", "current_thread", "Timer"},
    "urllib": {"parse"},
    "urllib.parse": {"urlsplit", "urlparse", "unquote", "unquote_plus", "quote", "parse_qs", "parse_qsl", "urlencode", "urljoin"},
    "unicodedata": {"normalize"},          # T-0175: NFKC before the foreign-dialog allowlist
    "zouinify.readonly": READONLY_API,
    "zouinify.panelstate": PANELSTATE_API,
    "zouinify.webauth": WEBAUTH_API,
}
ZOUINIFY_NAMES = {"readonly", "panelstate", "webauth"}
BANNED_NAMES = {
    "getattr", "setattr", "delattr", "__import__", "exec", "eval", "compile", "globals", "locals", "vars", "breakpoint", "input",
    "exit", "quit", "help", "memoryview", "subprocess", "sys", "importlib", "socket", "signal", "ctypes", "runpy", "code",
    "webbrowser", "pty", "shlex", "tempfile", "glob", "builtins", "functools", "shutil", "io", "selectors", "asyncio",
    "multiprocessing", "os_", "posix", "zouinify",
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
    "settrace", "setprofile", "zouinify", "panelctl", "registry", "agents",   # (.tmux / .status / .config are plain data fields)
    "spawnlpe", "spawnvpe", "execlpe", "pwrite", "writev", "dup", "dup2", "fchmod", "fchown", "lchown", "mknod", "mkfifo", "utime",
    "ftruncate", "open",   # `.open` on anything: only the builtin open(...) in a literal read mode exists for the panel
}
ALLOWED_DUNDERS = {"__name__", "__file__", "__doc__", "__future__", "__init__", "__enter__", "__exit__", "__str__", "__repr__",
                   "__eq__", "__hash__", "__iter__", "__len__", "__contains__", "__getitem__", "__setitem__", "__call__"}
READ_MODES = {"r", "rb", "rt", "br", "tr"}

_SIBLING_CACHE: dict = {}


def module_allowed(name: str) -> bool:
    return name in MODULES


def sibling_path(pkg_dir: str, name: str) -> str | None:
    p = os.path.join(pkg_dir, name + ".py")
    return p if os.path.isfile(p) else None


def resolve(pkg_dir: str, module: str, level: int) -> str | None:
    """The absolute module name of an import seen from a file of the panel package, or None when it points
    nowhere the panel may look (more than two levels up)."""
    if level == 0:
        return module
    if level == 1:
        return f"{PACKAGE}.{module}" if module else PACKAGE
    if level == 2:
        return f"zouinify.{module}" if module else "zouinify"
    return None


def sibling_exports(pkg_dir: str, name: str) -> tuple:
    """(module bindings, defined names) of a sibling module, from its top-level statements (cached)."""
    path = sibling_path(pkg_dir, name)
    if path is None:
        return None, None
    if path in _SIBLING_CACHE:
        return _SIBLING_CACHE[path]
    _SIBLING_CACHE[path] = ({}, set())   # guards an import cycle
    bindings, defined = {}, set()
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
    except (OSError, SyntaxError):
        return bindings, defined
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                if module_allowed(a.name):
                    bindings[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            mod = resolve(pkg_dir, node.module or "", node.level)
            for a in node.names:
                local = a.asname or a.name
                if mod == "zouinify" and a.name in ZOUINIFY_NAMES:
                    bindings[local] = f"zouinify.{a.name}"
                elif mod == PACKAGE and sibling_path(pkg_dir, a.name):
                    bindings[local] = f"{PACKAGE}.{a.name}"
                elif mod and mod.startswith(PACKAGE + "."):
                    sb, sd = sibling_exports(pkg_dir, mod[len(PACKAGE) + 1:])
                    if sb is not None and a.name in sb:
                        bindings[local] = sb[a.name]
                    else:
                        defined.add(local)
                elif mod in MODULES and f"{mod}.{a.name}" in MODULES:
                    bindings[local] = f"{mod}.{a.name}"
                else:
                    defined.add(local)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defined.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
    _SIBLING_CACHE[path] = (bindings, defined)
    return bindings, defined


class Checker(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.pkg_dir = os.path.dirname(os.path.abspath(path))
        self.problems: list = []
        self.modules: dict = {}      # local name -> module path (allowlisted module or a panel sibling)
        self.called_attr: set = set()
        self.called_name: set = set()

    def flag(self, node, why: str) -> None:
        line = f"{self.path}:{getattr(node, 'lineno', 0)}: {why}"
        if line not in self.problems:
            self.problems.append(line)

    # -- imports ----------------------------------------------------------------------------------------
    def module_level(self, node) -> bool:
        if isinstance(getattr(node, "parent", None), ast.Module):
            return True
        self.flag(node, "an import inside a class, function or block: imports are allowed at module level only")
        return False

    def visit_Import(self, node):
        if not self.module_level(node):
            return
        for a in node.names:
            if a.name == "zouinify" or a.name.startswith("zouinify."):
                self.flag(node, f"`import {a.name}`: the panel imports zouinify only as `from zouinify import readonly|panelstate|webauth` "
                                "or `from zouinify.panel.<mod> import <name>`")
                continue
            if not module_allowed(a.name):
                self.flag(node, f"`import {a.name}` is not on the panel allowlist")
                continue
            if a.asname:
                self.modules[a.asname] = a.name
            else:
                top = a.name.split(".")[0]
                self.modules[top] = top
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if not self.module_level(node):
            return
        mod = resolve(self.pkg_dir, node.module or "", node.level)
        shown = f"{'.' * node.level}{node.module or ''}"
        if mod is None:
            self.flag(node, f"`from {shown} import ...`: relative import beyond the zouinify package")
            return
        for a in node.names:
            if a.name == "*":
                self.flag(node, "star import")
        if mod == "zouinify":
            for a in node.names:
                if a.name not in ZOUINIFY_NAMES:
                    self.flag(node, f"`from {shown} import {a.name}`: the panel may import only {sorted(ZOUINIFY_NAMES)} from zouinify")
                else:
                    self.modules[a.asname or a.name] = f"zouinify.{a.name}"
            return
        if mod == PACKAGE:
            for a in node.names:
                if a.name == "*":
                    continue
                if sibling_path(self.pkg_dir, a.name):
                    self.modules[a.asname or a.name] = f"{PACKAGE}.{a.name}"
                else:
                    self.flag(node, f"`from {shown} import {a.name}`: not a module of the panel package")
            return
        if mod.startswith(PACKAGE + "."):
            sib = mod[len(PACKAGE) + 1:]
            bindings, defined = sibling_exports(self.pkg_dir, sib)
            if bindings is None:
                self.flag(node, f"`from {shown} import ...`: {sib}.py is not a module of the panel package")
                return
            for a in node.names:
                if a.name == "*":
                    continue
                if a.name in bindings:
                    self.modules[a.asname or a.name] = bindings[a.name]
                elif a.name not in defined:
                    self.flag(node, f"`from {shown} import {a.name}`: {sib}.py defines no such name")
            return
        if mod.startswith("zouinify"):
            self.flag(node, f"`from {shown} import ...` is not on the panel allowlist (only readonly, panelstate, webauth and panel siblings)")
            return
        if mod not in MODULES:
            self.flag(node, f"`from {shown} import ...` is not on the panel allowlist")
            return
        allowed = MODULES[mod]
        for a in node.names:
            if a.name == "*":
                continue
            if allowed != ALL and a.name not in allowed:
                self.flag(node, f"`from {mod} import {a.name}`: not on {mod}'s allowlist")
                continue
            if f"{mod}.{a.name}" in MODULES:
                self.modules[a.asname or a.name] = f"{mod}.{a.name}"

    # -- attributes and names ------------------------------------------------------------------------------
    def walk_chain(self, node, root_name: str, chain: list) -> None:
        m = self.modules[root_name]
        for a in reversed(chain):
            if m.startswith(PACKAGE + "."):
                bindings, defined = sibling_exports(self.pkg_dir, m[len(PACKAGE) + 1:])
                if bindings is None:
                    break
                if a in bindings:
                    m = bindings[a]
                    continue
                if a not in defined:
                    self.flag(node, f"{m}.{a}: the sibling defines no such name")
                break
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

    def visit_Attribute(self, node):
        attr = node.attr
        if attr.startswith("_") and attr not in ALLOWED_DUNDERS:
            self.flag(node, f"private attribute .{attr}")
        if attr == "write":
            if not (isinstance(node.value, ast.Attribute) and node.value.attr == "wfile"):
                self.flag(node, "attribute .write is allowed only on the response's wfile")
        elif attr in BANNED_ATTRS:
            self.flag(node, f"attribute .{attr} is banned in the panel")
        chain, root = [], node
        while isinstance(root, ast.Attribute):
            chain.append(root.attr)
            root = root.value
        if isinstance(root, ast.Name) and root.id in self.modules:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                verb = "deletion of" if isinstance(node.ctx, ast.Del) else "assignment to"
                self.flag(node, f"{verb} {self.modules[root.id]}.{'.'.join(reversed(chain))}: modules are read-only for the panel "
                                "(no rebinding of a checked function, constant or class)")
            self.walk_chain(node, root.id, chain)
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
        _SIBLING_CACHE.clear()
        problems, files = check_dir(d)
        if problems:
            print(f"panel ast FAIL: {len(problems)} violation(s) in {d}:")
            for p in problems:
                print("  " + p)
            rc = 1
        else:
            print(f"panel ast ok: {files} file(s) under {d} use only allowlisted imports and attributes (relative imports resolved), "
                  "no dynamic import, no alias, no exec, no write, no private attribute")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
