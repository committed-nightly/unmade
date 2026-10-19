"""CLI behaviour: exit codes, channels, and the ways it can refuse to answer.

The refusals matter more than usual here. make prints a partial database
before it gives up on a broken makefile, so the failure mode to guard
against is unmade parsing half a file and exiting 0 with nothing to say --
which reads as "your makefile is clean".
"""

from __future__ import annotations

import json
import shutil

import pytest

from unmade import cli

pytestmark = pytest.mark.make


@pytest.fixture(autouse=True)
def _needs_make():
    if shutil.which("make") is None:  # pragma: no cover - environment
        pytest.skip("GNU make is not on PATH")


def write(tmp_path, source: str, name: str = "Makefile"):
    (tmp_path / name).write_text(source)
    return str(tmp_path / name)


DIRTY = "A = 1\nA = 2\nall: ; @true\n"
CLEAN = "A = 1\nall: ; @echo $(A)\n"


def test_findings_exit_one(tmp_path, capsys):
    assert cli.main([write(tmp_path, DIRTY)]) == 1
    assert "superseded" in capsys.readouterr().out


def test_clean_exits_zero_and_says_so(tmp_path, capsys):
    assert cli.main([write(tmp_path, CLEAN)]) == 0
    assert "Every line unmade can check has an effect" in capsys.readouterr().out


def test_quiet_is_one_line_per_finding(tmp_path, capsys):
    cli.main([write(tmp_path, DIRTY), "-q"])
    out = capsys.readouterr().out.strip().split("\n")
    assert len(out) == 1
    assert out[0].startswith("Makefile:1: superseded:")


def test_quiet_on_a_clean_file_prints_nothing(tmp_path, capsys):
    assert cli.main([write(tmp_path, CLEAN), "-q"]) == 0
    assert capsys.readouterr().out == ""


def test_json_is_parseable_and_carries_the_detail(tmp_path, capsys):
    cli.main([write(tmp_path, DIRTY), "--json"])
    document = json.loads(capsys.readouterr().out)
    assert document["makefiles_read"] == ["Makefile"]
    assert [f["kind"] for f in document["findings"]] == ["superseded"]
    assert document["findings"][0]["line"] == 1
    assert document["findings"][0]["instead"] == "Makefile:2"
    assert document["errors"] == []
    assert "GNU Make" in document["make_version"]


def test_a_makefile_make_refuses_exits_two_and_claims_nothing(tmp_path, capsys):
    """The important one.

    make exits 2 on this and still prints ~35KB of partial database on the
    way out. Parsing that would produce findings about a file make never
    finished reading.
    """
    path = write(tmp_path, "all:\n\t@true\nifeq (1,1)\n")
    assert cli.main([path]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "make could not read it" in captured.err
    assert "missing 'endif'" in captured.err


def test_a_hard_include_of_a_missing_file_exits_two(tmp_path, capsys):
    """`include` (no dash) of a missing file is fatal to make, so to us."""
    path = write(tmp_path, "include nope.mk\nall: ; @true\n")
    assert cli.main([path]) == 2
    assert "nope.mk" in capsys.readouterr().err


def test_missing_makefile_exits_two(tmp_path, capsys):
    assert cli.main([str(tmp_path / "Nope")]) == 2
    assert "no such file" in capsys.readouterr().err


def test_no_makefile_in_directory_names_what_make_looks_for(tmp_path, capsys):
    assert cli.main(["-C", str(tmp_path)]) == 2
    error = capsys.readouterr().err
    assert "GNUmakefile, makefile, Makefile" in error


def test_defaults_to_the_file_make_would_pick(tmp_path, capsys):
    write(tmp_path, CLEAN, "GNUmakefile")
    write(tmp_path, DIRTY, "Makefile")
    assert cli.main(["-C", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "make read GNUmakefile" in out
    # Makefile is never read, so its superseded line is reported as a line in
    # an unread file rather than as a superseded one.
    assert "unread-file" in out


def test_naming_the_shadowed_file_explains_rather_than_inverts(tmp_path, capsys):
    """`unmade Makefile` beside a GNUmakefile must not call GNUmakefile dead.

    With `-f Makefile` make really does read Makefile, so that file's lines
    are the live ones for this invocation. The useful thing to say is that a
    bare `make` would not have read it at all.
    """
    write(tmp_path, CLEAN, "GNUmakefile")
    assert cli.main([write(tmp_path, DIRTY, "Makefile")]) == 1
    out = capsys.readouterr().out
    assert "not-the-default-makefile" in out
    assert "a bare `make` in this directory reads GNUmakefile" in out
    assert "unread-file" not in out


def test_define_requires_an_equals(tmp_path, capsys):
    assert cli.main([write(tmp_path, CLEAN), "-D", "NOPE"]) == 2
    assert "expected VAR=VALUE" in capsys.readouterr().err


def test_define_is_passed_through_to_make(tmp_path, capsys):
    cli.main([write(tmp_path, CLEAN), "-D", "A=cmd", "-q"])
    assert "beaten-by-command-line" in capsys.readouterr().out


def test_not_gnu_make_exits_two(tmp_path, capsys):
    """unmade reads GNU make's database. Anything else, say so and stop."""
    fake = tmp_path / "fakemake"
    fake.write_text("#!/bin/sh\necho 'bmake 20240101'\n")
    fake.chmod(0o755)
    assert cli.main([write(tmp_path, CLEAN), "--make", str(fake)]) == 2
    assert "is not GNU make" in capsys.readouterr().err


def test_disagreements_are_printed_not_swallowed(tmp_path, capsys):
    """A variable unmade declines to judge must still be mentioned.

    `$(eval ...)` defines FOO where the lexer cannot see it, so the model's
    idea of the winning line and make's disagree. The contract is that this
    produces no finding *and* no silence.
    """
    source = "FOO = first\n$(eval FOO = second)\nall: ; @echo $(FOO)\n"
    cli.main([write(tmp_path, source)])
    out = capsys.readouterr().out
    assert "unmade said nothing about these variables" in out
    assert "FOO" in out
    assert "superseded" not in out
