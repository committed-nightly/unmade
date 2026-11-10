"""The README's quoted example, compared to a real run in full.

`README.md` quotes `unmade Makefile` on `examples/Makefile` complete, and CI
has a step named for exactly that claim. What the step actually did was
fourteen greps -- a set of lines that have to be *present*. A present-lines
check cannot notice a line that was *added*, and every channel unmade has for
declining to answer adds one: `notes` for a `.PHONY` name it could not probe
or a deletion check it could not run, `disagreements` for a variable its model
and make's database do not agree about. Blind the phony probe and the example
grows an `unprobed:` line, keeps all seven findings and both summary counts,
and every one of those fourteen greps still passes.

`errors` is the worse one. It is how unmade says "make read a file and I could
not open it" -- so the findings came from a partial read -- and it has no
channel but stderr. The CI step redirected stdout and dropped stderr, so a
partial read of the example leaves all seven dogfood steps green.
`test_cli.py::test_json_is_parseable_and_carries_the_detail` does assert
`errors == []`, on a makefile written for that test; what was unwatched is the
run the README quotes.

Two of the greps were also unsound in themselves: `-F` without `-x`, so
`grep -qF '7 lines with no effect on this build.'` passes on a run that
reported 17. That is logbook#64's `prefix` finding for this repo, fixed in
`ci.yml`, and `test_a_substring_grep_on_the_count_line_passes_on_17` is the
part of it that cannot rot back.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from unmade import cli

pytestmark = pytest.mark.make

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "Makefile"

#: The line the README shows being typed. What follows it is the claim.
PROMPT = "$ cd examples && unmade Makefile"

#: Every pattern the CI step greps for over that output. Listed here so the
#: control below can show what they do and do not catch; the first two are the
#: ones that needed `-x`.
CI_PATTERNS = (
    "7 lines with no effect on this build.",
    "6 of them confirmed by deleting the line and running make again (3 make runs).",
    "superseded",
    "unread-include",
    "never-read",
    "phony-without-rule",
    "overridden-recipe",
    "Makefile:11",
    "Makefile:17",
    "Makefile:23",
    "Makefile:27",
    "Makefile:28",
    "Makefile:31",
    "Makefile:40",
)


@pytest.fixture(autouse=True)
def _needs_make():
    if shutil.which("make") is None:  # pragma: no cover - environment
        pytest.skip("GNU make is not on PATH")


def readme_example() -> str:
    """The output half of the README's one `unmade Makefile` block."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"^```[^\n]*\n(.*?)^```", text, re.S | re.M)
    quoted = [block for block in blocks if block.startswith(PROMPT + "\n")]
    assert len(quoted) == 1, f"expected one {PROMPT!r} block, found {len(quoted)}"
    return quoted[0].split("\n", 1)[1]


def test_readme_example_is_a_real_run(capsys):
    status = cli.main([str(EXAMPLE)])
    captured = capsys.readouterr()

    assert captured.out == readme_example()
    assert captured.err == ""
    assert status == 1


def test_the_greps_pass_on_output_the_readme_does_not_describe(capsys):
    """The control: the failure the comparison above exists to catch.

    A `notes` entry is what unmade prints when it declined to answer -- here
    the `.PHONY: instal` probe not running, which is reachable by one edit to
    `phony.py`. The findings, both counts and every line number stay exactly
    as they were, so all fourteen greps pass over a report that now says it
    never asked make about the phony finding at all.
    """
    cli.main([str(EXAMPLE)])
    real = capsys.readouterr().out
    declined = real + (
        "\nMakefile:31: unprobed: `.PHONY: instal` was not tested against make"
        " (unmade could not find that name as a plain word on a `.PHONY:` line),"
        " so the finding for it is unmade's model claiming no rule matches that"
        " name, not make's answer\n"
    )

    missed = [pattern for pattern in CI_PATTERNS if pattern not in declined]
    assert missed == [], f"these greps would have caught it after all: {missed}"
    assert declined != readme_example()


def test_a_substring_grep_on_the_count_line_passes_on_17(tmp_path, capsys):
    """Why `ci.yml` needs `-x` on the two summary assertions.

    `-F` without `-x` matches a whole sentence as a substring, and
    `17 lines ...` contains `7 lines ...`. Sixteen superseded assignments plus
    one unverifiable `.PHONY` name gives 17 findings with 16 verified, which is
    the example's shape an order of magnitude up -- and both of the step's
    count assertions pass on it.
    """
    source = ["all: ; @echo hi", ".PHONY: instal"]
    for index in range(16):
        source += [f"V{index} = a", f"V{index} = b"]
    makefile = tmp_path / "Makefile"
    makefile.write_text("\n".join(source) + "\n")

    cli.main([str(makefile)])
    out = capsys.readouterr().out
    assert "17 lines with no effect on this build." in out

    for pattern in CI_PATTERNS[:2]:
        assert pattern in out, "grep -qF would have failed, so there is no bug here"
        assert pattern not in out.split("\n"), "grep -qxF is the fix and it works"
