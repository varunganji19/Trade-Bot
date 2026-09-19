#!/usr/bin/env python3
"""Print every CLI subcommand main.py registers, one per line.

WHY THIS EXISTS: CI's smoke job used to iterate a HARDCODED list of
subcommands. When `market` and `seed-demo` were removed the list kept naming
them, so the job failed on commands that no longer exist while testing
nothing about the ones that do — and `config`, newly added, was never smoked
at all. A list that has to be hand-maintained in a second place will drift;
this derives it from the parser itself.

Imports main.py rather than regexing it: if the module cannot even be
imported, that is a failure worth reporting here too.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as cli  # noqa: E402


def subcommands() -> list[str]:
    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    if parser is None:
        # main() builds its parser inline; rebuild by capturing add_subparsers
        raise SystemExit("main.py exposes no build_parser(); see scripts/list_subcommands.py")
    out: list[str] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            out.extend(sorted(action.choices))
    return out


if __name__ == "__main__":
    names = subcommands()
    if not names:
        raise SystemExit("no subcommands found — the parser's shape changed")
    print("\n".join(names))
