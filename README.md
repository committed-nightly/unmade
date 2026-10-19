# unmade

Say which Makefile GNU make actually reads, and which of its assignments and
recipes never take effect.

If you have inherited a Makefile, this tells you which lines you can delete.
Not by reasoning about make's precedence rules — by reading make's own
database to find the lines that lost, and then deleting each one and running
make again to confirm nothing changed.

## Install

```sh
pip install -e .
```

Needs GNU make on `PATH`. Python 3.10+, no dependencies.

## Use

```sh
unmade                  # the file a bare `make` here would read
unmade path/to/Makefile
unmade -q               # one line per finding
unmade --json
unmade -D TOOLCHAIN=clang   # ask the question your CI asks
```

Exit code is 0 when there is nothing to report, 1 when there is, and 2 when
unmade could not get an answer.

## An example

`examples/Makefile` is a plausible project makefile with the kind of dead
lines that accumulate in a real one.

```
$ cd examples && unmade Makefile
Makefile: make read Makefile.

Makefile:11: superseded
    PREFIX is assigned again; this line has no effect on the value make uses
    make used: Makefile:12

Makefile:17: superseded
    VERSION is already set by then, and `?=` only assigns when it is not; this
    line has no effect on the value make uses
    make used: Makefile:16

Makefile:23: unread-include
    `-include local.mk` read nothing: the file is not in make's list of files
    read, and -include does not complain when it is missing

Makefile:27: never-read
    CC is assigned inside a conditional make did not enter: make attributes CC
    to an earlier line, so this one never ran
    make used: Makefile:5

Makefile:28: never-read
    SANITISE is assigned inside a conditional make did not enter, and is not
    defined anywhere in the database

Makefile:31: phony-without-rule
    `.PHONY: instal` names a target with no rule anywhere, so `make instal`
    runs nothing and exits 0

Makefile:40: overridden-recipe
    a later rule for `test` replaces this recipe; make runs only one recipe
    for a single-colon target
    make used: Makefile:50

7 lines with no effect on this build.
6 of them confirmed by deleting the line and running make again (2 make runs).
```

Every answer is for *this* invocation, and saying so is the point. Add
`-D TOOLCHAIN=clang` and lines 27 and 28 come alive while line 5 — `CC = gcc`
— becomes the dead one.

## What it reports

| kind | what it means |
| --- | --- |
| `superseded` | a later assignment replaces this one, or a `?=` whose variable was already set |
| `never-read` | the line is in a conditional branch make did not enter, or a file it never read |
| `overridden-recipe` | a later rule for the same single-colon target replaces this recipe |
| `unread-include` | `-include` of a file that was not there, which make does not mention |
| `phony-without-rule` | a `.PHONY` name with no rule, so `make <name>` does nothing and exits 0 |
| `beaten-by-command-line` | `make VAR=x` beats the makefile, unless the makefile says `override` |
| `beaten-by-environment` | a `?=` lost to the environment, or `-e` was in play |
| `not-the-default-makefile` | a bare `make` in this directory reads a different file |

## How it decides

Two steps, and the second is the one that matters.

**The database.** `make -qp` prints make's internal database: every variable
with its origin and, when it came from a file, the file and line of the
definition that won; every target with the line of the recipe it would run.
unmade lexes the makefile for lines that claim to do something, then reports
the ones make's database does not name. It never decides which line won.

**The deletion check.** Everything from step one is a *candidate*. A
candidate says "this line has no effect", which is a claim with an experiment
attached: delete the line, run make again, compare the variable values and
recipe text. A candidate that changes either is dropped.

That second step is not belt-and-braces, it is the product. Here is the
makefile that proves it — `tools/testing/selftests/Makefile` from the Linux
tree, which builds `TARGETS` out of 126 `+=` lines and then does this:

```make
SKIP_TARGETS ?= bpf sched_ext
ifneq ($(SKIP_TARGETS),)
	TMP := $(filter-out $(SKIP_TARGETS), $(TARGETS))
	override TARGETS := $(TMP)
endif
```

`override TARGETS := $(TMP)` resets `TARGETS`, so precedence rules say all
126 lines above it are dead. They are not: `TMP` was built *from* `TARGETS`,
so the reset feeds the old value straight back in. The self-reference is two
variables deep and arrives through `filter-out`, and no amount of modelling
in a tool like this is going to catch it.

Deleting a line and looking catches it without needing to understand why. On
that file unmade nominates 129 candidates, throws out 125, and keeps 4 — and
two of the survivors are findings no model would have produced at all:

```
Makefile:6:   superseded: TARGETS is assigned again ...
Makefile:100: superseded: TARGETS is assigned again ...
```

Lines 6 and 100 are `TARGETS += bpf` and `TARGETS += sched_ext`. The default
`SKIP_TARGETS` filters both back out, so those two `+=` lines really do
nothing, while the other 124 reach the build. unmade has no idea why. It
deleted them and nothing changed.

When unmade's own model and make's database disagree about which line won, it
reports nothing for that variable and prints the disagreement — see the
bottom of the output. `$(eval ...)` does this, and so will anything else that
defines a variable where the lexer cannot see it.

### What it costs

One `make -qp` for the database, one for the baseline, and one more if every
candidate is dead — the usual case. When some candidates turn out to be live
it falls back to one run per candidate, which is where the selftests file
spends 136 runs and about 90 seconds. `--no-verify` skips the whole step and
leaves you with what a precedence model thinks, which on that file would be
125 wrong answers.

## Two things to know before you run it

**`$(shell ...)` runs.** `make -qp` does not run recipes — there is a test
that asserts it — but make expands `$(shell ...)` while *reading* a makefile,
before question mode has anything to decide. A makefile whose variables are
computed by shelling out will shell out, once per make run, and the deletion
check makes several. There is no flag for this; it is make's behaviour, and
the only real mitigation is not to point unmade at a makefile you would not
be willing to run.

**It writes a temporary file next to your makefile.** The deletion check
needs the edited copy to sit where the original's `include` lines and
relative paths still resolve, so it writes `.unmade-*.mk` into that directory
and removes it afterwards. On a read-only tree it cannot, and says so rather
than skipping the check silently.

## What it deliberately does not do

- **Pattern and implicit rules.** make's database lists them but says nothing
  about whether one ever matched, so whether `%.o: %.weird` is dead is not a
  question unmade can answer by reading. It stays quiet rather than guessing.
- **Target reachability.** A target nothing depends on is not reported.
- **Target-specific variables** (`test: CFLAGS = -g`) are not modelled, so
  they are neither reported nor mistaken for global assignments.
- **Recursive sub-makes.** unmade analyses one makefile and the files it
  includes. A `$(MAKE) -C sub` is somebody else's database.
- **Other makes.** It reads GNU make's database format and refuses anything
  else by name rather than mis-parsing it.

## Development

```sh
pip install -e ".[dev]"
pytest
ruff check .
```

`tests/test_against_make.py` is the suite worth reading first: for every
finding unmade produces on every case makefile, it deletes the line and
asserts make's behaviour is unchanged. A failure there means unmade told
someone to delete a line that mattered, which is the only outcome in this
project worse than staying silent.

## Licence

MIT.
