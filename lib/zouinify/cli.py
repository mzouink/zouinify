#!/usr/bin/env python3
"""Entry point run by bin/zouinify (`python3 -I lib/zouinify/cli.py ...`): puts lib/ on sys.path and
dispatches to zouinify.commands.main."""
import os
import sys

_LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from zouinify.commands import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
