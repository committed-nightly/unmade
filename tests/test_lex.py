"""Lexer tests, concentrated on the lines that look like something else.

A recipe line is shell, not make. A `define` body is a value, not syntax. A
target-specific variable is not a variable unmade models. Getting any of
these wrong invents findings out of nothing, which is why they are here.
"""

from __future__ import annotations

from unmade import lex


def names(parsed) -> list[tuple[str, str, int]]:
    return [(a.name, a.operator, a.line) for a in parsed.assignments]


def test_operators_and_lines():
    parsed = lex.lex(
        "A = 1\nB := 2\nC ::= 3\nD += 4\nE ?= 5\nF != echo 6\n",
        "Makefile",
    )
    assert names(parsed) == [
        ("A", "=", 1),
        ("B", ":=", 2),
        ("C", "::=", 3),
        ("D", "+=", 4),
        ("E", "?=", 5),
        ("F", "!=", 6),
    ]


def test_recipe_lines_are_not_assignments():
    """`FOO=bar` under a tab is shell. It must not look like a make variable."""
    parsed = lex.lex("all:\n\tFOO=bar make -C sub\n\techo done # not a comment\n", "Makefile")
    assert names(parsed) == []
    assert parsed.rules[0].targets == ("all",)
    assert parsed.rules[0].recipe_line == 2


def test_define_body_is_skipped():
    parsed = lex.lex(
        "define BLOCK\nINSIDE = not-a-real-assignment\necho hi\nendef\nAFTER = yes\n",
        "Makefile",
    )
    assert names(parsed) == [("BLOCK", "=", 1), ("AFTER", "=", 5)]


def test_define_with_operator():
    parsed = lex.lex("define BLOCK :=\nbody\nendef\n", "Makefile")
    assert names(parsed) == [("BLOCK", ":=", 1)]


def test_target_specific_variable_is_not_a_global():
    """`test: CFLAGS = -g` sets CFLAGS for one target only.

    unmade does not model target-specific variables, so the right behaviour
    is to record nothing rather than to record a global CFLAGS.
    """
    parsed = lex.lex("CFLAGS = -O2\ntest: CFLAGS = -g\ntest: ; @true\n", "Makefile")
    assert names(parsed) == [("CFLAGS", "=", 1)]


def test_continuation_reports_the_first_line():
    parsed = lex.lex("SOURCES = a.c \\\n    b.c \\\n    c.c\nNEXT = 1\n", "Makefile")
    assert names(parsed) == [("SOURCES", "=", 1), ("NEXT", "=", 4)]


def test_comments_and_escaped_hash():
    parsed = lex.lex("A = 1 # trailing\n# B = 2\nC = \\#literal\n", "Makefile")
    assert names(parsed) == [("A", "=", 1), ("C", "=", 3)]


def test_rule_with_colon_in_an_expansion():
    """A target that expands to text containing a colon is still a rule."""
    parsed = lex.lex("$(shell echo a:b): dep\n\t@true\n", "Makefile")
    assert names(parsed) == []
    assert len(parsed.rules) == 1


def test_assignment_wins_when_the_colon_is_the_operator():
    parsed = lex.lex("VAR := value\n", "Makefile")
    assert names(parsed) == [("VAR", ":=", 1)]
    assert parsed.rules == []


def test_double_colon_is_flagged():
    parsed = lex.lex("both:: ; @echo one\nboth:: ; @echo two\n", "Makefile")
    assert [r.double_colon for r in parsed.rules] == [True, True]
    assert [r.recipe_line for r in parsed.rules] == [1, 2]


def test_inline_recipe_without_a_body_has_no_recipe():
    parsed = lex.lex("all: dep ;\n", "Makefile")
    assert parsed.rules[0].has_recipe is False


def test_phony_names_are_collected():
    parsed = lex.lex(".PHONY: all clean\n.PHONY: test\n", "Makefile")
    assert parsed.phony == [("all", 1), ("clean", 1), ("test", 2)]


def test_conditional_depth_is_tracked():
    parsed = lex.lex("ifeq ($(X),1)\nA = 1\nelse\nB = 2\nendif\nC = 3\n", "Makefile")
    conditional = {a.name: a.conditional for a in parsed.assignments}
    assert conditional == {"A": True, "B": True, "C": False}


def test_override_and_export_prefixes():
    parsed = lex.lex("override A = 1\nexport B = 2\nexport C\n", "Makefile")
    assert names(parsed) == [("A", "=", 1), ("B", "=", 2)]
    assert parsed.assignments[0].overrides is True
    assert parsed.assignments[1].overrides is False
    # `export C` declares nothing, so it is not an assignment.
    assert any(u.line == 3 for u in parsed.unmodelled)


def test_computed_variable_names_are_unmodelled():
    """`$(prefix)_FLAGS = x` has a name unmade cannot know. Never guess it."""
    parsed = lex.lex("$(prefix)_FLAGS = x\n", "Makefile")
    assert names(parsed) == []
    assert len(parsed.unmodelled) == 1


def test_includes():
    parsed = lex.lex("include a.mk\n-include b.mk\nsinclude c.mk\n", "Makefile")
    assert [(i.kind, i.operand, i.line) for i in parsed.includes] == [
        ("include", "a.mk", 1),
        ("-include", "b.mk", 2),
        ("sinclude", "c.mk", 3),
    ]
