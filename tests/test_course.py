"""Course selection and command line options."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from canvassync import command_line

from .conftest import MakeSync


def test_course_by_config_course_id(make_sync: MakeSync) -> None:
    sync = make_sync({"course_id": 4242})
    course = sync.course

    sync.canvas.get_course.assert_called_once_with(
        4242, **{"include[]": "syllabus_body"}
    )
    assert course is sync.canvas.get_course.return_value


def test_course_id_option_overrides_config(make_sync: MakeSync) -> None:
    sync = make_sync({"course_id": 4242}, course_id=12345)
    _ = sync.course

    sync.canvas.get_course.assert_called_once_with(
        12345, **{"include[]": "syllabus_body"}
    )


def test_course_id_option_overrides_sis(make_sync: MakeSync) -> None:
    sync = make_sync(course_id=12345)
    _ = sync.course

    args, kwargs = sync.canvas.get_course.call_args
    assert args == (12345,)
    assert "use_sis_id" not in kwargs


def test_course_by_sis(make_sync: MakeSync) -> None:
    sync = make_sync()
    _ = sync.course

    sync.canvas.get_course.assert_called_once_with(
        "SIS 101", use_sis_id=True, **{"include[]": "syllabus_body"}
    )


def test_no_course_configured(make_sync: MakeSync) -> None:
    sync = make_sync({"course_sis": None})

    with pytest.raises(ValueError, match="No course specified"):
        _ = sync.course


def test_cli_passes_course_id_and_sync_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"api_url": "x", "modules": []}))

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "canvas-sync",
            "-q",
            "--course-id",
            "12345",
            "--config",
            str(config),
            "sync",
            "--create-assignments",
            "--replace-quizzes",
            "--limit",
            "Week 1",
        ],
    )

    with patch.object(command_line, "CanvasSync") as canvas_sync:
        assert command_line.sync() == 0

    canvas_sync.assert_called_once_with(
        config, root=None, api_key=None, course_id=12345
    )
    canvas_sync.return_value.sync.assert_called_once_with(
        limits=["Week 1"], create_assignments=True, replace_quizzes=True
    )


def test_cli_flags_default_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"api_url": "x", "modules": []}))

    monkeypatch.setattr(
        sys, "argv", ["canvas-sync", "-q", "--config", str(config), "sync"]
    )

    with patch.object(command_line, "CanvasSync") as canvas_sync:
        canvas_sync.return_value = MagicMock()
        assert command_line.sync() == 0

    assert canvas_sync.call_args.kwargs["course_id"] is None
    canvas_sync.return_value.sync.assert_called_once_with(
        limits=None, create_assignments=False, replace_quizzes=False
    )
