"""Classic Quiz import from QTI and module items for quizzes."""

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from canvassync.sync import (
    get_sha256_marker,
    item_type,
    set_sha256_marker,
)

from .conftest import MakeSync, obj

ZIP_BYTES = b"PK\x03\x04 fake qti zip"
ZIP_SHA = hashlib.sha256(ZIP_BYTES).hexdigest()


@pytest.fixture
def qti_zip(tmp_path: Path) -> Path:
    path = tmp_path / "quizzes" / "quiz01.zip"
    path.parent.mkdir()
    path.write_bytes(ZIP_BYTES)
    return path


def quiz_item(**extra: Any) -> dict[str, Any]:
    item = {
        "title": "Quiz 1: 5G NR",
        "quiz": "quizzes/quiz01.zip",
        "published": True,
        "time_limit": 30,
        "allowed_attempts": 2,
        "shuffle_answers": True,
        "assignment_group": "Quizzes",
        "due_at": "1/10/2027 11:59pm",
        "indent": 1,
    }
    item.update(extra)
    return item


def migration(state: str) -> MagicMock:
    return obj(id=77, workflow_state=state)


def response(status: int, headers: dict[str, str] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.ok = status < 400
    resp.headers = headers or {}
    resp.text = ""
    return resp


def setup_import(course: MagicMock, existing: list[MagicMock]) -> MagicMock:
    """Mock a course whose QTI import creates quiz 501."""
    imported = obj(
        id=501,
        title="Quiz 1",
        description="<p>From QTI</p>",
        published=False,
    )
    imported.edit.return_value = obj(id=501, title="Quiz 1: 5G NR")

    other = obj(id=400, title="Other quiz")

    def get_quizzes(**kwargs: Any) -> list[MagicMock]:
        if "search_term" in kwargs:
            return existing
        # Before the import, then after it
        get_quizzes.calls += 1  # type: ignore[attr-defined]
        if get_quizzes.calls == 1:  # type: ignore[attr-defined]
            return [other, *existing]
        return [other, *existing, imported]

    get_quizzes.calls = 0  # type: ignore[attr-defined]
    course.get_quizzes.side_effect = get_quizzes

    course.get_assignment_groups.return_value = [obj(name="Quizzes", id=8)]
    course.create_content_migration.return_value = obj(
        id=77,
        workflow_state="pre_processing",
        pre_attachment={
            "upload_url": "https://upload.example.edu/files",
            "upload_params": {"key": "abc", "token": "t"},
        },
    )
    course.get_content_migration.side_effect = [
        migration("queued"),
        migration("running"),
        migration("completed"),
    ]
    course.get_quiz.return_value = imported
    return imported


def test_item_type_quiz() -> None:
    assert item_type({"title": "Q", "quiz": "q.zip"}) == "Quiz"
    assert item_type({"title": "A", "assignment": "A"}) == "Assignment"


def test_sha256_marker_roundtrip() -> None:
    html = set_sha256_marker("<p>Intro</p>", ZIP_SHA)
    assert html.startswith("<p>Intro</p>")
    assert html.endswith(f'<p data-canvassync-sha256="{ZIP_SHA}"></p>')
    assert get_sha256_marker(html) == ZIP_SHA

    # Replacing a marker keeps only one
    other = "0" * 64
    html2 = set_sha256_marker(html, other)
    assert html2.count("data-canvassync-sha256") == 1
    assert get_sha256_marker(html2) == other

    assert get_sha256_marker("<p>Intro</p>") is None
    assert get_sha256_marker(None) is None


def test_quiz_import_flow(
    make_sync: MakeSync, course: MagicMock, qti_zip: Path
) -> None:
    imported = setup_import(course, existing=[])
    sync = make_sync(course=course)
    statuses: list[tuple[str, str | None]] = []
    sync._status_callback = lambda m, i: statuses.append((m, i))

    with (
        patch("canvassync.sync.requests.post") as post,
        patch("canvassync.sync.requests.get") as get,
        patch("canvassync.sync.time.sleep") as sleep,
    ):
        post.return_value = response(
            302, {"Location": "https://canvas.example.edu/confirm/1"}
        )
        get.return_value = response(200)

        quiz = sync.sync_quiz_item(quiz_item())

    # Migration created with the file's name and size
    course.create_content_migration.assert_called_once_with(
        migration_type="qti_converter",
        pre_attachment={"name": "quiz01.zip", "size": len(ZIP_BYTES)},
    )

    # Multipart upload without following redirects, then confirmation
    post_args, post_kwargs = post.call_args
    assert post_args == ("https://upload.example.edu/files",)
    assert post_kwargs["data"] == {"key": "abc", "token": "t"}
    assert post_kwargs["files"]["file"][0] == "quiz01.zip"
    assert post_kwargs["allow_redirects"] is False
    get.assert_called_once()
    assert get.call_args.args == ("https://canvas.example.edu/confirm/1",)
    assert get.call_args.kwargs["headers"] == {
        "Authorization": "Bearer secret-token"
    }

    # Polled until completed
    assert course.get_content_migration.call_count == 3
    assert sleep.call_count == 2
    assert any("importing QTI" in (i or "") for _, i in statuses)

    # The new quiz (not the pre-existing one) is edited
    course.get_quiz.assert_called_once_with(501)
    attrs = imported.edit.call_args.kwargs["quiz"]
    assert attrs["title"] == "Quiz 1: 5G NR"
    assert attrs["time_limit"] == 30
    assert attrs["allowed_attempts"] == 2
    assert attrs["shuffle_answers"] is True
    assert attrs["assignment_group_id"] == 8
    assert attrs["published"] is True
    assert attrs["due_at"].startswith("2027-01-10T23:59:00")
    assert attrs["description"] == (
        f'<p>From QTI</p>\n<p data-canvassync-sha256="{ZIP_SHA}"></p>'
    )
    assert quiz is imported.edit.return_value


def test_quiz_upload_created_response(
    make_sync: MakeSync, course: MagicMock, qti_zip: Path
) -> None:
    setup_import(course, existing=[])
    sync = make_sync(course=course)

    with (
        patch("canvassync.sync.requests.post") as post,
        patch("canvassync.sync.requests.get") as get,
        patch("canvassync.sync.time.sleep"),
    ):
        post.return_value = response(201)
        sync.sync_quiz_item(quiz_item())

    get.assert_not_called()


def test_quiz_import_failure(
    make_sync: MakeSync, course: MagicMock, qti_zip: Path
) -> None:
    setup_import(course, existing=[])
    course.get_content_migration.side_effect = [migration("failed")]
    sync = make_sync(course=course)

    with (
        patch("canvassync.sync.requests.post") as post,
        patch("canvassync.sync.time.sleep"),
        pytest.raises(ValueError, match="failed"),
    ):
        post.return_value = response(201)
        sync.sync_quiz_item(quiz_item())


def test_quiz_import_timeout(
    make_sync: MakeSync, course: MagicMock, qti_zip: Path
) -> None:
    setup_import(course, existing=[])
    course.get_content_migration.side_effect = None
    course.get_content_migration.return_value = migration("running")
    sync = make_sync(course=course)
    sync.migration_timeout = 0

    with (
        patch("canvassync.sync.requests.post") as post,
        patch("canvassync.sync.time.sleep"),
        patch("canvassync.sync.time.monotonic", side_effect=[0, 5]),
        pytest.raises(TimeoutError),
    ):
        post.return_value = response(201)
        sync.sync_quiz_item(quiz_item())


def test_existing_quiz_updates_only_differences(
    make_sync: MakeSync, course: MagicMock, qti_zip: Path
) -> None:
    existing = obj(
        id=600,
        title="Quiz 1: 5G NR",
        time_limit=30,
        allowed_attempts=1,
        shuffle_answers=True,
        assignment_group_id=8,
        due_at="2027-01-11T04:59:00Z",
        published=True,
    )
    course.get_quizzes.return_value = [
        obj(id=1, title="Quiz 1: 5G NR (practice)"),
        existing,
    ]
    course.get_assignment_groups.return_value = [obj(name="Quizzes", id=8)]
    sync = make_sync(course=course)

    with patch("canvassync.sync.parse_datetime") as parse:
        import dateutil.parser

        parse.return_value = dateutil.parser.isoparse("2027-01-11T04:59:00Z")
        sync.sync_quiz_item(quiz_item())

    course.create_content_migration.assert_not_called()
    existing.edit.assert_called_once_with(quiz={"allowed_attempts": 2})


def test_replace_quiz_guard_with_submissions(
    make_sync: MakeSync,
    course: MagicMock,
    qti_zip: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    existing = obj(
        id=600,
        title="Quiz 1: 5G NR",
        description=set_sha256_marker("<p>old</p>", "f" * 64),
        published=True,
    )
    existing.get_submissions.return_value = [obj(id=1)]
    course.get_quizzes.return_value = [existing]
    sync = make_sync(course=course)
    sync.replace_quizzes = True

    quiz = sync.sync_quiz_item(
        {
            "title": "Quiz 1: 5G NR",
            "quiz": "quizzes/quiz01.zip",
            "published": True,
        }
    )

    assert "student submissions" in capsys.readouterr().err
    existing.delete.assert_not_called()
    course.create_content_migration.assert_not_called()
    # The existing quiz is kept, and its sha256 marker is not rewritten
    assert quiz is existing
    existing.edit.assert_not_called()


def test_replace_quiz_without_submissions(
    make_sync: MakeSync, course: MagicMock, qti_zip: Path
) -> None:
    existing = obj(
        id=600,
        title="Quiz 1: 5G NR",
        description=set_sha256_marker("<p>old</p>", "f" * 64),
    )
    existing.get_submissions.return_value = []
    imported = setup_import(course, existing=[existing])
    sync = make_sync(course=course)
    sync.replace_quizzes = True

    with (
        patch("canvassync.sync.requests.post") as post,
        patch("canvassync.sync.time.sleep"),
    ):
        post.return_value = response(201)
        quiz = sync.sync_quiz_item(quiz_item())

    existing.delete.assert_called_once()
    course.create_content_migration.assert_called_once()
    assert quiz is imported.edit.return_value


def test_replace_quiz_skipped_when_unchanged(
    make_sync: MakeSync, course: MagicMock, qti_zip: Path
) -> None:
    existing = obj(
        id=600,
        title="Quiz 1: 5G NR",
        description=set_sha256_marker("<p>old</p>", ZIP_SHA),
        published=True,
    )
    course.get_quizzes.return_value = [existing]
    sync = make_sync(course=course)
    sync.replace_quizzes = True

    sync.sync_quiz_item(
        {
            "title": "Quiz 1: 5G NR",
            "quiz": "quizzes/quiz01.zip",
            "published": True,
        }
    )

    existing.get_submissions.assert_not_called()
    existing.delete.assert_not_called()
    existing.edit.assert_not_called()


def test_replace_quiz_without_marker_replaces(
    make_sync: MakeSync, course: MagicMock, qti_zip: Path
) -> None:
    existing = obj(id=600, title="Quiz 1: 5G NR", description="<p>x</p>")
    existing.get_submissions.return_value = []
    setup_import(course, existing=[existing])
    sync = make_sync(course=course)
    sync.replace_quizzes = True

    with (
        patch("canvassync.sync.requests.post") as post,
        patch("canvassync.sync.time.sleep"),
    ):
        post.return_value = response(201)
        sync.sync_quiz_item(quiz_item())

    existing.delete.assert_called_once()


def test_no_replace_without_flag(
    make_sync: MakeSync, course: MagicMock, qti_zip: Path
) -> None:
    existing = obj(
        id=600, title="Quiz 1: 5G NR", description="<p>x</p>", published=True
    )
    course.get_quizzes.return_value = [existing]
    sync = make_sync(course=course)

    sync.sync_quiz_item(
        {
            "title": "Quiz 1: 5G NR",
            "quiz": "quizzes/quiz01.zip",
            "published": True,
        }
    )

    existing.get_submissions.assert_not_called()
    existing.delete.assert_not_called()


def test_create_quiz_module_item(
    make_sync: MakeSync, course: MagicMock
) -> None:
    sync = make_sync(course=course)
    sync.sync_quiz_item = MagicMock(  # type: ignore[method-assign]
        return_value=obj(id=501)
    )
    module = MagicMock()

    sync.create_module_item(module, quiz_item(), 3)

    module.create_module_item.assert_called_once_with(
        module_item={
            "title": "Quiz 1: 5G NR",
            "type": "Quiz",
            "position": 4,
            "indent": 1,
            "content_id": 501,
        }
    )
    module.create_module_item.return_value.edit.assert_called_once_with(
        module_item={"published": True}
    )


def test_sync_quiz_module_item(make_sync: MakeSync, course: MagicMock) -> None:
    sync = make_sync(course=course)
    sync.sync_quiz_item = MagicMock(  # type: ignore[method-assign]
        return_value=obj(id=501)
    )
    module = MagicMock()

    same = obj(title="Quiz 1: 5G NR", type="Quiz", content_id=501)
    assert sync.sync_module_item(module, quiz_item(), 0, same)
    same.edit.assert_called_once_with(
        module_item={
            "title": "Quiz 1: 5G NR",
            "indent": 1,
            "position": 1,
            "published": True,
        }
    )

    replaced = obj(title="Quiz 1: 5G NR", type="Quiz", content_id=400)
    assert not sync.sync_module_item(module, quiz_item(), 0, replaced)
    replaced.edit.assert_not_called()


def test_find_quiz_module_item(make_sync: MakeSync, course: MagicMock) -> None:
    sync = make_sync(course=course)
    items = [
        obj(title="Quiz 1: 5G NR", type="Assignment"),
        obj(title="Quiz 1: 5G NR", type="Quiz"),
    ]

    assert sync.find_module_item(quiz_item(), 0, items) is items[1]


def test_replaced_quiz_recreates_module_item(
    make_sync: MakeSync, course: MagicMock, qti_zip: Path
) -> None:
    """End to end through sync_module: the item is deleted and re-created."""
    from canvasapi.exceptions import ResourceDoesNotExist

    existing = obj(
        id=600,
        title="Quiz 1: 5G NR",
        description=set_sha256_marker("", "f" * 64),
    )
    existing.get_submissions.return_value = []
    imported = setup_import(course, existing=[existing])
    imported.edit.return_value = obj(id=501, title="Quiz 1: 5G NR")

    old_item = obj(title="Quiz 1: 5G NR", type="Quiz", content_id=600)
    # Canvas removes module items of deleted quizzes
    old_item.delete.side_effect = ResourceDoesNotExist("gone")

    course_module = MagicMock()
    course_module.name = "Week 1"
    course_module.get_module_items.return_value = [old_item]

    sync = make_sync(course=course)
    sync.replace_quizzes = True

    module_config = {
        "name": "Week 1",
        "items": [
            {
                "title": "Quiz 1: 5G NR",
                "quiz": "quizzes/quiz01.zip",
                "published": True,
            }
        ],
    }

    with (
        patch("canvassync.sync.requests.post") as post,
        patch("canvassync.sync.time.sleep"),
    ):
        post.return_value = response(201)
        sync.sync_module(module_config, course_module)

    existing.delete.assert_called_once()
    course.create_content_migration.assert_called_once()
    attrs = course_module.create_module_item.call_args.kwargs["module_item"]
    assert attrs["type"] == "Quiz"
    assert attrs["content_id"] == 501
