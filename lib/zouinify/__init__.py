"""zouinify -- a pluggable management pipeline for multi-session Claude Code work.

Everything project-specific comes from <project>/.ops/config.yaml; this package holds no project's
names, paths or ids. The lifecycle of tickets stays in the project's scripts/ticket.sh: this code never
edits a ticket file and never runs a mutating ticket.sh command (the only exception is `launch
--receipt` / `down --receipt`, which append one `ticket.sh log` line signed by the caller, the desk).
"""

__version__ = "0.1.0"


class ZouinifyError(Exception):
    """A user-facing error: printed as `zouinify: <message>` on stderr, exit status 1."""
