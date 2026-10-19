"""The deletion check, and the real makefile that made it necessary.

`analyse` produces candidates from a precedence model. `verify` deletes each
candidate's line and asks make whether anything changed. These tests pin the
cases where the two disagree, because that gap is the whole reason the
second step exists.
"""

from __future__ import annotations

import os
import shutil
import stat

import pytest

from unmade import analyse, cli, database, lex, verify

pytestmark = pytest.mark.make


@pytest.fixture(autouse=True)
def _needs_make():
    if shutil.which("make") is None:  # pragma: no cover - environment
        pytest.skip("GNU make is not on PATH")


def candidates(tmp_path, source: str, defines: tuple[str, ...] = ()):
    (tmp_path / "Makefile").write_text(source)
    db = database.read("Makefile", str(tmp_path), variables=defines)
    lexed = {f: lex.lex((tmp_path / f).read_text(), f) for f in db.makefile_list}
    return analyse.analyse(lexed, db, "Makefile", str(tmp_path))


def checked(tmp_path, source: str, defines: tuple[str, ...] = ()):
    result = candidates(tmp_path, source, defines)
    return verify.verify(result, str(tmp_path), variables=defines)


# The shape from tools/testing/selftests/Makefile in the Linux tree, cut down
# to the part that matters. `override TARGETS := $(TMP)` resets TARGETS, so a
# precedence model calls every `+=` above it dead -- but TMP was built *from*
# TARGETS, so the reset feeds the old value straight back in.
SELF_REFERENCE_VIA_TMP = """\
TARGETS += acct
TARGETS += bpf
TARGETS += arm64
SKIP_TARGETS ?= bpf
ifneq ($(SKIP_TARGETS),)
	TMP := $(filter-out $(SKIP_TARGETS), $(TARGETS))
	override TARGETS := $(TMP)
endif
all: ; @echo $(TARGETS)
"""

DIRECT_SELF_REFERENCE = """\
CFLAGS = -Wall
CFLAGS := $(CFLAGS) -O2
all: ; @echo $(CFLAGS)
"""

GENUINELY_SUPERSEDED = """\
PREFIX = /usr/local
PREFIX = /opt/example
all: ; @echo $(PREFIX)
"""


def test_model_alone_calls_the_live_lines_dead(tmp_path):
    """Without verification, the selftests shape produces false positives.

    This is the test that justifies the second step. If it ever fails
    because the model got cleverer, good -- but the check stays.
    """
    result = candidates(tmp_path, SELF_REFERENCE_VIA_TMP)
    assert [(f.kind, f.line) for f in result.findings] == [
        ("superseded", 1),
        ("superseded", 2),
        ("superseded", 3),
    ]


def test_verification_rejects_them_and_keeps_the_real_one(tmp_path):
    """...and with verification, only the line that is really dead survives.

    `TARGETS += bpf` has no effect because SKIP_TARGETS filters bpf out
    again. The other two reach the final value. No model unmade could
    reasonably carry would tell those apart; deleting the line does.
    """
    result = checked(tmp_path, SELF_REFERENCE_VIA_TMP)
    assert result.verification == "checked"
    assert [(f.kind, f.line) for f in result.findings] == [("superseded", 2)]
    assert sorted(line for _, line, _ in result.rejected) == [1, 3]


def test_direct_self_reference_is_rejected(tmp_path):
    """`CFLAGS := $(CFLAGS) -O2` keeps the line above it alive."""
    unverified = candidates(tmp_path, DIRECT_SELF_REFERENCE)
    assert [f.line for f in unverified.findings] == [1]

    result = checked(tmp_path, DIRECT_SELF_REFERENCE)
    assert result.findings == []
    assert [line for _, line, _ in result.rejected] == [1]


def test_a_genuine_finding_survives(tmp_path):
    result = checked(tmp_path, GENUINELY_SUPERSEDED)
    assert [(f.kind, f.line) for f in result.findings] == [("superseded", 1)]
    assert result.rejected == []
    assert result.verified == 1


def test_a_clean_set_costs_one_extra_make_run(tmp_path):
    """The fast path: test every candidate at once, and usually stop there.

    Two runs: one baseline through the renamed copy, one with all the
    candidate lines removed.
    """
    result = checked(tmp_path, GENUINELY_SUPERSEDED)
    assert result.make_runs == 2


def test_a_mixed_set_falls_back_to_one_run_per_line(tmp_path):
    result = checked(tmp_path, SELF_REFERENCE_VIA_TMP)
    # baseline + the whole set + one per candidate
    assert result.make_runs == 2 + 3


def test_no_verify_leaves_the_model_in_charge(tmp_path, capsys):
    """`--no-verify` must actually change the answer, or it is a lie."""
    (tmp_path / "Makefile").write_text(DIRECT_SELF_REFERENCE)
    path = str(tmp_path / "Makefile")

    assert cli.main([path, "-q"]) == 0
    assert capsys.readouterr().out == ""

    assert cli.main([path, "-q", "--no-verify"]) == 1
    assert "superseded" in capsys.readouterr().out


def test_an_unwritable_directory_says_so_and_verifies_nothing(tmp_path, capsys):
    """A root-owned tree under /usr/src is the common case for this."""
    (tmp_path / "Makefile").write_text(GENUINELY_SUPERSEDED)
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IXUSR)
    try:
        assert cli.main([str(tmp_path / "Makefile")]) == 1
        out = capsys.readouterr().out
        assert "unverified" in out
        assert "could not write a temporary copy" in out
        # The finding is still reported -- clearly labelled as unchecked.
        assert "superseded" in out
    finally:
        os.chmod(tmp_path, stat.S_IRWXU)


def test_fingerprint_ignores_line_numbers(tmp_path):
    """Deleting a line renumbers the file, so attribution cannot be compared."""
    (tmp_path / "Makefile").write_text("A = 1\nall: ; @echo $(A)\n")
    first = verify.fingerprint(database.read("Makefile", str(tmp_path)))

    (tmp_path / "Makefile").write_text("# a comment that shifts everything\nA = 1\nall: ; @echo $(A)\n")
    second = verify.fingerprint(database.read("Makefile", str(tmp_path)))
    assert first == second


def test_fingerprint_notices_a_changed_recipe(tmp_path):
    (tmp_path / "Makefile").write_text("all:\n\t@echo one\n")
    first = verify.fingerprint(database.read("Makefile", str(tmp_path)))

    (tmp_path / "Makefile").write_text("all:\n\t@echo two\n")
    assert verify.fingerprint(database.read("Makefile", str(tmp_path))) != first


def test_fingerprint_notices_a_changed_value(tmp_path):
    (tmp_path / "Makefile").write_text("A = 1\nall: ; @true\n")
    first = verify.fingerprint(database.read("Makefile", str(tmp_path)))

    (tmp_path / "Makefile").write_text("A = 2\nall: ; @true\n")
    assert verify.fingerprint(database.read("Makefile", str(tmp_path))) != first


def test_a_makefile_that_reads_its_own_name_is_not_verified(tmp_path, capsys):
    """Verification runs a renamed copy, so a makefile that reads
    `$(MAKEFILE_LIST)` would be compared against a different baseline.

    Detected by measuring the unedited file both ways and noticing they
    differ, rather than by looking for the function by name.
    """
    source = "SELF := $(lastword $(MAKEFILE_LIST))\nA = 1\nA = 2\nall: ; @echo $(SELF)\n"
    (tmp_path / "Makefile").write_text(source)
    assert cli.main([str(tmp_path / "Makefile")]) == 1
    out = capsys.readouterr().out
    assert "unverified" in out
    assert "behaves differently when read under another name" in out


def test_phony_and_origin_findings_are_not_deletion_checked(tmp_path):
    """They are excluded on purpose, so the exclusion is pinned.

    `.PHONY: real instal` has one dead name on a line whose other name is
    live. Deleting the line would change the build, so the check would
    throw out a correct finding.
    """
    source = ".PHONY: real instal\nreal:\n\t@echo built\n"
    result = checked(tmp_path, source)
    assert [(f.kind, f.line) for f in result.findings] == [("phony-without-rule", 1)]
    # No candidate was verifiable, so make was never re-run.
    assert result.verification == "off"
    assert result.make_runs == 0
