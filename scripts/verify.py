"""Run the gateway's offline acceptance suite and save reviewable reports."""

import argparse
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ElementTree
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FEATURES = {
    "health": ("Health and OpenAPI", ("test_health.py",)),
    "config": ("Environment and secrets configuration", ("test_config.py",)),
    "models": ("Model catalog, routing and capabilities", ("test_models.py",)),
    "prompts": ("Prompt templates, versions and parameters", ("test_prompts.py",)),
    "chat": ("Unified chat API and validation", ("test_chat.py",)),
    "adapters": (
        "DeepSeek, compatible and Anthropic adapters",
        ("test_deepseek_adapter.py", "test_anthropic_adapter.py"),
    ),
    "errors": ("Error mapping and sanitization", ("test_errors.py",)),
    "structured": ("Structured output and repair attempts", ("test_structured_output.py",)),
    "streaming": ("SSE events, terminal states and cancellation", ("test_streaming.py",)),
    "usage": ("Token normalization and aggregation", ("test_usage.py",)),
    "governance": ("Call records, privacy, pricing and local queries", ("test_governance.py",)),
    "verification": ("Verification reports and failure detection", ("test_verification.py",)),
}


def summarize_junit(path: Path, selected: list[str]) -> dict:
    owners = {
        Path(filename).stem: group
        for group, (_, filenames) in FEATURES.items()
        for filename in filenames
    }
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    groups = {group: dict(counts) for group in selected}
    for case in ElementTree.parse(path).getroot().iter("testcase"):
        module = case.get("classname", "").removeprefix("tests.").split(".")[0]
        group = owners.get(module, "other")
        outcome = "passed"
        for tag, name in (("error", "errors"), ("failure", "failed"), ("skipped", "skipped")):
            if case.find(tag) is not None:
                outcome = name
                break
        counts[outcome] += 1
        groups.setdefault(group, dict.fromkeys(counts, 0))[outcome] += 1
    empty_groups = [group for group, results in groups.items() if not sum(results.values())]
    complete = bool(counts["passed"]) and not (
        counts["failed"] or counts["errors"] or counts["skipped"] or empty_groups
    )
    return {
        "total": sum(counts.values()),
        **counts,
        "groups": groups,
        "empty_groups": empty_groups,
        "complete": complete,
    }


def run_step(name: str, arguments: list[str], report_dir: Path) -> dict:
    command = [sys.executable, "-m", *arguments]
    log_path = report_dir / f"{name}.log"
    print(f"\n[{name}] {subprocess.list2cmdline(command)}", flush=True)
    started = time.perf_counter()
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    with log_path.open("w", encoding="utf-8") as log_file:
        try:
            with subprocess.Popen(
                command,
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            ) as process:
                for line in process.stdout:
                    print(line, end="", flush=True)
                    log_file.write(line)
                returncode = process.wait()
        except OSError as error:
            message = f"Could not start {name}: {error}\n"
            print(message, end="", flush=True)
            log_file.write(message)
            returncode = 1
    return {
        "name": name,
        "command": command,
        "returncode": returncode,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "log": str(log_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="List feature groups without running")
    parser.add_argument("--group", nargs="+", choices=FEATURES, help="Run selected feature groups")
    parser.add_argument(
        "--tests-only", action="store_true", help="Skip Ruff lint and format checks"
    )
    parser.add_argument(
        "--report-dir", type=Path, help="Report directory, relative to repository root"
    )
    arguments = parser.parse_args(argv)
    if arguments.list:
        for group, (description, filenames) in FEATURES.items():
            print(f"{group:14} {description}\n               {', '.join(filenames)}")
        return 0

    selected = list(dict.fromkeys(arguments.group or FEATURES))
    paths = (
        list(
            dict.fromkeys(
                f"tests/{filename}" for group in selected for filename in FEATURES[group][1]
            )
        )
        if arguments.group
        else ["tests"]
    )
    started_at = datetime.now(UTC)
    report_dir = arguments.report_dir or Path(
        "reports", "verification", started_at.strftime("%Y%m%dT%H%M%S.%fZ")
    )
    report_dir = (ROOT / report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    junit_path = report_dir / "junit.xml"
    junit_path.unlink(missing_ok=True)
    print(f"Features: {', '.join(selected)}\nReports: {report_dir}", flush=True)
    steps = [
        run_step(
            "tests",
            [
                "pytest",
                *paths,
                "-v",
                "--strict-config",
                "--strict-markers",
                "--durations=10",
                f"--junitxml={junit_path}",
            ],
            report_dir,
        )
    ]
    try:
        results = summarize_junit(junit_path, selected)
    except (OSError, ElementTree.ParseError) as error:
        results = {"complete": False, "report_error": str(error)}
    if not arguments.tests_only:
        steps.append(run_step("lint", ["ruff", "check", "."], report_dir))
        steps.append(run_step("format", ["ruff", "format", "--check", "."], report_dir))
    passed = results["complete"] and all(step["returncode"] == 0 for step in steps)
    summary = {
        "status": "passed" if passed else "failed",
        "scope": "selected" if arguments.group else "all",
        "selected_groups": selected,
        "started_at": started_at.isoformat(),
        "ended_at": datetime.now(UTC).isoformat(),
        "tests": results,
        "steps": steps,
    }
    summary_path = report_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    for group, counts in results.get("groups", {}).items():
        print(f"{group:14} " + ", ".join(f"{name}={count}" for name, count in counts.items()))
    print(f"\nVerification {summary['status'].upper()}. Summary: {summary_path}", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
