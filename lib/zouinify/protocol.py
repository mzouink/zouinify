"""The ticket protocol files rendered from templates (T-0188, for T-0185's `init --protocol`): `tickets/README.md`,
`tickets/_TEMPLATE.md` and `tickets/WORKER_BRIEF.md` from `templates/protocol/tickets/*.md.tmpl`, with three kinds of input:

- the project's config (names, repos, trunk, prod, verbs, types, deploy kinds, the desk and debug sessions);
- the deploy plugin's doc fragments, `tickets/plugins/<name>/docs/*.md`, vendored byte-identical from the upstream core under
  `templates/protocol/tickets/plugins/` (only `none` ships publicly; a project's own plugin lives in its repository): the
  deploy-specific prose of README rules 2, 3, 4 and 8, the two whole rows of section 5 for `deployed` and `done`, the brief's
  points 5 and 6 and the `## Deploy handoff` block of the ticket template;
- the project's own README.md text from `<root>/.ops/README.project.md`: any numbered section `## N. <title>` it carries
  replaces the template's section N verbatim (the template's own text is the default; sections 0 and 7 have no template
  text, so a project without them gets a stub saying where to write them), a `## N.M <title>` block appends its text to
  rule M of the template's section N as an indented continuation while the rest of the section keeps following the
  template (T-0224), and a `## Preamble` block replaces the intro paragraph under the H1 (T-0219). A replaced section
  stops receiving template updates; `protocol render` prints per section where it came from.

Optional plugin docs fragments (OPTIONAL_FRAGMENTS) carry the deploy wording that differs between plugins -- how the inbox
names what waits for the human, the human's deploy step in the life line and the board table, the in-review evidence row,
the ignored files prune keeps, the list of things that need the human -- with the template's generic words as the default
when the docs lack the file (the `none` docs are vendored byte-identical and ship none of them).

README.md and WORKER_BRIEF.md are generated artefacts and carry the header of zouinify.generated (T-0219, the briefs'
mechanism): an untouched render or `--force` is overwritten, a hand edit refused. _TEMPLATE.md carries none: `ticket.sh
new` copies it into every new ticket, so it keeps the plain whole-file comparison.

`zouinify protocol render --out DIR` writes the three files under DIR and nowhere else; `init --protocol` (T-0185) is what
puts them into a repository, behind a printed plan and the human's own yes.
"""
from __future__ import annotations

import os
import re
import sys

from zouinify import ZouinifyError, generated
from zouinify import briefs as briefsmod
from zouinify import ticketenv
from zouinify.config import ROLE_SESSIONS

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROTOCOL = os.path.join(REPO, "templates", "protocol")          # repo-relative layout: scripts/, tickets/ (T-0185)
TEMPLATES = os.path.join(PROTOCOL, "tickets")
PLUGINS = os.path.join(TEMPLATES, "plugins")
SUFFIX = ".tmpl"                                   # README.md.tmpl, _TEMPLATE.md.tmpl, WORKER_BRIEF.md.tmpl
FILES = ("README.md", "_TEMPLATE.md", "WORKER_BRIEF.md")
FRAGMENTS = ("rule-no-live-mutations", "rule-protect-artifacts", "rule-deploy-evidence", "rule-after-deploy",
             "evidence-deployed", "evidence-done", "brief-live-mutations", "brief-paid-runs", "handoff")
OPTIONAL_FRAGMENTS = ("verbs", "inbox-waiting", "inbox-kinds", "human-deploy-row", "life-deploy-step", "evidence-in-review",
                      "prune-ignored", "human-asks")
# the generic words the template uses when the plugin docs lack the optional fragment (T-0219); `verbs` has its own default,
# the sentence generated from the project's ask verbs (verbs_line)
OPTIONAL_DEFAULTS = {
    "inbox-waiting": "the deploys waiting (one item per handoff, the handoff's command, the expected last line; a plan that a newer "
                     "plan voided is listed under \"do NOT run\")",
    "inbox-kinds": "deploys, decisions, asks, checks",
    "human-deploy-row": "| `READY-TO-DEPLOY` | merged, handoff written; the ticket's `## Deploy handoff` has the exact commands | run them, "
                        "then say \"deployed\" |",
    "life-deploy-step": "--handoff--> ready-to-deploy --human deploys-->",
    "evidence-in-review": "| `in-review` | owner | suites covering `areas` run (command + summary line); RED-on-base / GREEN-on-branch test; "
                          "the linters of the touched languages; every `affects` box ticked or a `siblings:` ticket filed; worktree clean |",
    "prune-ignored": "secrets, keys, backups, and whatever the deploy plugin names",
    "human-asks": "a decision, a paid run, a deploy, a browser check, a command you were refused",
}
HEADERED = ("README.md", "WORKER_BRIEF.md")      # the generated artefacts that carry the header; _TEMPLATE.md does not
TOOL = "zouinify protocol render"
NOUN, THOSE = "file", "one of those"
STATES = ("backlog", "ready", "in-dev", "in-review", "ready-to-merge", "merged", "ready-to-deploy", "deployed", "done", "blocked",
          "dropped", "skipped")
VERB_HINTS = {"APPLY": "a saved plan", "DEPLOY": "a deploy", "PUSH": "an image", "RUN": "any other command", "DECIDE": "",
              "CHECK": "browser or email", "APPROVE": "a paid run, with the dollar amount", "SSO": "refresh credentials"}
HEADING_RE = re.compile(r"^## (\d+)\. ")
RULE_HEADING_RE = re.compile(r"^## (\d+)\.(\d+)(?:\s.*)?$")      # `## 4.5 <title>`: appends to rule 5 of section 4 (T-0224)
NEAR_HEADING_RE = re.compile(r"^## \d+\.")                         # looks like one of the two but is neither: refused, never dropped
ITEM_RE = re.compile(r"^(\d+)\. ")                                  # a numbered list item at the margin
PREAMBLE_RE = re.compile(r"^## Preamble\s*$")
FENCE_RE = re.compile(r"^(```|~~~)")
RULE_RE = re.compile(r"^-{3,}\s*$")
PROJECT_README_REL = os.path.join(".ops", "README.project.md")
WORKER_NOTES_ROLE = "worker"                        # .ops/briefs.project/worker.md: the project's own rules for its worker sessions (T-0354)


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
    outside fenced code), {(N, M): text} for every `## N.M <title>` block (its body, without the heading: it is appended to
    rule M of the template's section N; T-0224) and {"preamble": text} for a `## Preamble` block (its body; T-0219); the
    text before the first heading is ignored, CRLF normalized, trailing blank and `---` rule lines dropped; the same
    section or rule twice, a second preamble, or a section replaced whole and also given rules is an error."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    sections: dict = {}
    current = None
    fenced = None
    opened_at = 0
    for lineno, line in enumerate(text.split("\n"), 1):
        f = FENCE_RE.match(line)
        if f:
            fenced = None if fenced == f.group(1) else (fenced or f.group(1))
            if fenced is not None:
                opened_at = lineno
        m = HEADING_RE.match(line) if fenced is None else None
        if m:
            n = int(m.group(1))
            if n in sections:
                raise ZouinifyError(f".ops/README.project.md: section `## {n}.` appears twice")
            current = n
            sections[n] = [line]
            continue
        r = RULE_HEADING_RE.match(line) if fenced is None else None
        if r:
            key = (int(r.group(1)), int(r.group(2)))
            if key in sections:
                raise ZouinifyError(f".ops/README.project.md: `## {key[0]}.{key[1]}` appears twice")
            current = key
            sections[key] = []                           # the body only: the rule keeps the template's own text
            continue
        if fenced is None and NEAR_HEADING_RE.match(line):    # `## 4.5. title`, `## 4.5.1 x`, `## 4.Title`: a block nobody would get
            raise ZouinifyError(f".ops/README.project.md: `{line.strip()}` is neither a section heading (`## N. <title>`) nor a rule block "
                                f"(`## N.M <title>`); its text would be dropped, so it is refused")
        if fenced is None and PREAMBLE_RE.match(line):
            if "preamble" in sections:
                raise ZouinifyError(".ops/README.project.md: `## Preamble` appears twice")
            current = "preamble"
            sections[current] = []                       # the body only: the intro has no heading of its own
            continue
        if current is not None:
            sections[current].append(line)
    if fenced is not None:                              # an unclosed fence would swallow every later heading (review)
        raise ZouinifyError(f".ops/README.project.md: the code fence opened at line {opened_at} is never closed")
    out = {}
    for n, lines in sections.items():
        while lines and (not lines[-1].strip() or RULE_RE.match(lines[-1])):
            lines.pop()
        if n == "preamble" or isinstance(n, tuple):
            while lines and not lines[0].strip():
                lines.pop(0)
        if n == "preamble" and not lines:
            raise ZouinifyError(".ops/README.project.md: `## Preamble` is empty (write the intro under it, or drop the heading)")
        if isinstance(n, tuple) and not lines:
            raise ZouinifyError(f".ops/README.project.md: `## {n[0]}.{n[1]}` is empty (write the sentence to append under it, or drop the heading)")
        out[n] = "\n".join(lines)
    for key in out:
        if isinstance(key, tuple) and key[0] in out:
            raise ZouinifyError(f".ops/README.project.md: section `## {key[0]}.` is replaced whole and `## {key[0]}.{key[1]}` appends to one of "
                                f"its rules; a replaced section has no template rules to append to -- use one form or the other")
    return out


def apply_overrides(body: str, sections: dict) -> tuple:
    """The project's numbered sections replace the template's, verbatim; its `## N.M` blocks append to rule M of section N
    (the rule keeps the template's text, the project's lines follow it indented as a continuation; T-0224); its preamble
    replaces the intro under the H1. Returns (text, provenance): provenance maps "preamble" and every section number of
    the template to "template", "project", "stub" (0 and 7 without a project block) or "template + project rules 1, 5".
    A section the template does not have, or a rule number its section does not have, is an error."""
    heads = []                                          # (offset, number) of every real section heading: a `## N.` line inside
    pos, fenced = 0, None                               # fenced code is content (the project's sections may quote one)
    for line in body.split("\n"):
        f = FENCE_RE.match(line)
        if f:
            fenced = None if fenced == f.group(1) else (fenced or f.group(1))
        m = HEADING_RE.match(line) if fenced is None else None
        if m:
            heads.append((pos, int(m.group(1))))
        pos += len(line) + 1
    if fenced is not None:
        raise ZouinifyError("the rendered README has an unclosed code fence; nothing written")
    nums = [n for _, n in heads]
    for n in sections:
        sec = n[0] if isinstance(n, tuple) else n
        if n != "preamble" and sec not in nums:
            raise ZouinifyError(f".ops/README.project.md: the template has no section `## {sec}.` to {'append to' if isinstance(n, tuple) else 'replace'} "
                                f"(it has {', '.join(str(x) for x in nums)})")
    rules = {}                                          # section -> {rule: text}
    for key, text in sections.items():
        if isinstance(key, tuple):
            rules.setdefault(key[0], {})[key[1]] = text
    prov = {}
    for _, n in heads:
        if n in sections:
            prov[n] = "project"
        elif n in rules:
            prov[n] = "template + project rules " + ", ".join(str(m) for m in sorted(rules[n]))
        else:
            prov[n] = "stub" if n in (0, 7) else "template"
    for n in rules:
        if n in sections:
            raise ZouinifyError(f".ops/README.project.md: section `## {n}.` is replaced whole and also given rules; use one form or the other")
    for idx in range(len(heads) - 1, -1, -1):
        start, n = heads[idx]
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(body)
        if n in rules:
            body = body[:start] + append_to_rules(body[start:end], n, rules[n]) + body[end:]
            continue
        if n not in sections:
            continue
        old = body[start:end].rstrip("\n")
        # the template's `---` rule after a section (sections 0 and 1 are framed by two) stays where it was
        tail = "\n\n---\n\n" if RULE_RE.match(old.rsplit("\n", 1)[-1]) else "\n\n"
        block = sections[n].rstrip("\n") + (tail if idx + 1 < len(heads) else "\n")
        body = body[:start] + block + body[end:]
    pre = sections.get("preamble")
    prov["preamble"] = "project" if pre is not None else "template"
    if pre is not None:
        m = re.match(r"(# [^\n]*\n\n)(.*?)(\n(?:---\n|## ))", body, re.S)
        if not m:
            raise ZouinifyError("the README template has no intro paragraph under its title to replace")
        body = body[:m.start(2)] + pre.rstrip("\n") + "\n" + body[m.end(2):]
    return body, prov


def read_project_readme(cfg, given: str | None) -> dict:
    """The project's sections: from `<root>/.ops/README.project.md`, read like the brief notes (a symlink, a non-file, a
    path resolving outside the project or a non-UTF-8 file refused, never followed: its text becomes part of the protocol
    page every session reads; T-0219 review), or from an explicit --project-readme FILE, the caller's own choice."""
    what = "the project's README sections"
    if given:
        try:
            text = generated.read_project_text(cfg.root, PROJECT_README_REL, what, explicit=given)
        except ZouinifyError as e:
            raise ZouinifyError(f"--project-readme {e}") from None
        return project_sections(text or "")
    text = generated.read_project_text(cfg.root, PROJECT_README_REL, what)
    return project_sections(text) if text is not None else {}


def _join_words(items: list) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def repo_paths(cfg, main_root: str | None = None) -> list:
    """Every repo's checkout, realpath'd. A relative `path:` resolves against the main checkout the render speaks for:
    --main-root when given (a render from a ticket worktree names the main checkout, so the embedded paths and names are
    main's, not the worktree's), else the project root."""
    base = os.path.realpath(os.path.expanduser(main_root)) if main_root else cfg.root
    out = []
    for r in cfg.repos:
        rel = os.path.expanduser(str(r["path"]))
        out.append(os.path.realpath(rel if os.path.isabs(rel) else os.path.join(base, rel)))
    return out


def repo_dirs(cfg, main_root: str | None = None) -> list:
    return [os.path.basename(p) or r["name"] for p, r in zip(repo_paths(cfg, main_root), cfg.repos)]


def verbs_line(cfg, fragments: dict) -> str:
    if "verbs" in fragments:
        return fragments["verbs"]
    parts = []
    for v in cfg.ask_verbs:
        hint = VERB_HINTS.get(v, "")
        parts.append(f"`{v}` ({hint})" if hint else f"`{v}`")
    return "Verbs: " + ", ".join(parts) + "."


def worker_notes(cfg) -> str:
    """The project's own rules for its worker sessions, `.ops/briefs.project/worker.md`, as the text WORKER_BRIEF.md.tmpl splices
    after its last rule and before the desk's launch line: "" when the project has not written the file (the brief then reads
    as before), else the file's text with one blank line on each side. Read exactly like the role notes (briefs.notes_file +
    generated.read_project_text: a symlinked directory or file, a non-file, anything resolving outside the root, is refused).
    `briefs render` skips the worker on purpose (sessions.worker has no brief); this is where its notes land (T-0354)."""
    if briefsmod.notes_file(cfg.root, WORKER_NOTES_ROLE) is None:
        return ""
    text = generated.read_project_text(cfg.root, briefsmod.notes_rel(WORKER_NOTES_ROLE), "project worker notes")
    return "\n" + (text or "").strip("\n") + "\n" if (text or "").strip() else ""


def context(cfg, fragments: dict, sections: dict, main_root: str | None = None) -> dict:
    ctx = briefsmod.context(cfg)
    ctx["project.worker_notes"] = worker_notes(cfg)
    names = [str(r["name"]) for r in cfg.repos]
    dirs = repo_dirs(cfg, main_root)
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
        "main_paths": _join_words([f"`{p}`" for p in repo_paths(cfg, main_root)]),
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
        elif name in OPTIONAL_DEFAULTS:
            ctx["plugin." + name.replace("-", "_")] = OPTIONAL_DEFAULTS[name]
    ctx["project.section_0"] = sections.get(0) or (
        "## 0. Environments\n\n_Not written yet._ Add a section `## 0. Environments` to `.ops/README.project.md` (a table: name, where it\n"
        "runs, who deploys it, what it is for) and run `zouinify protocol render` again; it is spliced in here verbatim.")
    ctx["project.section_7"] = sections.get(7) or (
        "## 7. Deploying (all human-run; sessions prepare the handoff)\n\n_Not written yet._ Add a section `## 7. Deploying` to\n"
        "`.ops/README.project.md` (per deploy kind: the exact commands, from where, what the handoff must contain) and run\n"
        "`zouinify protocol render` again; it is spliced in here verbatim.")
    return ctx


def append_to_rules(block: str, n: int, rules: dict) -> str:
    """`block` is the rendered text of section N (heading included). For each rule M, the project's lines go after the
    last line of the numbered item `M. ...` (a markdown list item at the margin, outside fenced code; the item runs to the
    next item or the end of the section, trailing blank lines excluded), each indented by the item's own continuation
    indent (3 spaces for `1.`, 4 for `10.`), so they read as the rule's continuation and the template's text stays."""
    lines = block.split("\n")
    items = {}                                          # rule -> (first line index, continuation indent)
    margin = []                                         # every line at the margin outside fenced code: where an item ends
    fenced = None
    for i, line in enumerate(lines):
        f = FENCE_RE.match(line)
        if f:
            fenced = None if fenced == f.group(1) else (fenced or f.group(1))
            margin.append(i)
            continue
        if fenced is not None or not line.strip():
            continue
        if not line[0].isspace():
            margin.append(i)
        m = ITEM_RE.match(line)
        if m:
            items.setdefault(int(m.group(1)), (i, len(m.group(0))))
    for m in rules:
        if m not in items:
            have = ", ".join(str(k) for k in sorted(items)) or "none"
            raise ZouinifyError(f".ops/README.project.md: section `## {n}.` has no numbered rule {m} to append to (it has {have})")
    for m in sorted(rules, key=lambda r: items[r][0], reverse=True):     # from the last item up, so the earlier offsets hold
        first, indent = items[m]
        later = [i for i in margin if i > first]        # the item runs to the next margin-level line (an item, a paragraph, a
        end = later[0] if later else len(lines)         # rule, a fence), trailing blank lines excluded
        while end > first + 1 and not lines[end - 1].strip():
            end -= 1
        extra = [(" " * indent + l) if l.strip() else "" for l in rules[m].split("\n")]
        lines[end:end] = extra
    return "\n".join(lines)


def sources_of(cfg, name: str) -> str:
    """What the header of a generated protocol file names as its sources: the places to edit, never a value of the config
    (the plugin's name, say): the config is a source too, and changing it must render straight through (review)."""
    plugin = "the deploy plugin's docs"
    if name == "README.md":
        return f"`templates/protocol/tickets/README.md.tmpl`, {plugin} and `.ops/README.project.md`"
    if name == "WORKER_BRIEF.md":
        return f"`templates/protocol/tickets/WORKER_BRIEF.md.tmpl`, {plugin} and `{briefsmod.notes_rel(WORKER_NOTES_ROLE)}`"
    return f"`templates/protocol/tickets/{name}.tmpl` and {plugin}"


def render_detail(cfg, plugin_docs: str | None = None, project_readme: str | None = None, out=None,
                  main_root: str | None = None) -> tuple:
    """({file name: text as written, header included}, {template section: provenance} for README.md)."""
    if not os.path.isdir(TEMPLATES):
        raise ZouinifyError(f"no templates under {TEMPLATES}")
    fragments = load_fragments(plugin_docs_dir(cfg, plugin_docs, out))
    sections = read_project_readme(cfg, project_readme)
    ctx = context(cfg, fragments, sections, main_root)
    texts, prov = {}, {}
    for name in FILES:
        with open(os.path.join(TEMPLATES, name + SUFFIX), encoding="utf-8") as fh:
            text, missing = briefsmod.render(fh.read(), ctx)
        if missing:
            raise ZouinifyError(f"{name}{SUFFIX}: unknown placeholder(s) {', '.join('{{' + m + '}}' for m in missing)}")
        body = text.rstrip("\n") + "\n"
        if name == "README.md":
            body, prov = apply_overrides(body, sections)
        texts[name] = generated.header(TOOL, sources_of(cfg, name), NOUN, THOSE, body) + body if name in HEADERED else body
    return texts, prov


def render_all(cfg, plugin_docs: str | None = None, project_readme: str | None = None, out=None) -> dict:
    return render_detail(cfg, plugin_docs, project_readme, out)[0]


def untouched(cfg, name: str, text: str) -> bool:
    """A README.md or WORKER_BRIEF.md this renderer wrote for this project's sources and nobody edited."""
    return name in HEADERED and generated.untouched(text, TOOL, sources_of(cfg, name), NOUN, THOSE)


def cmd_list(cfg, out=None) -> int:
    out = out or sys.stdout
    for name in FILES:
        print(f"{name:<16} {os.path.join(TEMPLATES, name + SUFFIX)}", file=out)
    print(f"plugin docs      {plugin_docs_dir(cfg, None)}  (tickets.deploy_plugin: {cfg.deploy_plugin})", file=out)
    pr = cfg.path(PROJECT_README_REL)
    print(f"project sections {pr}  ({'present' if os.path.isfile(pr) else 'absent: sections 0 and 7 get a stub'}; any `## N.` section "
          f"there replaces the template's section N, `## Preamble` the intro)", file=out)
    wn = cfg.path(briefsmod.notes_rel(WORKER_NOTES_ROLE))
    print(f"worker notes     {wn}  ({'present; spliced into WORKER_BRIEF.md after rule 8, before the launch line' if os.path.isfile(wn) else 'absent: the brief ends with the launch line'})", file=out)
    return 0


def cmd_render(cfg, out_dir: str, plugin_docs: str | None = None, project_readme: str | None = None, force: bool = False,
               out=None, main_root: str | None = None) -> int:
    out = out or sys.stdout
    if main_root is not None:
        if not os.path.isdir(os.path.expanduser(main_root)):
            raise ZouinifyError(f"--main-root {main_root} is not a directory (the main checkout whose path and name the files embed)")
        print(f"main root  {os.path.realpath(os.path.expanduser(main_root))}", file=out)
    rendered, prov = render_detail(cfg, plugin_docs, project_readme, out, main_root=main_root)
    out_dir = os.path.realpath(os.path.expanduser(out_dir))
    conflicts = []
    for name, text in rendered.items():
        target = os.path.join(out_dir, name)
        if os.path.lexists(target) and (os.path.islink(target) or not os.path.isfile(target)):
            raise ZouinifyError(f"{target} is a symlink or not a regular file; refusing to write")
        if os.path.exists(target) and not force:
            with open(target, encoding="utf-8", errors="replace") as fh:
                current = fh.read()
            # README.md and WORKER_BRIEF.md carry the digest header: an untouched render is overwritten whatever changed in
            # the sources, a hand edit is refused; _TEMPLATE.md is compared whole (T-0219)
            if current != text and not untouched(cfg, name, current):
                conflicts.append(target)
    if conflicts:
        raise ZouinifyError("refusing to overwrite files that differ from the render and were not left as rendered (a hand edit: fold it "
                            "into the template, the plugin docs or .ops/README.project.md, then render; use --force to overwrite "
                            "them anyway):\n  " + "\n  ".join(conflicts))
    os.makedirs(out_dir, exist_ok=True)
    for name, text in rendered.items():
        target = os.path.join(out_dir, name)
        verb = "wrote"
        if os.path.exists(target):
            with open(target, encoding="utf-8", errors="replace") as fh:
                verb = "unchanged" if fh.read() == text else "overwrote"
        if verb != "unchanged":
            ticketenv.write_atomic(target, text)
        print(f"{verb:<10} {target}", file=out)
    for key in ["preamble"] + sorted(k for k in prov if k != "preamble"):
        label = "preamble" if key == "preamble" else f"section {key}"
        src = prov[key]
        if src.startswith("template + "):
            note = f"{src} ({PROJECT_README_REL} appends to those rules; the rest of the section follows the template)"
        else:
            note = {"project": f"project ({PROJECT_README_REL} overrides the template's text; it receives no template updates)",
                    "stub": f"stub (write `## {key}.` in {PROJECT_README_REL}; the template has no text of its own here)",
                    "template": "template"}[src]
        print(f"{label:<10} {note}", file=out)
    return 0
