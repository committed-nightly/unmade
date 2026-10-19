"""The precedence model on its own, including the case that motivated it.

`_live_and_dead` is the only place in unmade that reasons about make's rules
rather than reading make's answer, so it is the only place a wrong idea can
hide. It is also checked against make in `test_against_make.py`; these tests
are the fast, exhaustive version.
"""

from __future__ import annotations

from unmade import lex
from unmade.analyse import _live_and_dead


def build(*pairs: tuple[str, str]) -> list[lex.Assignment]:
    """`build(("=", "a"), ("+=", "b"))` -> assignments on lines 1, 2, ..."""
    return [
        lex.Assignment("V", operator, line, "Makefile")
        for line, (operator, _) in enumerate(pairs, start=1)
    ]


def verdict(*pairs: tuple[str, str]) -> tuple[list[int], int | None]:
    dead, winner = _live_and_dead(build(*pairs))
    return [a.line for a in dead], (winner.line if winner else None)


def test_single_assignment():
    assert verdict(("=", "a")) == ([], 1)


def test_reassignment_kills_the_first():
    assert verdict(("=", "a"), ("=", "b")) == ([1], 2)


def test_append_kills_nothing():
    assert verdict(("=", "a"), ("+=", "b"), ("+=", "c")) == ([], 3)


def test_reset_after_append_kills_the_whole_chain():
    """The case a naive diff gets wrong in the other direction.

    make attributes the variable to the last line that touched it. With
    `= / += / =`, that is line 3 -- and *both* line 1 and line 2 are dead.
    Reporting only line 2, because it is the one make stopped naming, would
    leave a dead line in place and look like a considered decision.
    """
    assert verdict(("=", "a"), ("+=", "b"), ("=", "c")) == ([1, 2], 3)


def test_long_alternating_chain():
    assert verdict(
        ("=", "a"),
        ("+=", "b"),
        ("=", "c"),
        ("+=", "d"),
        ("+=", "e"),
    ) == ([1, 2], 5)


def test_question_mark_after_a_set_is_dead():
    assert verdict(("=", "a"), ("?=", "b")) == ([2], 1)


def test_question_mark_first_wins():
    assert verdict(("?=", "a")) == ([], 1)


def test_question_mark_then_reset():
    assert verdict(("?=", "a"), ("=", "b")) == ([1], 2)


def test_two_question_marks():
    """The second cannot fire, because the first has set the variable."""
    assert verdict(("?=", "a"), ("?=", "b")) == ([2], 1)


def test_append_to_unset_then_question_mark():
    """`+=` on an unset variable sets it, so a later `?=` does nothing."""
    assert verdict(("+=", "a"), ("?=", "b")) == ([2], 1)


def test_bang_equals_resets():
    assert verdict(("=", "a"), ("!=", "echo b")) == ([1], 2)


def test_colon_equals_resets():
    assert verdict((":=", "a"), ("::=", "b")) == ([1], 2)


def test_three_plain_assignments_kill_two():
    assert verdict(("=", "a"), ("=", "b"), ("=", "c")) == ([1, 2], 3)


def test_dead_lines_come_back_in_file_order():
    dead, _ = _live_and_dead(build(("=", "a"), ("+=", "b"), ("=", "c")))
    assert [a.line for a in dead] == sorted(a.line for a in dead)


def test_empty():
    assert verdict() == ([], None)
