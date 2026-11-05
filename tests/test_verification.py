"""Ensure verification reports cannot turn missing or failing coverage green."""

import json
from pathlib import Path

import pytest

from scripts.verify import main, summarize_junit


def write_report(tmp_path: Path, cases: str) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>", encoding="utf-8")
    return path


def test_report_groups_parameterized_cases_and_includes_new_modules(tmp_path: Path) -> None:
    path = write_report(
        tmp_path,
        '<testcase classname="tests.test_streaming" name="example[a]"/>'
        '<testcase classname="tests.test_streaming" name="example[b]"/>'
        '<testcase classname="tests.test_future_feature" name="new_feature"/>',
    )
    result = summarize_junit(path, ["streaming"])
    assert result["complete"] is True
    assert result["total"] == 3
    assert result["groups"]["streaming"]["passed"] == 2
    assert result["groups"]["other"]["passed"] == 1


@pytest.mark.parametrize("tag", ["failure", "error", "skipped"])
def test_unsuccessful_or_skipped_case_prevents_acceptance(tmp_path: Path, tag: str) -> None:
    path = write_report(
        tmp_path,
        '<testcase classname="tests.test_health" name="success"/>'
        f'<testcase classname="tests.test_health" name="problem"><{tag}/></testcase>',
    )
    result = summarize_junit(path, ["health"])
    assert result["total"] == 2
    assert result["passed"] == 1
    assert result["complete"] is False


def test_empty_report_and_missing_feature_prevent_acceptance(tmp_path: Path) -> None:
    empty = summarize_junit(write_report(tmp_path, ""), ["health"])
    assert empty["complete"] is False
    assert empty["empty_groups"] == ["health"]
    path = write_report(tmp_path, '<testcase classname="tests.test_health" name="success"/>')
    partial = summarize_junit(path, ["health", "governance"])
    assert partial["complete"] is False
    assert partial["empty_groups"] == ["governance"]


def test_listing_features_does_not_create_reports(tmp_path: Path, capsys) -> None:
    output = tmp_path / "reports"
    assert main(["--list", "--report-dir", str(output)]) == 0
    assert "streaming" in capsys.readouterr().out
    assert not output.exists()


def test_invalid_group_fails_instead_of_running_an_unintended_suite(tmp_path: Path) -> None:
    output = tmp_path / "reports"
    with pytest.raises(SystemExit) as error:
        main(["--group", "misspelled", "--report-dir", str(output)])
    assert error.value.code == 2
    assert not output.exists()


@pytest.mark.parametrize("failed_step", [None, "tests", "lint", "format"])
def test_runner_propagates_step_failures_and_saves_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_step: str | None
) -> None:
    steps = []

    def run_step(name: str, arguments: list[str], report_dir: Path) -> dict:
        steps.append(name)
        if name == "tests":
            write_report(report_dir, '<testcase classname="tests.test_health" name="success"/>')
        return {"name": name, "returncode": int(name == failed_step)}

    monkeypatch.setattr("scripts.verify.run_step", run_step)
    exit_code = main(["--group", "health", "--report-dir", str(tmp_path)])
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert steps == ["tests", "lint", "format"]
    assert summary["scope"] == "selected"
    assert summary["tests"]["passed"] == 1
    assert exit_code == (0 if failed_step is None else 1)
    assert summary["status"] == ("passed" if failed_step is None else "failed")


def test_missing_new_report_cannot_reuse_previous_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_report(tmp_path, '<testcase classname="tests.test_health" name="old_success"/>')
    monkeypatch.setattr(
        "scripts.verify.run_step", lambda *arguments: {"name": "tests", "returncode": 0}
    )
    exit_code = main(["--group", "health", "--tests-only", "--report-dir", str(tmp_path)])
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert exit_code == 1
    assert summary["status"] == "failed"
    assert summary["tests"]["complete"] is False
    assert "report_error" in summary["tests"]
