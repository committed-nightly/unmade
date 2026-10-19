"""Find the lines in a makefile that *claim* to do something.

This is deliberately not a makefile parser. It never evaluates anything, and
it is allowed to shrug: a logical line it does not recognise becomes an
``Unmodelled`` record and nothing downstream will ever report on it. Every
claim unmade makes is settled later against make's own database, so the only
job here is to find candidate lines and their numbers without being clever.

The one thing this module must get right is *which physical line a construct
starts on*, because that is what gets printed and what a reader goes and
looks at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Assignment operators, longest first so `::=` wins over `:=` and `=`.
_OPERATORS = ("::=", ":::=", ":=", "+=", "?=", "!=", "=")

#: Operators that *reset* a variable: everything assigned before one of these
#: is gone. `+=` appends and `?=` may do nothing, so neither resets.
RESETTING = frozenset({"=", ":=", "::=", ":::=", "!="})

_CONDITIONAL_OPEN = re.compile(r"^\s*(ifeq|ifneq|ifdef|ifndef)\b")
_CONDITIONAL_ELSE = re.compile(r"^\s*else\b")
_CONDITIONAL_CLOSE = re.compile(r"^\s*endif\b")
_INCLUDE = re.compile(r"^\s*(include|-include|sinclude)\s+(.*)$")
_DEFINE = re.compile(r"^\s*define\s+(\S+)\s*(::=|:::=|:=|\+=|\?=|=)?\s*$")
_ENDEF = re.compile(r"^\s*endef\b")

#: Directives that can precede an assignment. `private` and `unexport` are
#: here so their lines lex as assignments rather than as mystery text.
_PREFIXES = ("override", "export", "unexport", "private")

_NAME = re.compile(r"^[A-Za-z_.][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class Assignment:
    """A variable assignment, as written."""

    name: str
    operator: str
    line: int
    file: str
    overrides: bool = False
    #: True when this line sits inside an `ifeq`/`ifdef` block. Only used to
    #: explain *why* make never read a line, never to decide that it didn't.
    conditional: bool = False


@dataclass(frozen=True)
class Rule:
    """A rule line. `targets` is literal text, so it may contain `$(...)`."""

    targets: tuple[str, ...]
    line: int
    file: str
    double_colon: bool
    has_recipe: bool
    conditional: bool = False
    #: First physical line of the recipe, which is what make's database
    #: attributes a recipe to. Equal to `line` for a `target: ; cmd` rule.
    recipe_line: int | None = None


@dataclass(frozen=True)
class Include:
    kind: str  # "include", "-include" or "sinclude"
    operand: str
    line: int
    file: str
    conditional: bool = False


@dataclass(frozen=True)
class Unmodelled:
    """A logical line this module declined to classify.

    Carried through on purpose. A file full of these is a file unmade should
    be quiet about, and counting them is how the tests notice that a
    construct stopped being understood.
    """

    text: str
    line: int
    file: str


@dataclass
class Lexed:
    file: str
    assignments: list[Assignment] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    includes: list[Include] = field(default_factory=list)
    unmodelled: list[Unmodelled] = field(default_factory=list)
    #: Names appearing as prerequisites of `.PHONY`, with the line they were
    #: declared on.
    phony: list[tuple[str, int]] = field(default_factory=list)

    @property
    def targets_with_recipes(self) -> list[Rule]:
        return [r for r in self.rules if r.has_recipe]


def _strip_comment(text: str) -> str:
    """Remove an unescaped `#` comment. Not used on recipe lines."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] == "#":
            out.append("#")
            i += 2
            continue
        if ch == "#":
            break
        out.append(ch)
        i += 1
    return "".join(out)


def _split_operator(text: str) -> tuple[str, str, str] | None:
    """Split `text` at its assignment operator.

    Returns `(before, operator, after)` for the *first* operator in the line,
    choosing the longest match at that position. `None` when there is none.
    """
    for i in range(len(text)):
        for op in _OPERATORS:
            if text.startswith(op, i):
                return text[:i], op, text[i + len(op) :]
    return None


def _first_colon_outside_parens(text: str) -> int:
    """Index of the first `:` that is not inside `$(...)`/`${...}`.

    `$(patsubst %:,%,$x): dep` is a rule whose target is an expansion
    containing a colon, so a naive `find(":")` splits in the wrong place.
    """
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "$" and i + 1 < len(text) and text[i + 1] in "({":
            depth += 1
            i += 2
            continue
        if depth and ch in ")}":
            depth -= 1
        elif not depth and ch == ":":
            return i
        i += 1
    return -1


def _classify(
    logical: str, line: int, file: str, conditional: bool, has_recipe: bool
) -> Assignment | Rule | Include | Unmodelled | None:
    body = _strip_comment(logical).strip()
    if not body:
        return None

    include = _INCLUDE.match(body)
    if include:
        return Include(include.group(1), include.group(2).strip(), line, file, conditional)

    # Directive prefixes. `export FOO` on its own is not an assignment, so
    # only keep the prefix if what follows still looks like one.
    overrides = False
    stripped = body
    while True:
        head = stripped.split(None, 1)
        if len(head) == 2 and head[0] in _PREFIXES:
            if head[0] == "override":
                overrides = True
            stripped = head[1]
            continue
        break

    colon = _first_colon_outside_parens(stripped)
    split = _split_operator(stripped)

    # A rule when a bare colon comes first. `FOO := x` has its colon *as* the
    # operator, so compare positions: the operator wins when it starts at or
    # before the colon.
    if colon >= 0 and (split is None or colon < len(split[0])):
        targets = tuple(t for t in stripped[:colon].split() if t)
        if not targets:
            return Unmodelled(body, line, file)
        rest = stripped[colon:]
        double = rest.startswith("::")
        # `target: VAR = value` is a target-specific variable, which unmade
        # does not model. It still lexes as a rule so the recipe logic is
        # right, and the assignment is simply not recorded anywhere.
        return Rule(targets, line, file, double, has_recipe, conditional)

    if split is None:
        return Unmodelled(body, line, file)

    name, operator, _ = split
    name = name.strip()
    if not _NAME.match(name):
        # Computed names (`$(p)_FLAGS = x`) and anything else surprising.
        return Unmodelled(body, line, file)
    return Assignment(name, operator, line, file, overrides, conditional)


def lex(text: str, file: str) -> Lexed:
    """Lex one makefile's text. `file` is the name make knows it by."""
    out = Lexed(file=file)
    lines = text.split("\n")

    conditional_depth = 0
    i = 0
    # Set once a rule has been seen, so a tab-indented line can be told from
    # an assignment that merely happens to be indented.
    in_recipe_context = False

    def flush_recipe_flag(target_line: int, recipe_line: int) -> None:
        """Mark the rule at `target_line` as having a recipe.

        `recipe_line` is the recipe's *first* line and only takes effect
        once, because that is the one make's database names.
        """
        for index in range(len(out.rules) - 1, -1, -1):
            if out.rules[index].line == target_line:
                rule = out.rules[index]
                out.rules[index] = Rule(
                    rule.targets,
                    rule.line,
                    rule.file,
                    rule.double_colon,
                    True,
                    rule.conditional,
                    rule.recipe_line if rule.recipe_line is not None else recipe_line,
                )
                return

    last_rule_line: int | None = None

    while i < len(lines):
        raw = lines[i]
        start = i + 1

        # A tab-indented line inside a rule's recipe is shell text. Never
        # look at it: `FOO=bar` there is a shell assignment, not a make one,
        # and a `#` there is a shell comment.
        if raw.startswith("\t") and in_recipe_context:
            if last_rule_line is not None and raw.strip():
                flush_recipe_flag(last_rule_line, start)
            # A recipe line may itself continue.
            while raw.endswith("\\") and i + 1 < len(lines):
                i += 1
                raw = lines[i]
            i += 1
            continue

        # Join continuations into one logical line.
        logical = raw
        while logical.endswith("\\") and i + 1 < len(lines):
            i += 1
            logical = logical[:-1] + lines[i]

        bare = _strip_comment(logical).strip()

        if _DEFINE.match(bare):
            match = _DEFINE.match(bare)
            assert match is not None
            name = match.group(1)
            operator = match.group(2) or "="
            if _NAME.match(name):
                out.assignments.append(
                    Assignment(name, operator, start, file, False, conditional_depth > 0)
                )
            else:
                out.unmodelled.append(Unmodelled(bare, start, file))
            # Skip the body wholesale. Its lines are a value, not makefile
            # syntax, and lexing them produces nonsense findings.
            i += 1
            while i < len(lines) and not _ENDEF.match(lines[i]):
                i += 1
            i += 1
            in_recipe_context = False
            continue

        if _CONDITIONAL_OPEN.match(bare):
            conditional_depth += 1
            in_recipe_context = False
            i += 1
            continue
        if _CONDITIONAL_CLOSE.match(bare):
            conditional_depth = max(0, conditional_depth - 1)
            in_recipe_context = False
            i += 1
            continue
        if _CONDITIONAL_ELSE.match(bare):
            in_recipe_context = False
            i += 1
            continue

        record = _classify(logical, start, file, conditional_depth > 0, False)
        if isinstance(record, Assignment):
            out.assignments.append(record)
            in_recipe_context = False
        elif isinstance(record, Rule):
            out.rules.append(record)
            last_rule_line = record.line
            in_recipe_context = True
            if ".PHONY" in record.targets:
                colon = _first_colon_outside_parens(_strip_comment(logical))
                names = _strip_comment(logical)[colon:].lstrip(": \t").split()
                for phony_name in names:
                    out.phony.append((phony_name, record.line))
            # A one-line rule written with `;` carries its recipe inline.
            semi = _strip_comment(logical)
            colon = _first_colon_outside_parens(semi)
            if colon >= 0 and ";" in semi[colon:] and semi[colon:].split(";", 1)[1].strip():
                flush_recipe_flag(record.line, record.line)
        elif isinstance(record, Include):
            out.includes.append(record)
            in_recipe_context = False
        elif isinstance(record, Unmodelled):
            out.unmodelled.append(record)
            in_recipe_context = False
        i += 1

    return out
