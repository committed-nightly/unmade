"""Diff what the makefile says against what make did.

The rule this module is built on: unmade never decides which line won. It
works out which line it *expects* make to have used, asks make, and only
reports when the two agree. Where they disagree it says so and reports
nothing for that variable — see `Result.disagreements`.

That sounds like a weaker tool and it is the opposite. The expensive mistake
here is calling a live line dead: someone deletes it, the build changes, and
the tool that told them to is the reason. `CFLAGS += -O2` is the shape that
causes it, because make attributes the variable to the *last* appending line,
so a naive diff reports the original `=` as superseded. Nothing in this file
is allowed to get that wrong quietly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import database, lex

#: Names GNU make looks for, in the order it tries them. The first that
#: exists is the only one it reads.
DEFAULT_NAMES = ("GNUmakefile", "makefile", "Makefile")


@dataclass(frozen=True)
class Finding:
    kind: str
    file: str
    line: int
    detail: str
    #: What make used instead, when that is a place in a file.
    instead: str | None = None


@dataclass(frozen=True)
class Disagreement:
    """unmade's model of a variable did not match make's database.

    Printed, not swallowed. Each one is a variable unmade has deliberately
    said nothing about, and a construct worth a look.
    """

    name: str
    expected: str
    actual: str


@dataclass
class Result:
    makefile: str
    makefile_list: tuple[str, ...]
    findings: list[Finding] = field(default_factory=list)
    disagreements: list[Disagreement] = field(default_factory=list)
    #: Non-failures worth printing: things that are true but not wrong.
    notes: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    make_version: str = ""
    #: "checked", "skipped" or "off" -- whether each finding below was tested
    #: by deleting its line and running make again. See `verify.py`.
    verification: str = "off"
    verified: int = 0
    #: Candidates the verifier threw out, as (kind, line, detail). These are
    #: lines the model called dead and make proved were not.
    rejected: list[tuple[str, int, str]] = field(default_factory=list)
    make_runs: int = 0

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (f.file != self.makefile, f.file, f.line))


def _live_and_dead(
    assignments: list[lex.Assignment],
) -> tuple[list[lex.Assignment], lex.Assignment | None]:
    """Model make's variable precedence within the makefiles it read.

    Returns `(dead, winner)`: the assignments that can have no effect on the
    final value, and the one make should attribute the variable to.

    The rules, which are make's and not ours:

    * `=`, `:=`, `::=`, `!=` replace the value. Everything before is gone.
    * `+=` appends, so it does *not* kill what came before — but make
      attributes the variable to the last line that appended.
    * `?=` assigns only when the variable is not already set, so a `?=`
      after anything that set the variable is the line that does nothing.
    """
    dead: list[lex.Assignment] = []
    #: Assignments that still contribute to the value make ends up with. A
    #: reset kills all of them at once, which is the part that is easy to get
    #: wrong: in `FOO = a` / `FOO += b` / `FOO = c`, *both* of the first two
    #: lines are dead, not just the one make names.
    contributors: list[lex.Assignment] = []

    for assignment in assignments:
        if assignment.operator in lex.RESETTING:
            dead.extend(contributors)
            contributors = [assignment]
        elif assignment.operator == "+=":
            # Appends to whatever is there, so nothing before it dies. An
            # append to an unset variable just sets it.
            contributors.append(assignment)
        elif assignment.operator == "?=":
            if contributors:
                dead.append(assignment)
            else:
                contributors = [assignment]

    dead.sort(key=lambda a: a.line)
    return dead, (contributors[-1] if contributors else None)


def _is_later(
    assignment: lex.Assignment, place: tuple[str | None, int | None], order: dict[str, int]
) -> bool:
    """True when `assignment` sits after `place` in make's reading order."""
    file, line = place
    if file is None or line is None:
        return False
    mine = (order.get(assignment.file, len(order)), assignment.line)
    theirs = (order.get(file, len(order)), line)
    return mine > theirs


def _append_chain(assignments: list[lex.Assignment]) -> bool:
    """True when `+=` is involved, so a dead-set claim needs the strict check."""
    return any(a.operator == "+=" for a in assignments)


def analyse(
    lexed: dict[str, lex.Lexed],
    db: database.Database,
    makefile: str,
    directory: str = ".",
    check_default_name: bool = True,
) -> Result:
    """Compare every lexed file against the database make printed."""
    result = Result(
        makefile=makefile,
        makefile_list=db.makefile_list,
        make_version="",
    )
    read_files = set(db.makefile_list)

    _check_variables(lexed, db, read_files, result)
    _check_recipes(lexed, db, read_files, result)
    _check_phony(lexed, db, result)
    _check_includes(lexed, db, read_files, result)
    if check_default_name:
        _check_default_name(makefile, directory, result)
    return result


def _check_variables(
    lexed: dict[str, lex.Lexed],
    db: database.Database,
    read_files: set[str],
    result: Result,
) -> None:
    by_name: dict[str, list[lex.Assignment]] = {}
    unread: dict[str, list[lex.Assignment]] = {}

    for file, parsed in lexed.items():
        for assignment in parsed.assignments:
            if file in read_files:
                by_name.setdefault(assignment.name, []).append(assignment)
            else:
                unread.setdefault(assignment.name, []).append(assignment)

    # Order matters: make reads the files in MAKEFILE_LIST order, and within
    # a file, top to bottom.
    order = {name: index for index, name in enumerate(db.makefile_list)}
    for assignments in by_name.values():
        assignments.sort(key=lambda a: (order.get(a.file, len(order)), a.line))

    for name, assignments in sorted(by_name.items()):
        variable = db.variable(name)

        if variable is None:
            # make has no such variable. Only a conditional branch it never
            # entered explains that safely; `undefine` and `$(eval)` can
            # produce it too, so anything else stays quiet.
            for assignment in assignments:
                if assignment.conditional:
                    result.findings.append(
                        Finding(
                            "never-read",
                            assignment.file,
                            assignment.line,
                            f"{name} is assigned inside a conditional make did not "
                            f"enter, and is not defined anywhere in the database",
                        )
                    )
            continue

        if variable.origin == "command line":
            for assignment in assignments:
                if assignment.overrides:
                    continue
                result.findings.append(
                    Finding(
                        "beaten-by-command-line",
                        assignment.file,
                        assignment.line,
                        f"{name} is set on make's command line, which beats any "
                        f"assignment in a makefile unless it says `override`",
                        instead=f"command line ({name} = {variable.value})",
                    )
                )
            continue

        if variable.origin in ("environment", "environment under -e"):
            for assignment in assignments:
                how = (
                    "`?=` assigns only when the variable is unset, and it was "
                    "set in the environment"
                    if assignment.operator == "?="
                    else "make was run with -e, so the environment beats the makefile"
                )
                result.findings.append(
                    Finding(
                        "beaten-by-environment",
                        assignment.file,
                        assignment.line,
                        f"{name} comes from the environment: {how}",
                        instead=f"environment ({name} = {variable.value})",
                    )
                )
            continue

        if not variable.located:
            # No location to compare against, so no claim can be checked.
            if len(assignments) > 1:
                result.disagreements.append(
                    Disagreement(
                        name,
                        f"a location in {assignments[-1].file}",
                        f"origin {variable.origin!r} with no location",
                    )
                )
            continue

        candidates = list(assignments)
        never_read: list[lex.Assignment] = []
        actual = (variable.file, variable.line)

        # A `CC = clang` inside `ifeq ($(TOOLCHAIN),clang)` is the common
        # shape in any real makefile, and the model above cannot see that the
        # branch was skipped -- it reads the file, not the run. make can:
        # if it attributes the variable to an *earlier* line, then a later
        # resetting assignment never executed, so the conditional was false.
        #
        # Drop those one at a time, each justified by make's own answer, and
        # re-check. Anything that still disagrees is left alone.
        while True:
            dead, winner = _live_and_dead(candidates)
            if winner is None or (winner.file, winner.line) == actual:
                break
            later = _is_later(winner, actual, order)
            if winner.conditional and later:
                never_read.append(winner)
                candidates.remove(winner)
                continue
            break

        if winner is None:
            continue

        if (winner.file, winner.line) != actual:
            # The model and make still disagree about which line won. Report
            # nothing for this variable; the disagreement is the finding.
            result.disagreements.append(
                Disagreement(
                    name,
                    f"{winner.file}:{winner.line} ({winner.operator})",
                    f"{variable.file}:{variable.line}",
                )
            )
            continue

        for assignment in sorted(never_read, key=lambda a: a.line):
            result.findings.append(
                Finding(
                    "never-read",
                    assignment.file,
                    assignment.line,
                    f"{name} is assigned inside a conditional make did not enter: "
                    f"make attributes {name} to an earlier line, so this one never ran",
                    instead=f"{variable.file}:{variable.line}",
                )
            )

        appends = _append_chain(assignments)
        for assignment in dead:
            reason = (
                f"{name} is assigned again"
                if assignment.operator != "?="
                else f"{name} is already set by then, and `?=` only assigns when it is not"
            )
            detail = f"{reason}; this line has no effect on the value make uses"
            if appends:
                detail += " (the `+=` lines after it do still contribute)"
            result.findings.append(
                Finding(
                    "superseded",
                    assignment.file,
                    assignment.line,
                    detail,
                    instead=f"{variable.file}:{variable.line}",
                )
            )

    for name, assignments in sorted(unread.items()):
        for assignment in assignments:
            result.findings.append(
                Finding(
                    "unread-file",
                    assignment.file,
                    assignment.line,
                    f"{name} is assigned in {assignment.file}, which make never read",
                )
            )


def _check_recipes(
    lexed: dict[str, lex.Lexed],
    db: database.Database,
    read_files: set[str],
    result: Result,
) -> None:
    for file, parsed in lexed.items():
        if file not in read_files:
            continue
        for rule in parsed.rules:
            if not rule.has_recipe or rule.recipe_line is None:
                continue
            for target in rule.targets:
                if "$" in target or "%" in target or target.startswith("."):
                    # Expansions and pattern rules are not something unmade
                    # can match against the database by name.
                    continue
                entries = db.targets_named(target)
                if not entries:
                    continue
                recipes = [
                    (entry.recipe_file, entry.recipe_line)
                    for entry in entries
                    if entry.recipe_line is not None
                ]
                if not recipes:
                    continue
                if (file, rule.recipe_line) in recipes:
                    continue
                winner = recipes[-1]
                result.findings.append(
                    Finding(
                        "overridden-recipe",
                        file,
                        rule.recipe_line,
                        f"a later rule for `{target}` replaces this recipe; make runs "
                        f"only one recipe for a single-colon target",
                        instead=f"{winner[0]}:{winner[1]}",
                    )
                )


def _check_phony(
    lexed: dict[str, lex.Lexed], db: database.Database, result: Result
) -> None:
    declared: dict[str, int] = {}
    declared_in: dict[str, str] = {}
    for file, parsed in lexed.items():
        for name, line in parsed.phony:
            if "$" in name:
                continue
            declared.setdefault(name, line)
            declared_in.setdefault(name, file)

    for target in db.targets:
        if not target.phony or not target.does_nothing:
            continue
        if target.name not in declared:
            continue
        result.findings.append(
            Finding(
                "phony-without-rule",
                declared_in[target.name],
                declared[target.name],
                f"`.PHONY: {target.name}` names a target with no rule anywhere, so "
                f"`make {target.name}` runs nothing and exits 0",
            )
        )


def _check_includes(
    lexed: dict[str, lex.Lexed],
    db: database.Database,
    read_files: set[str],
    result: Result,
) -> None:
    for file, parsed in lexed.items():
        if file not in read_files:
            continue
        for include in parsed.includes:
            if include.kind == "include":
                # A hard `include` of a missing file is fatal, so make would
                # never have produced a database. Nothing to say.
                continue
            if "$" in include.operand:
                continue
            for word in include.operand.split():
                if any(char in word for char in "*?["):
                    continue
                if os.path.normpath(word) in {os.path.normpath(f) for f in read_files}:
                    continue
                result.findings.append(
                    Finding(
                        "unread-include",
                        file,
                        include.line,
                        f"`{include.kind} {word}` read nothing: the file is not in "
                        f"make's list of files read, and {include.kind} does not "
                        f"complain when it is missing",
                    )
                )


def _check_default_name(makefile: str, directory: str, result: Result) -> None:
    """Which file a bare `make` in this directory would read.

    GNU make tries GNUmakefile, then makefile, then Makefile, and stops at
    the first that exists. A `Makefile` next to a `makefile` is never read,
    which is the sort of thing that survives in a repository for years
    because both names look right.
    """
    present = [name for name in DEFAULT_NAMES if os.path.isfile(os.path.join(directory, name))]
    if not present:
        return
    chosen = present[0]
    base = os.path.basename(makefile)

    if base in DEFAULT_NAMES and base != chosen:
        result.findings.append(
            Finding(
                "not-the-default-makefile",
                base,
                1,
                f"a bare `make` in this directory reads {chosen}, not {base}: make "
                f"tries {', '.join(DEFAULT_NAMES)} and stops at the first that exists",
                instead=chosen,
            )
        )
        return

    for name in present[1:]:
        result.notes.append(
            Finding(
                "shadowed-makefile",
                name,
                1,
                f"{name} is never read while {chosen} exists",
                instead=chosen,
            )
        )
