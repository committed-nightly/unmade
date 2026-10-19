"""The falsifiable claim, checked against make itself.

unmade's findings all say the same thing: *this line has no effect*. That is
a claim you can test by deleting the line and asking make whether anything
changed. These tests do exactly that, for every finding unmade produces on
every case makefile below.

If a test here fails, unmade told someone to delete a line that mattered.
That is the only failure in this project that is worse than being silent.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from unmade import analyse, database, lex

pytestmark = pytest.mark.make

#: Findings that point at a line which is not, on its own, a safe deletion.
#:
#: `.PHONY: all clean instal` has one dead name on a line whose other names
#: are live, so deleting the line is not the fix and not the test. The phony
#: claim is checked directly in `test_phony_without_rule_does_nothing`.
#:
#: `not-the-default-makefile` and `shadowed-makefile` are about a whole file,
#: and their line number is 1 only because a finding needs one.
NOT_LINE_DELETIONS = frozenset(
    {"phony-without-rule", "not-the-default-makefile", "shadowed-makefile"}
)


def _require_make() -> None:
    if shutil.which("make") is None:  # pragma: no cover - environment
        pytest.skip("GNU make is not on PATH")


def semantics(directory, makefile: str = "Makefile") -> tuple[dict, dict]:
    """What make will *do*, with line numbers deliberately left out.

    Returns the value of every variable that came from a makefile, and the
    recipe text of every target that has one. Deleting a line with no effect
    must leave both of these identical; attribution is excluded because
    deleting a line renumbers everything after it.
    """
    db = database.read(makefile, str(directory))
    variables = {
        name: variable.value
        for name, variable in db.variables.items()
        if variable.origin in ("makefile", "'override' directive")
        and name not in ("MAKEFILE_LIST", "MAKEFLAGS", ".VARIABLES")
    }
    recipes = {}
    for target in db.targets:
        if target.recipe_file and target.recipe_file != "<built-in>":
            recipes.setdefault(target.name, []).append(target.name)
    return variables, recipes


def recipe_text(directory, goal: str, makefile: str = "Makefile") -> str:
    """What make would run for `goal`, as text. `-n` prints, does not run."""
    result = subprocess.run(
        ["make", "-n", "-f", makefile, goal],
        cwd=str(directory),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout


def run_unmade(directory, makefile: str = "Makefile", defines: tuple[str, ...] = ()):
    db = database.read(makefile, str(directory), variables=defines)
    lexed = {}
    for file in db.makefile_list:
        lexed[file] = lex.lex((directory / file).read_text(), file)
    return analyse.analyse(lexed, db, makefile, str(directory))


def assert_deletions_are_harmless(tmp_path, source: str, makefile: str = "Makefile") -> list:
    """Core property: every finding's line can be deleted with no effect."""
    _require_make()
    (tmp_path / makefile).write_text(source)
    result = run_unmade(tmp_path, makefile)

    before_vars, before_recipes = semantics(tmp_path, makefile)
    goals = sorted(before_recipes)
    before_text = {goal: recipe_text(tmp_path, goal, makefile) for goal in goals}

    original = source.split("\n")
    for finding in result.findings:
        if finding.kind in NOT_LINE_DELETIONS or finding.file != makefile:
            continue
        trimmed = [
            line for index, line in enumerate(original, start=1) if index != finding.line
        ]
        (tmp_path / makefile).write_text("\n".join(trimmed))
        try:
            after_vars, after_recipes = semantics(tmp_path, makefile)
            after_text = {goal: recipe_text(tmp_path, goal, makefile) for goal in goals}
        finally:
            (tmp_path / makefile).write_text(source)

        deleted = original[finding.line - 1]
        assert after_vars == before_vars, (
            f"{finding.kind} at line {finding.line} ({deleted!r}) was called dead, "
            f"but deleting it changed make's variables"
        )
        assert after_recipes == before_recipes, (
            f"{finding.kind} at line {finding.line} ({deleted!r}) was called dead, "
            f"but deleting it changed which targets have recipes"
        )
        assert after_text == before_text, (
            f"{finding.kind} at line {finding.line} ({deleted!r}) was called dead, "
            f"but deleting it changed the recipe make would run"
        )
    return result.findings


SUPERSEDED = """\
PREFIX = /usr/local
PREFIX = /opt/example
all: ; @echo $(PREFIX)
"""

APPEND_CHAIN = """\
CFLAGS = -Wall
CFLAGS += -O2
CFLAGS += -g
all: ; @echo $(CFLAGS)
"""

RESET_AFTER_APPEND = """\
CFLAGS = -Wall
CFLAGS += -O2
CFLAGS = -O0
all: ; @echo $(CFLAGS)
"""

CONDITIONAL = """\
CC = gcc
ifeq ($(TOOLCHAIN),clang)
CC = clang
SANITISE = -fsanitize=address
endif
all: ; @echo $(CC) $(SANITISE)
"""

OVERRIDDEN_RECIPE = """\
test:
	@echo first
other: ; @true
test:
	@echo second
"""

OPTIONAL_INCLUDE = """\
-include missing.mk
all: ; @true
"""

QUESTION_AFTER_SET = """\
VERSION = 1.2.0
VERSION ?= 0.0.0
all: ; @echo $(VERSION)
"""

DEFINE_BLOCK = """\
define GREETING
echo one
echo two
endef
NAME = real
all: ; @echo $(NAME)
"""

DOUBLE_COLON = """\
both:: ; @echo first
both:: ; @echo second
"""


@pytest.mark.parametrize(
    "name,source,expected_kinds",
    [
        ("superseded", SUPERSEDED, ["superseded"]),
        ("append chain stays live", APPEND_CHAIN, []),
        ("reset after append kills both", RESET_AFTER_APPEND, ["superseded", "superseded"]),
        ("untaken conditional", CONDITIONAL, ["never-read", "never-read"]),
        ("overridden recipe", OVERRIDDEN_RECIPE, ["overridden-recipe"]),
        ("optional include", OPTIONAL_INCLUDE, ["unread-include"]),
        ("question mark after set", QUESTION_AFTER_SET, ["superseded"]),
        ("define block", DEFINE_BLOCK, []),
        ("double colon rules both run", DOUBLE_COLON, []),
    ],
)
def test_findings_are_safe_deletions(tmp_path, name, source, expected_kinds):
    findings = assert_deletions_are_harmless(tmp_path, source)
    assert sorted(f.kind for f in findings) == sorted(expected_kinds), [
        (f.kind, f.line, f.detail) for f in findings
    ]


def test_append_chain_lines_really_are_live(tmp_path):
    """The converse of the `+=` case, because silence has to be earned.

    unmade says nothing about these three lines. That is only correct if
    deleting them *does* change the value -- otherwise the tool is quiet for
    the wrong reason and would stay quiet after a regression.
    """
    _require_make()
    (tmp_path / "Makefile").write_text(APPEND_CHAIN)
    before, _ = semantics(tmp_path)
    assert before["CFLAGS"] == "-Wall -O2 -g"

    lines = APPEND_CHAIN.split("\n")
    for number in (1, 2, 3):
        trimmed = [line for index, line in enumerate(lines, start=1) if index != number]
        (tmp_path / "Makefile").write_text("\n".join(trimmed))
        after, _ = semantics(tmp_path)
        assert after.get("CFLAGS") != before["CFLAGS"], (
            f"deleting line {number} of an append chain did not change CFLAGS, so "
            f"unmade's silence about it proves nothing"
        )


def test_conditional_verdict_follows_the_invocation(tmp_path):
    """With TOOLCHAIN=clang the dead line and the live line swap places."""
    _require_make()
    (tmp_path / "Makefile").write_text(CONDITIONAL)

    plain = run_unmade(tmp_path)
    assert [(f.kind, f.line) for f in plain.findings] == [
        ("never-read", 3),
        ("never-read", 4),
    ]
    assert not plain.disagreements

    with_clang = run_unmade(tmp_path, defines=("TOOLCHAIN=clang",))
    assert [(f.kind, f.line) for f in with_clang.findings] == [("superseded", 1)]
    assert not with_clang.disagreements


def test_phony_without_rule_does_nothing(tmp_path):
    """The phony claim, checked the way the finding words it."""
    _require_make()
    source = ".PHONY: real instal\nreal:\n\t@echo built\n"
    (tmp_path / "Makefile").write_text(source)

    findings = run_unmade(tmp_path).findings
    assert [(f.kind, f.line) for f in findings] == [("phony-without-rule", 1)]

    # "runs nothing and exits 0" is the whole finding, so assert both halves.
    # make does mention it -- "Nothing to be done for 'instal'." -- but on
    # stdout, with a zero exit. In a CI script that is a green step, which is
    # the reason this finding exists at all.
    result = subprocess.run(
        ["make", "instal"], cwd=str(tmp_path), capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "Nothing to be done for 'instal'." in result.stdout
    assert "built" not in result.stdout

    # ...and the live name on the same line still builds.
    built = subprocess.run(["make", "real"], cwd=str(tmp_path), capture_output=True, text=True)
    assert "built" in built.stdout


def test_command_line_beats_the_makefile_but_not_override(tmp_path):
    _require_make()
    source = "PLAIN = from-file\noverride FORCED = from-file\nall: ; @echo $(PLAIN) $(FORCED)\n"
    (tmp_path / "Makefile").write_text(source)

    result = run_unmade(tmp_path, defines=("PLAIN=cmd", "FORCED=cmd"))
    kinds = [(f.kind, f.line) for f in result.findings]
    assert kinds == [("beaten-by-command-line", 1)], kinds

    # The claim, from make: PLAIN took the command line, FORCED did not.
    ran = subprocess.run(
        ["make", "-s", "PLAIN=cmd", "FORCED=cmd"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert ran.stdout.split() == ["cmd", "from-file"]


def test_environment_loses_to_plain_assignment_and_wins_against_question(tmp_path):
    """The asymmetry worth having a tool for.

    `FOO = x` ignores the environment. `FOO ?= x` defers to it. The two
    lines look interchangeable and are not.
    """
    _require_make()
    source = "PLAIN = from-file\nDEFERRED ?= from-file\nall: ; @echo $(PLAIN) $(DEFERRED)\n"
    (tmp_path / "Makefile").write_text(source)

    db = database.read(
        "Makefile",
        str(tmp_path),
    )
    assert db.variable("PLAIN").origin == "makefile"

    import os

    environment = dict(os.environ, PLAIN="from-env", DEFERRED="from-env")
    ran = subprocess.run(
        ["make", "-s"], cwd=str(tmp_path), capture_output=True, text=True, env=environment
    )
    assert ran.stdout.split() == ["from-file", "from-env"]


def test_question_mode_runs_no_recipe(tmp_path):
    """`make -qp` must not run recipes. The README says so."""
    _require_make()
    (tmp_path / "Makefile").write_text("all:\n\t@touch RECIPE-RAN\n")
    database.read("Makefile", str(tmp_path))
    assert not (tmp_path / "RECIPE-RAN").exists()


def test_shell_function_does_run_under_question_mode(tmp_path):
    """...but `$(shell ...)` does, because make expands it while reading.

    This is make's behaviour and unmade cannot avoid it, so it is tested
    rather than hidden: the README warns about it and this is the test that
    keeps the warning true.
    """
    _require_make()
    (tmp_path / "Makefile").write_text(
        "STAMP := $(shell touch SHELL-RAN; echo done)\nall: ; @true\n"
    )
    database.read("Makefile", str(tmp_path))
    assert (tmp_path / "SHELL-RAN").exists()
