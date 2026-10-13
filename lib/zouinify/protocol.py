"""The ticket protocol files rendered from templates (T-0188, for T-0185's `init --protocol`): `tickets/README.md`,
`tickets/_TEMPLATE.md` and `tickets/WORKER_BRIEF.md` from `templates/tickets/*.md.tmpl`, with three kinds of input:

- the project's config (names, repos, trunk, prod, verbs, types, deploy kinds, the desk and debug sessions);
- the deploy plugin's doc fragments, `tickets/plugins/<name>/docs/*.md` in the vendored copy under `templates/tickets/plugins/`
  (the `none` plugin ships generic ones): the deploy-specific prose of README rules 2, 3, 4 and 8, the evidence rows of
  section 5, the brief's points 5 and 6 and the `## Deploy handoff` block of the ticket template;
- the project-owned sections `## 0. Environments` and `## 7. Deploying` from `<root>/.ops/README.project.md`, spliced in
  verbatim (a project without that file gets a stub telling it where to write them).

`zouinify protocol render --out DIR` writes the three files under DIR and nowhere else; `init --protocol` (T-0185) is what
puts them into a repository, behind a printed plan and the human's own yes.
"""
from __future__ import annotations

import os
import re
import sys

from zouinify import ZouinifyError
from zouinify import briefs as briefsmod
from zouinify import ticketenv
from zouinify.config import ROLE_SESSIONS

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATES = os.path.join(REPO, "templates", "tickets")
PLUGINS = os.path.join(TEMPLATES, "plugins")
SUFFIX = ".tmpl"                                   # README.md.tmpl, _TEMPLATE.md.tmpl, WORKER_BRIEF.md.tmpl
FILES = ("README.md", "_TEMPLATE.md", "WORKER_BRIEF.md")
FRAGMENTS = ("rule-no-live-mutations", "rule-protect-artifacts", "rule-deploy-evidence", "rule-after-deploy",
             "evidence-deployed", "evidence-done", "brief-live-mutations", "brief-paid-runs", "handoff")
OPTIONAL_FRAGMENTS = ("verbs",)
STATES = ("backlog", "ready", "in-dev", "in-review", "ready-to-merge", "merged", "ready-to-deploy", "deployed", "done", "blocked",
          "dropped", "skipped")
VERB_HINTS = {"APPLY": "a saved plan", "DEPLOY": "a deploy", "PUSH": "an image", "RUN": "any other command", "DECIDE": "",
              "CHECK": "browser or email", "APPROVE": "a paid run, with the dollar amount", "SSO": "refresh credentials"}
HEADING_RE = re.compile(r"^## (\d+)\. ")
FENCE_RE = re.compile(r"^(```|~~~)")
RULE_RE = re.compile(r"^-{3,}\s*$")
PROJECT_README_REL = os.path.join(".ops", "README.project.md")


def plugin_docs_dir(cfg, given: str | None, out=None) -> str:
    if given:
        d = os.path.realpath(os.path.expanduser(given))
        if not os.path.isdir(d):
            raise ZouinifyError(f"--plugin-docs {given}: not a directory")
        return d
    d = os.path.join(PLUGINS, cfg.deploy_plugin, "docs")
    if os.path.isdir(d):
        return d
    if cfg.deploy_plugin != "none" and out is not None:
        print(f"warning: no docs shipped for deploy plugin {cfg.deploy_plugin} ({d}); rendering with the generic `none` prose -- pass "
              f"--plugin-docs DIR for the plugin's own", file=out)
    return os.path.join(PLUGINS, "none", "docs")


def load_fragments(directory: str) -> dict:
    out = {}
    for name in FRAGMENTS + OPTIONAL_FRAGMENTS:
        path = os.path.join(directory, name + ".md")
        if not os.path.isfile(path):
            if name in OPTIONAL_FRAGMENTS:
                continue
            raise ZouinifyError(f"{directory}: the plugin docs lack {name}.md (required: {', '.join(FRAGMENTS)})")
        with open(path, encoding="utf-8") as fh:
            out[name] = fh.read().rstrip("\n")
    return out


def project_sections(text: str) -> dict:
    """{number: block} for every `## N. <title>` section of .ops/README.project.md (that exact heading form, at the margin,
    outside fenced code), its preamble ignored, CRLF normalized, trailing blank and `---` rule lines dropped; the same
    number twice is an error."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    sections: dict = {}
    current = None
    fenced = None
    for line in text.split("\n"):
        f = FENCE_RE.match(line)
        if f:
            fenced = None if fenced == f.group(1) else (fenced or f.group(1))
        m = HEADING_RE.match(line) if fenced is None else None
        if m:
            n = int(m.group(1))
            if n in sections:
                raise ZouinifyError(f".ops/README.project.md: section `## {n}.` appears twice")
            current = n
            sections[n] = [line]
            continue
        if current is not None:
            sections[current].append(line)
    out = {}
    for n, lines in sections.items():
        while lines and (not lines[-1].strip() or RULE_RE.match(lines[-1])):
            lines.pop()
        out[n] = "\n".join(lines)
    return out


def read_project_readme(cfg, given: str | None) -> dict:
    path = os.path.realpath(os.path.expanduser(given)) if given else cfg.path(PROJECT_README_REL)
    if not os.path.isfile(path):
        if given:
            raise ZouinifyError(f"--project-readme {given}: not a file")
        return {}
    with open(path, encoding="utf-8") as fh:
        return project_sections(fh.read())


def _join_words(items: list) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def repo_dirs(cfg) -> list:
    return [os.path.basename(os.path.realpath(cfg.path(r["path"]))) or r["name"] for r in cfg.repos]


def verbs_line(cfg, fragments: dict) -> str:
    if "verbs" in fragments:
        return fragments["verbs"]
    parts = []
    for v in cfg.ask_verbs:
        hint = VERB_HINTS.get(v, "")
        parts.append(f"`{v}` ({hint})" if hint else f"`{v}`")
    return "Verbs: " + ", ".join(parts) + "."


def context(cfg, fragments: dict, sections: dict) -> dict:
    ctx = briefsmod.context(cfg)
    names = [str(r["name"]) for r in cfg.repos]
    dirs = repo_dirs(cfg)
    default = next((r for r in cfg.repos if r.get("default")), cfg.repos[0])
    n = len(cfg.repos)
    trunks = {str(r["trunk"]) for r in cfg.repos}
    prod = cfg.prod
    ctx.update({
        "repos_title": " + ".join(dirs),
        "repos_code": _join_words([f"`{d}`" for d in dirs]),
        "repos_phrase": "this repo" if n == 1 else ("both repos" if n == 2 else f"all {n} repos"),
        "trunk_phrase": (f"`{cfg.trunk}`" if n == 1 else (f"`{cfg.trunk}` in {'both' if n == 2 else 'every'} repo{'s' if n == 2 else ''}"
                                                           if len(trunks) == 1 else
                                                           ", ".join(f"`{r['trunk']}` in {d}" for r, d in zip(cfg.repos, dirs)))),
        "prod_intro": (f"**`{prod}`'s HEAD is always a release** (each one described in `{cfg.tickets_dir}/releases/`) and it moves only at\n"
                       f"a release -- never push to `{prod}` by hand, never merge a ticket branch into it." if prod else
                       "There is no separate production branch: trunk is what runs, so every merge is a release."),
        "prod_clause_brief": f"`{prod}` is production and nobody commits to it" if prod else "there is no separate production branch",
        "main_paths": _join_words([f"`{os.path.realpath(cfg.path(r['path']))}`" for r in cfg.repos]),
        "verbs_line": verbs_line(cfg, fragments),
        "types_bar": " | ".join(cfg.ticket_types),
        "default_type": cfg.ticket_types[0] if cfg.ticket_types else "bug",
        "types_angle": "|".join(cfg.ticket_types),
        "deploy_kinds_bar": " | ".join(cfg.deploy_kinds),
        "repos_bar": " | ".join(names),
        "repos_many": "" if n == 1 else " (one ticket may list several)",
        "default_repo": str(default["name"]),
        "states_line": " ".join(STATES),
        "releases_item": (f"- `releases/R-YYYY-MM-DD-N.md` -- one per production release: what `{prod}` was moved to, and why." if prod else ""),
        "deploy_plugin": cfg.deploy_plugin,
        "debug_phrase": (f"the `{cfg.sessions['debug'].name}` session does this" if "debug" in cfg.sessions and cfg.sessions["debug"].name
                         else "a session that did not write the change does this"),
    })
    for role in ROLE_SESSIONS:
        ctx.setdefault(f"{role}.name", f"({role}: not configured)")
    for name in FRAGMENTS + OPTIONAL_FRAGMENTS:
        if name in fragments:
            ctx["plugin." + name.replace("-", "_")] = fragments[name]
    ctx["project.section_0"] = sections.get(0) or (
        "## 0. Environments\n\n_Not written yet._ Add a section `## 0. Environments` to `.ops/README.project.md` (a table: name, where it\n"
        "runs, who deploys it, what it is for) and run `zouinify protocol render` again; it is spliced in here verbatim.")
    ctx["project.section_7"] = sections.get(7) or (
        "## 7. Deploying (all human-run; sessions prepare the handoff)\n\n_Not written yet._ Add a section `## 7. Deploying` to\n"
        "`.ops/README.project.md` (per deploy kind: the exact commands, from where, what the handoff must contain) and run\n"
        "`zouinify protocol render` again; it is spliced in here verbatim.")
    return ctx


def render_all(cfg, plugin_docs: str | None = None, project_readme: str | None = None, out=None) -> dict:
    if not os.path.isdir(TEMPLATES):
        raise ZouinifyError(f"no templates under {TEMPLATES}")
    fragments = load_fragments(plugin_docs_dir(cfg, plugin_docs, out))
    ctx = context(cfg, fragments, read_project_readme(cfg, project_readme))
    out = {}
    for name in FILES:
        with open(os.path.join(TEMPLATES, name + SUFFIX), encoding="utf-8") as fh:
            text, missing = briefsmod.render(fh.read(), ctx)
        if missing:
            raise ZouinifyError(f"{name}{SUFFIX}: unknown placeholder(s) {', '.join('{{' + m + '}}' for m in missing)}")
        out[name] = text.rstrip("\n") + "\n"
    return out


def cmd_list(cfg, out=None) -> int:
    out = out or sys.stdout
    for name in FILES:
        print(f"{name:<16} {os.path.join(TEMPLATES, name + SUFFIX)}", file=out)
    print(f"plugin docs      {plugin_docs_dir(cfg, None)}  (tickets.deploy_plugin: {cfg.deploy_plugin})", file=out)
    pr = cfg.path(PROJECT_README_REL)
    print(f"project sections {pr}  ({'present' if os.path.isfile(pr) else 'absent: sections 0 and 7 get a stub'})", file=out)
    return 0


def cmd_render(cfg, out_dir: str, plugin_docs: str | None = None, project_readme: str | None = None, force: bool = False,
               out=None) -> int:
    out = out or sys.stdout
    rendered = render_all(cfg, plugin_docs, project_readme, out)
    out_dir = os.path.realpath(os.path.expanduser(out_dir))
    conflicts = []
    for name, text in rendered.items():
        target = os.path.join(out_dir, name)
        if os.path.lexists(target) and (os.path.islink(target) or not os.path.isfile(target)):
            raise ZouinifyError(f"{target} is a symlink or not a regular file; refusing to write")
        if os.path.exists(target) and not force:
            with open(target, encoding="utf-8") as fh:
                if fh.read() != text:
                    conflicts.append(target)
    if conflicts:
        raise ZouinifyError("refusing to overwrite files that differ (use --force):\n  " + "\n  ".join(conflicts))
    os.makedirs(out_dir, exist_ok=True)
    for name, text in rendered.items():
        target = os.path.join(out_dir, name)
        verb = "wrote"
        if os.path.exists(target):
            with open(target, encoding="utf-8") as fh:
                verb = "unchanged" if fh.read() == text else "overwrote"
        if verb != "unchanged":
            ticketenv.write_atomic(target, text)
        print(f"{verb:<10} {target}", file=out)
    return 0
