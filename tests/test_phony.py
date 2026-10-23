"""What a `.PHONY:` name is actually doing, checked against make.

Every test here that says a line is live proves it the only way that counts:
it runs make with the line and without it, and shows the two differ. A test
asserting "unmade no longer reports this" would pass just as well if the
check had been deleted, so each of those is paired with a control makefile
where the finding must still fire.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from unmade import analyse, cli, database, lex, phony

pytestmark = pytest.mark.make


def _require_make() -> None:
    if shutil.which("make") is None:  # pragma: no cover - environment
        pytest.skip("GNU make is not on PATH")


def analysed(directory, makefile: str = "Makefile"):
    """analyse + the phony probe, which is what the CLI runs."""
    db = database.read(makefile, str(directory))
    source = (directory / makefile).read_text()
    lexed = {makefile: lex.lex(source, makefile)}
    result = analyse.analyse(lexed, db, makefile, str(directory))
    return phony.check(result, str(directory))


def kinds(result) -> list[tuple[str, int]]:
    return [(f.kind, f.line) for f in result.findings]


def note_kinds(result) -> list[str]:
    return [note.kind for note in result.notes]


def make(directory, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["make", *args], cwd=str(directory), capture_output=True, text=True
    )


# --------------------------------------------------------------------------
# The two shapes that were being called dead.
# --------------------------------------------------------------------------


def test_a_phony_name_a_built_in_rule_could_match_is_not_dead(tmp_path):
    """`.PHONY: hello` next to a `hello.c` is the only reason nothing builds.

    make does not search implicit rules for a phony target, so the empty
    database entry this produces is what a *working* declaration looks like.
    """
    _require_make()
    (tmp_path / "hello.c").write_text("int main(void){return 0;}\n")
    live = "all: ; @echo real\n.PHONY: hello\n"
    (tmp_path / "Makefile").write_text(live)

    result = analysed(tmp_path)
    assert kinds(result) == []
    assert "phony-is-load-bearing" in note_kinds(result)

    # The counterfactual, which is the whole claim: take the line away and
    # make compiles a binary. If this stopped being true the test above
    # would be asserting nothing.
    assert make(tmp_path, "hello").returncode == 0
    assert not (tmp_path / "hello").exists()

    (tmp_path / "Makefile").write_text("all: ; @echo real\n")
    assert make(tmp_path, "hello").returncode == 0
    assert (tmp_path / "hello").exists()


def test_the_stock_sphinx_catch_all_is_not_dead(tmp_path):
    """The shape from /usr/share/doc that found this bug.

    A `%: Makefile` catch-all matches `Makefile` itself, so without the
    declaration make tries to rebuild its own makefile through it.
    """
    _require_make()
    source = (
        "help: ; @echo helping\n"
        ".PHONY: help Makefile\n"
        "%: Makefile\n"
        "\t@touch BUILT-$@\n"
    )
    (tmp_path / "Makefile").write_text(source)

    result = analysed(tmp_path)
    assert kinds(result) == []
    assert "phony-is-load-bearing" in note_kinds(result)

    # With the name phony, `make Makefile` does nothing at all.
    make(tmp_path, "Makefile")
    assert not (tmp_path / "BUILT-Makefile").exists()

    # Without it, the catch-all claims the makefile. make drops the circular
    # dependency and says so, which is the observable difference.
    (tmp_path / "Makefile").write_text(source.replace(".PHONY: help Makefile", ".PHONY: help"))
    result = make(tmp_path, "Makefile")
    assert "Circular" in result.stderr + result.stdout


def test_a_phony_prerequisite_that_forces_a_rebuild_is_not_dead(tmp_path):
    """A phony prerequisite is never up to date, so its parent always runs.

    `does_nothing` is blind to this: it only looks at the target itself, and
    `version.h` has no recipe and no prerequisites of its own.
    """
    _require_make()
    (tmp_path / "version.h").write_text("ver\n")
    body = "prog: version.h\n\t@echo built >> runs.log; touch prog\n"
    (tmp_path / "Makefile").write_text(body + ".PHONY: version.h\n")

    result = analysed(tmp_path)
    assert kinds(result) == []

    def builds() -> int:
        (tmp_path / "prog").unlink(missing_ok=True)
        (tmp_path / "runs.log").write_text("")
        for _ in range(3):
            make(tmp_path, "prog")
        return len((tmp_path / "runs.log").read_text().splitlines())

    assert builds() == 3
    (tmp_path / "Makefile").write_text(body)
    assert builds() == 1


# --------------------------------------------------------------------------
# Controls. Without these, deleting the check would pass the tests above.
# --------------------------------------------------------------------------


def test_a_genuinely_dead_phony_name_still_fires(tmp_path):
    """Nothing on this machine can build `instal`, so the finding stands."""
    _require_make()
    (tmp_path / "Makefile").write_text("real: ; @echo built\n.PHONY: real instal\n")

    result = analysed(tmp_path)
    assert kinds(result) == [("phony-without-rule", 2)]
    assert note_kinds(result) == []

    # And the finding's own wording holds: nothing runs, exit 0.
    ran = make(tmp_path, "instal")
    assert ran.returncode == 0
    assert "built" not in ran.stdout


def test_the_dead_name_is_named_so_the_live_one_on_the_line_survives(tmp_path):
    """One line, one live name, one dead name: only the dead one is reported."""
    _require_make()
    (tmp_path / "Makefile").write_text("build: ; @echo b\n.PHONY: build clean\n")

    result = analysed(tmp_path)
    assert kinds(result) == [("phony-without-rule", 2)]
    assert [f.subject for f in result.findings] == ["clean"]


def test_a_special_target_does_not_count_as_needing_a_name(tmp_path):
    """`.PHONY: clean` is a target `.PHONY` with a prerequisite `clean`.

    Pinned because getting this wrong silences the check completely rather
    than loudly: every declared name looks needed by something, so every
    finding disappears and the suite still goes green on the tests above.
    """
    _require_make()
    (tmp_path / "Makefile").write_text("all: ; @echo real\n.PHONY: clean\n.SILENT: clean\n")

    result = analysed(tmp_path)
    assert kinds(result) == [("phony-without-rule", 2)]


# --------------------------------------------------------------------------
# The probe itself.
# --------------------------------------------------------------------------


def test_read_goal_resolves_a_rule_the_default_goal_hides(tmp_path):
    """Naming the goal is what makes the implicit rule appear at all."""
    _require_make()
    (tmp_path / "hello.c").write_text("int main(void){return 0;}\n")
    (tmp_path / "Makefile").write_text("all: ; @echo real\n")

    default = database.read("Makefile", str(tmp_path))
    assert default.targets_named("hello") == []

    asked = database.read_goal("Makefile", str(tmp_path), "hello")
    assert asked is not None
    assert [t.prerequisites for t in asked.targets_named("hello")] == [("hello.c",)]


def test_read_goal_is_none_when_make_cannot_produce_the_goal(tmp_path):
    _require_make()
    (tmp_path / "Makefile").write_text("all: ; @echo real\n")
    assert database.read_goal("Makefile", str(tmp_path), "clean") is None


def test_the_probe_does_not_run_a_makefile_remaking_recipe(tmp_path):
    """`--old-file` earns its place, asserted in both directions.

    A rule that remakes a makefile runs its recipe even under `-q`, and the
    edit this probe makes is exactly the one that can expose such a rule --
    removing `.PHONY: Makefile` lets a catch-all `%:` claim the makefile. So
    the probe pins the makefile as old. The second half of this test drops
    that flag and watches the recipe run, because an assertion about
    something *not* happening is worth nothing until you have seen it fail.
    """
    _require_make()
    source = "all: ; @echo real\n%: force\n\t@touch REMAKE-RAN\nforce: ;\n"
    (tmp_path / "Makefile").write_text(source)

    assert database.read_goal("Makefile", str(tmp_path), "clean") is not None
    assert not (tmp_path / "REMAKE-RAN").exists()

    # Same question, same makefile, without the flag.
    subprocess.run(
        ["make", "-qp", "--no-print-directory", "-f", "Makefile", "clean"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert (tmp_path / "REMAKE-RAN").exists()


def test_a_name_the_edit_cannot_reach_is_kept_but_labelled_untested(tmp_path):
    """`.PHONY .SILENT: clean` declares one name through two targets.

    Taking `clean` off that line would stop it being silent as well as
    stop it being phony, so the probe would be answering a question nobody
    asked. It declines, and says so: the finding stays, because dropping it
    would lose real ones, but it is labelled so nobody reads it as make's
    answer.
    """
    _require_make()
    (tmp_path / "Makefile").write_text("all: ; @echo real\n.PHONY .SILENT: clean\n")

    result = analysed(tmp_path)
    assert kinds(result) == [("phony-without-rule", 2)]
    assert note_kinds(result) == ["unprobed"]
    # make really does honour it, so this is a live shape and not a strawman.
    db = database.read("Makefile", str(tmp_path))
    assert [t.phony for t in db.targets_named("clean")] == [True]


def test_the_edit_handles_the_spellings_make_honours(tmp_path):
    """make takes `.PHONY:clean` and `.PHONY : clean`; both are on this box.

    Asserted against make rather than assumed, because the whole point of
    reaching these is that an unreachable name goes untested.
    """
    _require_make()
    (tmp_path / "hello.c").write_text("int main(void){return 0;}\n")
    for line in (".PHONY:hello", ".PHONY : hello", ".PHONY: hello"):
        (tmp_path / "Makefile").write_text(f"all: ; @echo real\n{line}\n")
        # make honours the declaration...
        db = database.read("Makefile", str(tmp_path))
        assert [t.phony for t in db.targets_named("hello")] == [True], line
        # ...and so the probe has to reach it, or the finding stands wrongly.
        result = analysed(tmp_path)
        assert kinds(result) == [], line
        assert note_kinds(result) == ["phony-is-load-bearing"], line


def test_without_name_keeps_the_rest_of_the_line():
    assert phony._without_name([".PHONY: a b c"], "b") == [".PHONY: a c"]
    assert phony._without_name([".PHONY: only"], "only") == [""]
    assert phony._without_name([".PHONY:a b"], "a") == [".PHONY: b"]
    assert phony._without_name([".PHONY : a b"], "a") == [".PHONY : b"]
    assert phony._without_name([".PHONY: $(NAMES)"], "clean") is None
    # A continued declaration keeps its continuation, and a name on the
    # continuation line is reachable too.
    assert phony._without_name([".PHONY: a b \\", "    c"], "b") == [".PHONY: a \\", "    c"]
    assert phony._without_name([".PHONY: a \\", "    b c"], "b") == [".PHONY: a \\", "c"]
    # Every declaration goes, not just the first: a second line naming it
    # would leave the probe answering a different question.
    assert phony._without_name([".PHONY: a", "x: ; @true", ".PHONY: a b"], "a") == [
        "",
        "x: ; @true",
        ".PHONY: b",
    ]


def test_the_probe_runs_even_under_no_verify(tmp_path, capsys):
    """--no-verify turns off the deletion check, not this.

    "This line loses to that one" is a model's opinion and `--no-verify`
    leaves you with it on purpose. "No rule for this name exists anywhere"
    is not an opinion, and make is the only thing that knows.
    """
    _require_make()
    (tmp_path / "hello.c").write_text("int main(void){return 0;}\n")
    (tmp_path / "Makefile").write_text("all: ; @echo real\n.PHONY: hello\n")

    assert cli.main([str(tmp_path / "Makefile"), "--no-verify"]) == 0
    out = capsys.readouterr().out
    assert "phony-without-rule" not in out
    assert "phony-is-load-bearing" in out
