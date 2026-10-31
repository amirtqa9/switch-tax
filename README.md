# SwitchTax

SwitchTax measures observable handoff, reorientation, setup, validation, and wait activity when a task moves between sessions. Reports show each switch boundary, the first verified productive event, elapsed and active time, usage, and gaps in the trace.

## Install

Requires Python 3.11 or newer. From this repository:

```sh
python3 -m pip install -e .
```

## Use

```sh
switch-tax import raw-task-events.jsonl --out events.jsonl
switch-tax validate events.jsonl
switch-tax analyze events.jsonl --format json --out report.json
switch-tax explain report.json SWITCH_ID
```

The importer accepts JSONL rows with `type`, `id`, `task`, `session`, and increasing `seq` values, plus event-specific switch and timing fields. It hashes identifiers and command fingerprints, preserves valid references, and omits raw prompts and tool results. The analyzer reports incomplete switches and unknown measurements explicitly. It does not treat observed transition time as proof of causation.

Run the tests with `PYTHONPATH=src python3 -m unittest discover -s tests -q`.
