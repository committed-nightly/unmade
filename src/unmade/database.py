"""Ask make what it actually did.

`make -qp` prints make's internal database: every variable with the origin
and, for the ones that came from a file, the file and line of the definition
that won; every target with the file and line of the recipe it would run.
That is the whole basis of this tool. unmade does not decide which line won,
it reads make's answer and reports the lines that are not in it.

Two things about `-qp` are worth knowing before you run it on something you
did not write, and both are make's behaviour rather than ours:

* `-q` means question mode, so no recipe runs. Verified in the test suite.
* `$(shell ...)` *does* run, because make expands it while reading the file,
  before question mode has anything to decide. A makefile whose variables
  are computed by shelling out will shell out. There is no flag for this and
  no way to get the database without it.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

#: Long enough for a real project's `$(shell ...)` calls, short enough that a
#: makefile which hangs reports as a hang rather than as a silent tool.
TIMEOUT_SECONDS = 60

_ORIGIN_WITH_LOCATION = re.compile(
    r"^# (?P<origin>makefile|'override' directive)"
    r" \(from '(?P<file>.*)', line (?P<line>\d+)\)$"
)
_ORIGIN_BARE = re.compile(
    r"^# (?P<origin>makefile|environment|command line|default|automatic|"
    r"environment under -e|'override' directive|'default' directive)$"
)
_VARIABLE = re.compile(r"^(?P<name>[^\s=]+) :?\+?\??=")
_DEFINE = re.compile(r"^define (?P<name>\S+)$")
_RECIPE_FROM = re.compile(r"^#  recipe to execute \(from '(?P<file>.*)', line (?P<line>\d+)\):$")
_RECIPE_BUILTIN = re.compile(r"^#  recipe to execute \(built-in\):$")
_TARGET = re.compile(r"^(?P<name>[^\s#:][^:]*)::?(?: |$)")
_PHONY_MARK = "#  Phony target (prerequisite of .PHONY)."


class MakeUnavailable(RuntimeError):
    """`make` is not on PATH, or is not GNU make."""


class MakeRefused(RuntimeError):
    """make would not read the makefile, so there is no database to read.

    This is the important failure. make prints a partial database before it
    gives up, so a tool that parsed whatever it got would answer questions
    about half a file and sound just as confident.
    """


@dataclass(frozen=True)
class Variable:
    name: str
    value: str
    origin: str
    #: Set only when the origin carries a location.
    file: str | None = None
    line: int | None = None

    @property
    def located(self) -> bool:
        return self.file is not None and self.line is not None


@dataclass(frozen=True)
class Target:
    name: str
    phony: bool
    recipe_file: str | None
    recipe_line: int | None
    double_colon: bool
    prerequisites: tuple[str, ...] = ()
    #: The recipe's command lines, as make would run them. Part of the
    #: fingerprint used to verify a finding, so it must not include anything
    #: line-numbered.
    recipe_text: tuple[str, ...] = ()

    @property
    def has_recipe(self) -> bool:
        return self.recipe_file is not None or self.recipe_line is not None

    @property
    def does_nothing(self) -> bool:
        """`make <this>` would run no recipe and build no prerequisite.

        For a phony target that is the whole story: nothing happens, exit 0.
        """
        return not self.has_recipe and not self.prerequisites


@dataclass
class Database:
    variables: dict[str, Variable]
    targets: list[Target]
    #: Files make reports having read, in the order it read them.
    makefile_list: tuple[str, ...]
    #: make's own warnings, which include the overriding-recipe pair.
    warnings: tuple[str, ...]

    def variable(self, name: str) -> Variable | None:
        return self.variables.get(name)

    def targets_named(self, name: str) -> list[Target]:
        return [t for t in self.targets if t.name == name]


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            # A makefile that reads stdin should not get the terminal's.
            stdin=subprocess.DEVNULL,
            env={**os.environ, "LC_ALL": "C"},
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment
        raise MakeUnavailable(f"{args[0]} is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise MakeRefused(
            f"{args[0]} did not finish within {TIMEOUT_SECONDS}s; "
            "something in the makefile is waiting for input or hanging"
        ) from exc


def version(make: str = "make") -> str:
    """First line of `make --version`, for the report header."""
    result = _run([make, "--version"], cwd=".")
    first = (result.stdout or "").splitlines()
    if not first or "GNU Make" not in first[0]:
        raise MakeUnavailable(
            f"{make} is not GNU make; unmade reads GNU make's database and has "
            "nothing to say about other makes"
        )
    return first[0].strip()


def read(
    makefile: str, directory: str, make: str = "make", variables: tuple[str, ...] = ()
) -> Database:
    """Run `make -qp` on `makefile` and parse the database it prints.

    `variables` are passed through as command-line assignments, so you can
    ask the question unmade is actually about: which of my lines lose when
    CI invokes me like *this*.
    """
    version(make)
    args = [make, "-qp", "--no-print-directory", "-f", makefile, *variables]
    result = _run(args, cwd=directory)

    # 0 = goals up to date, 1 = something to do. Both mean make read the
    # file. 2 means it gave up, and the partial database it printed on the
    # way out is not something to draw conclusions from.
    if result.returncode not in (0, 1):
        message = (result.stderr or "").strip().splitlines()
        detail = message[-1] if message else f"make exited {result.returncode}"
        raise MakeRefused(detail)

    return parse(result.stdout, result.stderr)


def parse(stdout: str, stderr: str = "") -> Database:
    """Parse `make -qp` output. Split out from `read` so tests can feed it."""
    variables: dict[str, Variable] = {}
    targets: list[Target] = []

    lines = stdout.split("\n")

    # One `make -qp` can print more than one database. A makefile that runs
    # sub-makes while it is being read passes `-qp` down through MAKEFLAGS,
    # and every one of those children prints its own. The Linux selftests
    # Makefile produces 126 of them, one per entry in TARGETS, each with its
    # own CURDIR -- and the top-level database is the *last*, because the
    # children finish while the parent is still reading.
    #
    # So: keep only the final dump. Parsing the concatenation attributes
    # whichever child got there first to the file in front of you.
    starts = [
        index
        for index, line in enumerate(lines)
        if line.startswith("# Make data base, printed on")
    ]
    if len(starts) > 1:
        lines = lines[starts[-1] :]

    section = "head"
    pending_origin: tuple[str, str | None, int | None] | None = None
    #: make writes `# Not a target:` above an entry that is only ever a
    #: prerequisite. Those are not rules and must not be read as any.
    not_a_target = False

    index = 0
    while index < len(lines):
        line = lines[index]

        if line.startswith("# Variables"):
            section = "variables"
            index += 1
            continue
        if line.startswith("# Implicit Rules") or line.startswith("# Pattern-specific"):
            section = "implicit"
            index += 1
            continue
        if line.startswith("# Files"):
            section = "files"
            index += 1
            continue
        if line.startswith("# VPATH") or line.startswith("# Finished"):
            section = "tail"
            index += 1
            continue

        if section == "variables":
            located = _ORIGIN_WITH_LOCATION.match(line)
            if located:
                pending_origin = (
                    located.group("origin"),
                    located.group("file"),
                    int(located.group("line")),
                )
                index += 1
                continue
            bare = _ORIGIN_BARE.match(line)
            if bare:
                pending_origin = (bare.group("origin"), None, None)
                index += 1
                continue

            if pending_origin is not None and line and not line.startswith("#"):
                origin, file, number = pending_origin
                define = _DEFINE.match(line)
                if define:
                    body: list[str] = []
                    index += 1
                    while index < len(lines) and lines[index] != "endef":
                        body.append(lines[index])
                        index += 1
                    variables[define.group("name")] = Variable(
                        define.group("name"), "\n".join(body), origin, file, number
                    )
                    pending_origin = None
                    index += 1
                    continue
                variable = _VARIABLE.match(line)
                if variable:
                    name = variable.group("name")
                    value = line.split("=", 1)[1].strip() if "=" in line else ""
                    variables[name] = Variable(name, value, origin, file, number)
                pending_origin = None
            index += 1
            continue

        if section == "files":
            if line == "# Not a target:":
                not_a_target = True
                index += 1
                continue
            target = _TARGET.match(line)
            if target and not line.startswith(("#", "\t", " ")):
                if not_a_target:
                    # Skip this entry's whole comment block.
                    not_a_target = False
                    index += 1
                    while index < len(lines) and lines[index].startswith(("#", "\t", " ")):
                        index += 1
                    continue
                name = target.group("name").strip()
                after = line[len(target.group("name")) :]
                double = after.startswith("::")
                prerequisites = tuple(after.lstrip(": \t").split())
                phony = False
                recipe_file: str | None = None
                recipe_line: int | None = None
                recipe_text: list[str] = []
                in_recipe = False
                index += 1
                # The target's comment block runs until a blank line.
                while index < len(lines) and lines[index].startswith(("#", "\t", " ")):
                    detail = lines[index]
                    if detail == _PHONY_MARK:
                        phony = True
                    recipe = _RECIPE_FROM.match(detail)
                    if recipe:
                        recipe_file = recipe.group("file")
                        recipe_line = int(recipe.group("line"))
                        in_recipe = True
                    elif _RECIPE_BUILTIN.match(detail):
                        recipe_file = "<built-in>"
                        in_recipe = True
                    elif in_recipe and detail.startswith("\t"):
                        recipe_text.append(detail[1:])
                    index += 1
                targets.append(
                    Target(
                        name,
                        phony,
                        recipe_file,
                        recipe_line,
                        double,
                        prerequisites,
                        tuple(recipe_text),
                    )
                )
                continue
            index += 1
            continue

        index += 1

    makefile_list: tuple[str, ...] = ()
    listed = variables.get("MAKEFILE_LIST")
    if listed:
        makefile_list = tuple(listed.value.split())

    warnings = tuple(
        entry.strip() for entry in (stderr or "").splitlines() if "warning:" in entry
    )
    return Database(variables, targets, makefile_list, warnings)
