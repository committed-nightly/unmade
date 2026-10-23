"""Ask make whether a `.PHONY:` name is holding a rule back.

`analyse` reports `phony-without-rule` when make's database has a phony
target with no recipe and no prerequisites. That reads like "a target you
forgot to write", and often it is. But the database is only a record of the
rules make *resolved*, and make resolves implicit and pattern rules for the
goals it was asked about -- so "no rule in the database" is not "no rule".

The reason it is not is the same fact that makes the declaration worth
writing: **make does not search implicit rules for a phony target.** So
`.PHONY: hello` next to a `hello.c` is the only reason `make hello` does not
compile a binary, and the declaration appears in the database as a target
with nothing attached to it -- indistinguishable, by shape alone, from a
genuinely dead one.

That shape is in the distribution. `/usr/share/doc/python3-launchpadlib/
docs/Makefile` is the stock Sphinx makefile, whose last two lines are a
catch-all `%: Makefile` and whose `.PHONY: help Makefile` is what stops make
rebuilding its own makefile through it. unmade called that line dead.

No model is going to settle this. Whether a pattern rule matches a name
depends on stems, on `.SUFFIXES`, on which prerequisites happen to exist on
this machine, and on a hundred built-in rules -- and the module next door
exists because modelling make is how you call a live line dead. So this one
asks: take the name off its `.PHONY:` line, and see whether make then has a
way to build it.

One make run per candidate, and candidates are rare -- a makefile with no
`.PHONY:` line costs nothing.
"""

from __future__ import annotations

from . import database
from .analyse import Finding, Result
from .verify import Unwritable, Verifier

KIND = "phony-without-rule"


def _without_name(lines: list[str], name: str) -> list[str] | None:
    """The same file with `name` gone from every `.PHONY:` prerequisite list.

    Every declaration has to go, not just the one the finding points at: the
    question is what make would do if this name were not phony at all, and a
    second `.PHONY:` line naming it would quietly answer a different one.

    Returns `None` if the name is not a plain word on any such line -- it
    came in through a variable, a continuation that does not split the way
    this expects, or something else not worth guessing at.
    """
    out = list(lines)
    found = False
    in_phony = False
    for index, line in enumerate(lines):
        if not in_phony and not line.lstrip().startswith(".PHONY:"):
            continue
        # Stay inside the record across `\` continuations, where the names
        # are on lines that do not start with `.PHONY:` themselves.
        in_phony = line.rstrip().endswith("\\")

        words = line.split()
        if name not in words:
            continue
        kept = [word for word in words if word != name]
        if kept == [".PHONY:"]:
            # Nothing left to declare. `.PHONY:` on its own is legal and
            # declares nothing, but an empty line is what a reader would
            # have written, and make treats the two the same.
            out[index] = ""
        else:
            out[index] = " ".join(kept)
        found = True

    return out if found else None


def _buildable(db: database.Database | None, name: str) -> bool:
    """Did make come back with a way to produce `name`?"""
    if db is None:
        return False
    return any(not target.does_nothing for target in db.targets_named(name))


def check(
    result: Result,
    directory: str,
    make: str = "make",
    variables: tuple[str, ...] = (),
) -> Result:
    """Drop every `phony-without-rule` finding whose name make can build.

    Runs whether or not `--no-verify` was passed, which is a different call
    from the one `verify` makes and deliberately so. `--no-verify` leaves you
    with what a precedence model thinks, and "this line wins over that one"
    is a model's opinion worth having. "No rule for this name exists
    anywhere" is not an opinion; it is a claim about make's rule set that is
    either true or false, and this is the only thing in the tool that knows
    which.
    """
    candidates = [
        finding
        for finding in result.findings
        if finding.kind == KIND and finding.file == result.makefile and finding.subject
    ]
    if not candidates:
        return result

    verifier = Verifier(directory, result.makefile, make, variables)
    try:
        return _check_with(result, verifier, candidates)
    finally:
        verifier.cleanup()


def _check_with(result: Result, verifier: Verifier, candidates: list[Finding]) -> Result:
    dropped: set[int] = set()

    for finding in candidates:
        name = finding.subject
        assert name is not None  # the caller filtered on it

        edited = _without_name(verifier.lines, name)
        if edited is None:
            result.notes.append(_unprobed(result.makefile, finding.line, name, "unmade could not find that name as a plain word on a `.PHONY:` line"))
            continue

        try:
            db = verifier.goal(edited, name)
        except Unwritable as exc:
            result.notes.append(_unprobed(result.makefile, finding.line, name, str(exc)))
            continue

        if not _buildable(db, name):
            continue

        dropped.add(id(finding))
        result.notes.append(
            Finding(
                "phony-is-load-bearing",
                finding.file,
                finding.line,
                f"`.PHONY: {name}` has no rule of its own, but take the name "
                f"off that line and make finds one anyway -- a pattern rule, or "
                f"one of its built-ins. The declaration is what stops `make "
                f"{name}` building something, so it is not a dead line",
                subject=name,
            )
        )

    result.findings = [
        finding for finding in result.findings if id(finding) not in dropped
    ]
    return result


def _unprobed(makefile: str, line: int, name: str, why: str) -> Finding:
    """A candidate that could not be tested, kept but labelled as untested.

    The finding stays, because dropping it silently would lose a real one on
    the ordinary continued-line makefile. What does not stay is the
    impression that make agreed with it.
    """
    return Finding(
        "unprobed",
        makefile,
        line,
        f"`.PHONY: {name}` was not tested against make ({why}), so the finding "
        f"for it is unmade's model claiming no rule matches that name, not "
        f"make's answer",
        subject=name,
    )
