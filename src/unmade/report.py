"""Output. One block per line of makefile, in the order you would fix them."""

from __future__ import annotations

import json
import textwrap

from .analyse import Result

#: Width that keeps a finding readable in a split terminal.
WRAP = 78


def _wrap(text: str, indent: str) -> str:
    return textwrap.fill(text, width=WRAP, initial_indent=indent, subsequent_indent=indent)


def error_lines(result: Result) -> list[str]:
    """Diagnostics, for stderr. Never part of any mode's stdout."""
    return [f"unmade: {error}" for error in result.errors]


def text(result: Result, quiet: bool = False) -> str:
    findings = result.sorted_findings()

    if quiet:
        return "\n".join(
            f"{finding.file}:{finding.line}: {finding.kind}: {finding.detail}"
            for finding in findings
        )

    lines: list[str] = []
    read = ", ".join(result.makefile_list) if result.makefile_list else "nothing"
    lines.append(f"{result.makefile}: make read {read}.")
    lines.append("")

    for finding in findings:
        lines.append(f"{finding.file}:{finding.line}: {finding.kind}")
        lines.append(_wrap(finding.detail, "    "))
        if finding.instead:
            lines.append(f"    make used: {finding.instead}")
        lines.append("")

    if findings:
        plural = "s" if len(findings) != 1 else ""
        lines.append(f"{len(findings)} line{plural} with no effect on this build.")
    else:
        lines.append("Every line unmade can check has an effect on this build.")

    if result.notes:
        lines.append("")
        for note in result.notes:
            lines.append(f"{note.file}:{note.line}: {note.kind}: {note.detail}")

    if result.disagreements:
        lines.append("")
        lines.append(
            _wrap(
                "unmade said nothing about these variables: its model of which "
                "line wins disagreed with make's database, so any claim would "
                "have been a guess.",
                "",
            )
        )
        for disagreement in result.disagreements:
            lines.append(
                f"    {disagreement.name}: expected {disagreement.expected}, "
                f"make says {disagreement.actual}"
            )

    return "\n".join(lines)


def as_json(result: Result) -> str:
    return json.dumps(
        {
            "makefile": result.makefile,
            "make_version": result.make_version,
            "makefiles_read": list(result.makefile_list),
            "findings": [
                {
                    "kind": finding.kind,
                    "file": finding.file,
                    "line": finding.line,
                    "detail": finding.detail,
                    "instead": finding.instead,
                }
                for finding in result.sorted_findings()
            ],
            "notes": [
                {
                    "kind": note.kind,
                    "file": note.file,
                    "line": note.line,
                    "detail": note.detail,
                }
                for note in result.notes
            ],
            "disagreements": [
                {
                    "variable": d.name,
                    "expected": d.expected,
                    "make_says": d.actual,
                }
                for d in result.disagreements
            ],
            "errors": result.errors,
        },
        indent=2,
    )
