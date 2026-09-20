"""The `asimoov` command line."""

from __future__ import annotations

import json

import pytest
from tests.core.conftest import AVATAR_ROBOT, REPLAY_DIR

from asimoov.__main__ import main

FAMILY_EVENING = REPLAY_DIR / "family_evening.jsonl"


def test_doctor_reports_the_installation(capsys, asimoov_home):
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "bodies" in out and "fake" in out
    assert "sqlite fts5" in out
    assert "openai key" in out
    assert "not set (ASIMOOV_OPENAI_API_KEY)" in out


def test_doctor_reports_perception_models_and_audio(capsys, asimoov_home):
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "face models" in out
    assert "silero vad" in out
    assert "audio backends" in out
    assert "barge-in         full-duplex" in out


def test_doctor_reports_the_claude_pipeline_components(capsys, asimoov_home, monkeypatch):
    """One line per piece, and the keys reported as set or not, never printed."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ASIMOOV_ANTHROPIC_API_KEY", "sk-ant-secret-value")
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "claude_pipeline" in out
    assert "anthropic sdk" in out
    assert "claude stt" in out and "claude tts" in out
    assert "anthropic key    set" in out
    assert "sk-ant-secret-value" not in out


def test_doctor_reports_whether_espeak_ng_is_on_path(capsys, asimoov_home, monkeypatch):
    """`kokoro-onnx` phonemizes with `espeak-ng`; `doctor` says if it is missing."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert main(["doctor"]) == 0
    assert "espeak-ng        NOT on PATH" in capsys.readouterr().out

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/espeak-ng")
    assert main(["doctor"]) == 0
    assert "espeak-ng        on PATH (/usr/bin/espeak-ng)" in capsys.readouterr().out


def test_doctor_downloads_the_models_on_demand(capsys, asimoov_home, monkeypatch):
    called: list[bool] = []

    def fake_download():
        called.append(True)
        return [asimoov_home / "models" / "det_500m.onnx"]

    monkeypatch.setattr("asimoov.perception.models.download_models", fake_download)
    assert main(["doctor", "--download-models"]) == 0
    assert called == [True]
    assert "model download" in capsys.readouterr().out


def test_enroll_without_a_robot_running_is_reported(capsys):
    assert main(["enroll", "--name", "Sam", "--timeout", "0.2"]) == 2
    assert "no robot answering" in capsys.readouterr().err


def test_doctor_reads_a_robot_directory(capsys, monkeypatch):
    monkeypatch.setattr("asimoov.core.voice_loop._desktop_available", lambda: True)
    assert main(["doctor", str(AVATAR_ROBOT)]) == 0
    out = capsys.readouterr().out
    assert "Aria" in out
    assert "avatar" in out
    assert "audio            in=desktop out=desktop" in out


def test_doctor_refuses_to_call_a_deaf_robot_ready(capsys, monkeypatch):
    """A robot with no microphone and no speaker is not ready to converse."""
    monkeypatch.setattr("asimoov.core.voice_loop._desktop_available", lambda: False)
    monkeypatch.setattr("asimoov.core.voice_loop._android_available", lambda: False)
    assert main(["doctor", str(AVATAR_ROBOT)]) == 1
    assert "audio            NOT READY" in capsys.readouterr().out


def test_doctor_on_a_broken_robot_directory(capsys, tmp_path):
    assert main(["doctor", str(tmp_path)]) == 2
    assert "no such robot configuration file" in capsys.readouterr().err


def test_stats_on_an_empty_metrics_directory(capsys, tmp_path):
    assert main(["stats", "--metrics-dir", str(tmp_path)]) == 0
    assert "no metrics recorded yet" in capsys.readouterr().out


def test_stats_prints_percentiles(capsys, tmp_path):
    (tmp_path / "day.jsonl").write_text(
        "\n".join(
            json.dumps({"metric": "turn", "kind": "span", "ms": value, "tags": {}})
            for value in (100, 200, 300)
        ),
        encoding="utf-8",
    )
    assert main(["stats", "--metrics-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "turn" in out and "p50" in out


def test_replay_runs_the_family_evening_fixture(capsys):
    exit_code = main(
        [
            "replay",
            str(FAMILY_EVENING),
            "--robot",
            str(AVATAR_ROBOT),
            "--speed",
            "30",
            "--max-gap",
            "0.3",
            "--body",
            "fake",
            "--voice",
            "fake",
        ]
    )
    assert exit_code == 0
    assert "13 envelopes, 5 assertions passed" in capsys.readouterr().out


def test_run_drives_a_replay_and_exits(capsys):
    exit_code = main(
        [
            "run",
            str(AVATAR_ROBOT),
            "--voice",
            "fake",
            "--body",
            "fake",
            "--face",
            "none",
            "--no-hub",
            "--replay",
            str(FAMILY_EVENING),
            "--speed",
            "30",
            "--max-gap",
            "0.3",
        ]
    )
    assert exit_code == 0
    assert "assertions passed" in capsys.readouterr().out


def test_an_unknown_body_is_reported(capsys):
    exit_code = main(["run", str(AVATAR_ROBOT), "--body", "unicorn", "--voice", "none", "--face", "none"])
    assert exit_code == 2
    assert "unknown bodies 'unicorn'" in capsys.readouterr().err


def test_a_failing_replay_exits_one(capsys, tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text(
        json.dumps(
            {
                "v": 1,
                "kind": "percept",
                "topic": "percept.battery",
                "id": "x",
                "ts": 1000.0,
                "src": "test",
                "data": {"type": "battery", "level": 0.5},
            }
        )
        + "\n"
        + json.dumps({"assert": "expect", "topic": "mind.injection", "contains": "x", "within_s": 0.1})
        + "\n",
        encoding="utf-8",
    )
    assert main(["replay", str(path), "--robot", str(AVATAR_ROBOT)]) == 1
    assert "replay assertion failed" in capsys.readouterr().err


def test_a_missing_replay_file_exits_two(capsys, tmp_path):
    assert main(["replay", str(tmp_path / "nope.jsonl"), "--robot", str(AVATAR_ROBOT)]) == 2
    assert "asimoov:" in capsys.readouterr().err


def test_no_command_is_a_usage_error():
    with pytest.raises(SystemExit):
        main([])
