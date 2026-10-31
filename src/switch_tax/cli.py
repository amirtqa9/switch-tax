"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import TraceError, analyze, validate
from .importer import import_trace


def _jsonl(path: str) -> list[dict]:
    rows = []
    with Path(path).open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise TraceError(f"{path}:{number}: {exc.msg}") from exc
    return rows


def _markdown(report: dict) -> str:
    lines = ["# SwitchTax report", "", "| Switch | Status | First progress | Elapsed ms | Active ms | Tool calls |",
             "| --- | --- | --- | ---: | ---: | ---: |"]
    for switch in report["switches"]:
        metric = switch["metrics"]
        values = [switch["switch_id"], switch["status"], switch["first_progress_event_id"],
                  metric["observed_wall_ms"], metric["active_event_ms"], metric["tool_calls"]]
        lines.append("| " + " | ".join(str(v if v is not None else "unknown").replace("|", "\\|") for v in values) + " |")
    if report["warnings"]:
        lines += ["", "## Warnings", ""] + [f"- {item}" for item in report["warnings"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="switch-tax")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate")
    check.add_argument("events")
    importer = sub.add_parser("import", help="redact a local task-event JSONL export")
    importer.add_argument("source")
    importer.add_argument("--out", required=True)
    run = sub.add_parser("analyze")
    run.add_argument("events")
    run.add_argument("--labels")
    run.add_argument("--format", choices=("json", "md"), default="json")
    run.add_argument("--out")
    explain = sub.add_parser("explain")
    explain.add_argument("report")
    explain.add_argument("switch_id")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            print(f"valid: {len(validate(_jsonl(args.events)))} events")
            return 0
        if args.command == "import":
            source, destination = Path(args.source), Path(args.out)
            if source.resolve() == destination.resolve():
                raise TraceError("source and output paths must differ")
            with source.open(encoding="utf-8") as stream:
                def rows():
                    for number, line in enumerate(stream, 1):
                        if line.strip():
                            try:
                                yield json.loads(line)
                            except json.JSONDecodeError as exc:
                                raise TraceError(f"{source}:{number}: {exc.msg}") from exc
                events = import_trace(rows())
            destination.write_text("".join(json.dumps(event, sort_keys=True) + "\n" for event in events), encoding="utf-8")
            print(f"imported: {len(events)} redacted events")
            return 0
        if args.command == "explain":
            report = json.loads(Path(args.report).read_text(encoding="utf-8"))
            switch = next((s for s in report["switches"] if s["switch_id"] == args.switch_id), None)
            if switch is None:
                print(f"switch-tax: switch not found: {args.switch_id}", file=sys.stderr)
                return 3
            print(json.dumps(switch, indent=2, sort_keys=True))
            return 0 if switch["status"] == "complete" else 3
        labels = json.loads(Path(args.labels).read_text(encoding="utf-8")) if args.labels else None
        report = analyze(_jsonl(args.events), labels)
        output = _markdown(report) if args.format == "md" else json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.out:
            Path(args.out).write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"switch-tax: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
