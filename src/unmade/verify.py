"""Check each finding by deleting the line and asking make again.

Everything in `analyse` is a *candidate*. A candidate says "this line has no
effect", which is a claim with an obvious experiment attached: delete the
line, run make again, see whether anything changed. This module runs that
experiment, and a finding that fails it is dropped.

That turns the precedence model in `analyse` into a way of finding lines
worth testing rather than a source of truth, which is the only arrangement
that survives contact with real makefiles. The case that forced it, from the
Linux selftests tree:

    TARGETS += acct
    ... 125 more ...
    SKIP_TARGETS ?= bpf sched_ext
    ifneq ($(SKIP_TARGETS),)
        TMP := $(filter-out $(SKIP_TARGETS), $(TARGETS))
        override TARGETS := $(TMP)
    endif

`override TARGETS := $(TMP)` resets TARGETS, so a precedence model says all
126 `+=` lines before it are dead. They are not: `TMP` was built *from*
`TARGETS`, so the reset feeds the old value straight back in. No reasonable
amount of modelling catches that -- the self-reference is two variables deep
and arrives through `filter-out`. Deleting a line and looking does catch it,
every time, and does not need to understand why.
"""

from __future__ import annotations

import os
import tempfile

from . import database
from .analyse import Finding, Result

#: Finding kinds whose claim is "deleting this line changes nothing".
#:
#: The others are excluded for a reason, not for convenience:
#: `phony-without-rule` points at a `.PHONY:` line whose *other* names are
#: live, so the line is not a safe deletion and the finding never said it
#: was. `beaten-by-command-line` and `beaten-by-environment` are read
#: straight off make's own origin label, so there is nothing left to check.
#: `not-the-default-makefile`, `shadowed-makefile` and `unread-file` are
#: about a whole file.
VERIFIABLE = frozenset({"superseded", "never-read", "unread-include", "overridden-recipe"})


class Unwritable(OSError):
    """The makefile's directory cannot be written to, so nothing can be tested."""


def fingerprint(db: database.Database) -> tuple:
    """What make will do, with everything line-numbered left out.

    Deliberately excludes attribution: deleting a line renumbers the rest of
    the file, so comparing locations would report every deletion as a
    change. What has to stay identical is the values and the recipes.
    """
    variables = tuple(
        sorted(
            (name, variable.value)
            for name, variable in db.variables.items()
            if variable.origin in ("makefile", "'override' directive")
            and name not in ("MAKEFILE_LIST", "MAKEFLAGS", "CURDIR", ".VARIABLES")
        )
    )
    targets = tuple(
        sorted(
            (target.name, target.prerequisites, target.recipe_text, target.phony)
            for target in db.targets
        )
    )
    return (variables, targets)


class Verifier:
    """Runs make over edited copies of one makefile.

    The copy lives in the makefile's own directory, because that is the only
    place where its `include` lines, `$(wildcard ...)` calls and relative
    paths mean what they meant. The name starts with a dot and ends in `.mk`
    so a `*.mk` glob in the makefile itself will not pick it up, and it is
    removed whether or not anything raises.
    """

    def __init__(
        self,
        directory: str,
        makefile: str,
        make: str = "make",
        variables: tuple[str, ...] = (),
    ) -> None:
        self.directory = directory
        self.makefile = makefile
        self.make = make
        self.variables = variables
        self.source = open(os.path.join(directory, makefile), encoding="utf-8").read()
        self.lines = self.source.split("\n")
        self.runs = 0
        #: Set when the makefile's behaviour depends on its own filename, so
        #: no deletion can be compared against anything.
        self.unusable: str | None = None
        self.baseline: tuple | None = None

    def _measure(self, text: str) -> tuple | None:
        try:
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.directory,
                prefix=".unmade-",
                suffix=".mk",
                delete=False,
            )
        except OSError as exc:
            # A read-only or root-owned source tree, which is the normal
            # state of anything under /usr/src. Nothing can be verified, and
            # the caller reports that rather than guessing.
            raise Unwritable(str(exc)) from exc
        try:
            handle.write(text)
            handle.close()
            name = os.path.basename(handle.name)
            self.runs += 1
            db = database.read(name, self.directory, self.make, self.variables)
            return fingerprint(db)
        except database.MakeRefused:
            # Deleting the line broke the makefile, so the line mattered.
            return None
        finally:
            try:
                os.unlink(handle.name)
            except OSError:  # pragma: no cover - best effort
                pass

    def prepare(self) -> bool:
        """Measure the unedited file through the same path as every edit.

        If the fingerprint taken from a renamed copy differs from the one
        taken from the real file, the makefile reads its own name -- through
        `$(MAKEFILE_LIST)` or `$(lastword ...)` -- and every later comparison
        would be against the wrong baseline. Say so and verify nothing.
        """
        try:
            direct = fingerprint(
                database.read(self.makefile, self.directory, self.make, self.variables)
            )
        except database.MakeRefused as exc:  # pragma: no cover - caller checked
            self.unusable = str(exc)
            return False

        try:
            copied = self._measure(self.source)
        except Unwritable as exc:
            self.unusable = (
                f"unmade could not write a temporary copy next to {self.makefile} "
                f"({exc}), and a line can only be tested where its makefile's "
                f"relative paths still resolve"
            )
            return False
        if copied is None or copied != direct:
            self.unusable = (
                "this makefile behaves differently when read under another "
                "name, so a line can't be tested by deleting it from a copy"
            )
            return False
        self.baseline = direct
        return True

    def without(self, line_numbers: set[int]) -> tuple | None:
        text = "\n".join(
            line for index, line in enumerate(self.lines, start=1) if index not in line_numbers
        )
        return self._measure(text)

    def unchanged(self, line_numbers: set[int]) -> bool:
        return self.without(line_numbers) == self.baseline


def verify(
    result: Result,
    directory: str,
    make: str = "make",
    variables: tuple[str, ...] = (),
) -> Result:
    """Drop every finding whose line turns out to matter.

    Candidates for the makefile under analysis are tested as a set first,
    which is one make run and is what happens on a file whose findings are
    all genuine. Only when the set test fails does each line get its own
    run, so the slow path costs what it costs on exactly the files where
    being wrong would be expensive.
    """
    candidates = [
        finding
        for finding in result.findings
        if finding.kind in VERIFIABLE and finding.file == result.makefile
    ]
    if not candidates:
        return result

    verifier = Verifier(directory, result.makefile, make, variables)
    if not verifier.prepare():
        result.notes.append(
            Finding(
                "unverified",
                result.makefile,
                1,
                f"{len(candidates)} findings below were not checked by deletion: "
                f"{verifier.unusable}",
            )
        )
        result.verification = "skipped"
        return result

    survivors: list[Finding] = []
    rejected: list[Finding] = []

    everything = {finding.line for finding in candidates}
    try:
        if verifier.unchanged(everything):
            survivors = candidates
        else:
            for finding in candidates:
                if verifier.unchanged({finding.line}):
                    survivors.append(finding)
                else:
                    rejected.append(finding)
    except Unwritable as exc:  # pragma: no cover - prepare() already tested this
        result.notes.append(
            Finding("unverified", result.makefile, 1, f"deletion check stopped: {exc}")
        )
        result.verification = "skipped"
        return result

    kept = set(id(finding) for finding in survivors)
    result.findings = [
        finding
        for finding in result.findings
        if finding.kind not in VERIFIABLE
        or finding.file != result.makefile
        or id(finding) in kept
    ]
    result.verification = "checked"
    result.verified = len(survivors)
    result.rejected = [
        (finding.kind, finding.line, finding.detail) for finding in rejected
    ]
    result.make_runs = verifier.runs
    return result
