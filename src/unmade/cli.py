"""Command line entry point.

Exit codes follow the rest of the org: 0 nothing to report, 1 findings,
2 unmade could not answer. The third one matters here more than usual,
because the way this tool fails is make refusing to read the makefile — and
make prints a partial database on its way out. Exiting 0 with no findings
there would read as "your makefile is clean".
"""

from __future__ import annotations

import argparse
import os
import sys

from . import analyse, database, lex, phony, report, verify


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unmade",
        description=(
            "Say which Makefile GNU make actually reads, and which of its "
            "assignments and recipes never take effect."
        ),
        epilog=(
            "unmade runs `make -qp` to read make's own database. No recipe "
            "runs, but make expands $(shell ...) while reading a makefile, so "
            "those do run. Do not point it at a makefile you would not run."
        ),
    )
    parser.add_argument(
        "makefile",
        nargs="?",
        help="the makefile to check. Defaults to whatever a bare `make` here would read.",
    )
    parser.add_argument(
        "-C",
        "--directory",
        default=None,
        help="run make from here, as `make -C` would. Defaults to the makefile's directory.",
    )
    parser.add_argument(
        "-D",
        "--define",
        action="append",
        default=[],
        metavar="VAR=VALUE",
        help=(
            "pass VAR=VALUE to make as a command-line variable. Repeatable. "
            "Use it to ask the question that matters: which of my lines lose "
            "when CI invokes make like this."
        ),
    )
    parser.add_argument("--make", default="make", help="the make binary to ask (default: make)")
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help=(
            "skip the deletion check. Faster, and the findings become what a "
            "precedence model thinks rather than what make confirmed."
        ),
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="one line per finding, nothing else"
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def _default_makefile(directory: str) -> str | None:
    for name in analyse.DEFAULT_NAMES:
        if os.path.isfile(os.path.join(directory, name)):
            return name
    return None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.makefile:
        path = args.makefile
        directory = args.directory or (os.path.dirname(path) or ".")
        name = os.path.basename(path)
        if not os.path.isfile(os.path.join(directory, name)):
            print(f"unmade: {path}: no such file", file=sys.stderr)
            return 2
    else:
        directory = args.directory or "."
        found = _default_makefile(directory)
        if found is None:
            tried = ", ".join(analyse.DEFAULT_NAMES)
            print(
                f"unmade: no makefile in {directory}: make would look for {tried}",
                file=sys.stderr,
            )
            return 2
        name = found

    for define in args.define:
        if "=" not in define:
            print(
                f"unmade: --define {define}: expected VAR=VALUE",
                file=sys.stderr,
            )
            return 2

    try:
        version = database.version(args.make)
        db = database.read(name, directory, args.make, tuple(args.define))
    except database.MakeUnavailable as exc:
        print(f"unmade: {exc}", file=sys.stderr)
        return 2
    except database.MakeRefused as exc:
        # make would not read the file, so there is no database and no
        # honest report. Say which file and what make said.
        print(f"unmade: {name}: make could not read it: {exc}", file=sys.stderr)
        return 2

    lexed: dict[str, lex.Lexed] = {}
    unreadable: list[str] = []
    for file in db.makefile_list:
        full = os.path.join(directory, file)
        try:
            lexed[file] = lex.lex(open(full, encoding="utf-8", errors="replace").read(), file)
        except OSError:
            unreadable.append(file)

    # Makefiles make did *not* read, so their assignments can be reported as
    # dead rather than silently ignored.
    #
    # Only when the file under analysis is the one a bare `make` would pick.
    # If the caller said `-f Makefile` while a GNUmakefile sits next to it,
    # then Makefile's lines are the live ones *for that invocation*, and
    # calling GNUmakefile's lines dead would have it exactly backwards. The
    # not-the-default-makefile finding is what that situation deserves.
    if name == _default_makefile(directory):
        for candidate in analyse.DEFAULT_NAMES:
            if candidate in lexed or candidate == name:
                continue
            full = os.path.join(directory, candidate)
            if os.path.isfile(full):
                lexed[candidate] = lex.lex(
                    open(full, encoding="utf-8", errors="replace").read(), candidate
                )

    result = analyse.analyse(lexed, db, name, directory)
    result.make_version = version
    # Before the deletion check, and not behind --no-verify: this one decides
    # whether a `.PHONY:` finding is true at all, rather than confirming a
    # true one. See phony.py's docstring for why that is a different call.
    result = phony.check(result, directory, args.make, tuple(args.define))
    if not args.no_verify:
        result = verify.verify(result, directory, args.make, tuple(args.define))
    for file in unreadable:
        result.errors.append(f"make read {file} but unmade could not open it")

    if args.json:
        print(report.as_json(result))
    else:
        body = report.text(result, quiet=args.quiet)
        if body:
            print(body)

    for line in report.error_lines(result):
        print(line, file=sys.stderr)

    return 1 if result.findings else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
