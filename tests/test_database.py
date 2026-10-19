"""Database parsing, fed captured `make -qp` output.

`read()` needs make on PATH; `parse()` does not, so the shapes that matter
can be pinned without one. The samples below are trimmed from real output.
"""

from __future__ import annotations

import pytest

from unmade import database

SAMPLE = """\
# GNU Make 4.3
# Make data base, printed on Sun Oct 19 00:00:00 2026

# Variables

# environment
HOME = /home/runner
# makefile (from 'Makefile', line 3)
CFLAGS = -Wall -O2 -g
# command line
PREFIX = /opt
# 'override' directive (from 'Makefile', line 9)
FORCED = yes
# default
CC = cc
# makefile (from 'Makefile', line 12)
MAKEFILE_LIST :=  Makefile config.mk
# makefile (from 'Makefile', line 20)
define LONG
one
two
endef
# automatic
@ = target

# Implicit Rules

%.o: %.c
#  recipe to execute (built-in):
	$(COMPILE.c) $(OUTPUT_OPTION) $<

# Files

ghost:
#  Phony target (prerequisite of .PHONY).
#  File does not exist.

all: build install
#  Phony target (prerequisite of .PHONY).
#  File does not exist.

build:
#  Phony target (prerequisite of .PHONY).
#  recipe to execute (from 'Makefile', line 30):
	@echo building

both::
#  recipe to execute (from 'Makefile', line 40):
	@echo one

both::
#  recipe to execute (from 'Makefile', line 42):
	@echo two

# VPATH Search Paths

# Finished Make data base on Sun Oct 19 00:00:01 2026
"""


@pytest.fixture(scope="module")
def db():
    return database.parse(
        SAMPLE,
        stderr="Makefile:42: warning: overriding recipe for target 'both'\n",
    )


def test_origins(db):
    assert db.variable("HOME").origin == "environment"
    assert db.variable("PREFIX").origin == "command line"
    assert db.variable("CC").origin == "default"
    assert db.variable("FORCED").origin == "'override' directive"


def test_locations(db):
    cflags = db.variable("CFLAGS")
    assert (cflags.file, cflags.line) == ("Makefile", 3)
    assert cflags.value == "-Wall -O2 -g"
    assert cflags.located is True

    forced = db.variable("FORCED")
    assert (forced.file, forced.line) == ("Makefile", 9)


def test_an_origin_without_a_location_is_not_located(db):
    assert db.variable("HOME").located is False
    assert db.variable("CC").located is False


def test_define_body_is_captured(db):
    assert db.variable("LONG").value == "one\ntwo"
    assert db.variable("LONG").line == 20


def test_makefile_list(db):
    assert db.makefile_list == ("Makefile", "config.mk")


def test_phony_target_with_no_rule_does_nothing(db):
    ghost = db.targets_named("ghost")[0]
    assert ghost.phony is True
    assert ghost.has_recipe is False
    assert ghost.does_nothing is True


def test_phony_target_with_prerequisites_does_something(db):
    """`all: build install` has no recipe and is not a dead target."""
    everything = db.targets_named("all")[0]
    assert everything.phony is True
    assert everything.has_recipe is False
    assert everything.prerequisites == ("build", "install")
    assert everything.does_nothing is False


def test_target_with_a_recipe(db):
    build = db.targets_named("build")[0]
    assert (build.recipe_file, build.recipe_line) == ("Makefile", 30)
    assert build.does_nothing is False


def test_double_colon_rules_are_separate_entries(db):
    """Both recipes are in the database, because make runs both."""
    entries = db.targets_named("both")
    assert [e.recipe_line for e in entries] == [40, 42]
    assert all(e.double_colon for e in entries)


def test_builtin_recipes_have_no_line(db):
    pattern = db.targets_named("%.o")
    assert pattern == []  # implicit rules are not parsed as file targets


def test_warnings_are_kept(db):
    assert db.warnings == ("Makefile:42: warning: overriding recipe for target 'both'",)


def test_make_refuses_is_not_a_silent_zero(tmp_path):
    """Exit 2 from make raises, rather than parsing the partial database."""
    (tmp_path / "Makefile").write_text("ifeq (1,1)\n")
    with pytest.raises(database.MakeRefused):
        database.read("Makefile", str(tmp_path))


def test_not_gnu_make_raises(tmp_path):
    fake = tmp_path / "fakemake"
    fake.write_text("#!/bin/sh\necho 'bmake 20240101'\n")
    fake.chmod(0o755)
    with pytest.raises(database.MakeUnavailable):
        database.version(str(fake))
