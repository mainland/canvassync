"""Publishing and unpublishing course files."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from .conftest import MakeSync, obj


def canvas_file(**attrs: object) -> MagicMock:
    """A mock Canvas file with the given state."""
    mock = MagicMock(spec=[*attrs, "update"])
    for key, value in attrs.items():
        setattr(mock, key, value)
    return mock


@pytest.mark.parametrize(
    ("published", "state", "expected"),
    [
        # Unpublishing locks the file; "hidden" alone is still open by link.
        (False, {"locked": False, "hidden": False}, {"locked": True}),
        (False, {"locked": False, "hidden": True}, {"locked": True}),
        (True, {"locked": True, "hidden": False}, {"locked": False}),
        (True, {"locked": False, "hidden": True}, {"locked": False}),
    ],
)
def test_set_file_published_changes_state(
    make_sync: MakeSync,
    published: bool,
    state: dict[str, bool],
    expected: dict[str, bool],
) -> None:
    file = canvas_file(filename="solutions.pdf", **state)

    make_sync().set_file_published(file, published)

    file.update.assert_called_once_with(hidden=False, **expected)


@pytest.mark.parametrize("published", [True, False])
def test_set_file_published_leaves_matching_state(
    make_sync: MakeSync, published: bool
) -> None:
    file = canvas_file(filename="a.pdf", locked=not published, hidden=False)

    make_sync().set_file_published(file, published)

    file.update.assert_not_called()


def test_new_unpublished_file_item_is_locked(
    make_sync: MakeSync, course: MagicMock, tmp_path: Path
) -> None:
    (tmp_path / "solutions.pdf").write_bytes(b"%PDF")
    file = canvas_file(
        id=5, filename="solutions.pdf", locked=False, hidden=False
    )
    sync = make_sync(course=course)
    sync.upload_file = MagicMock(return_value=(True, file))  # type: ignore[method-assign]
    module = MagicMock()
    module.create_module_item.return_value = obj(id=1)

    sync.create_module_item(
        module,
        {
            "title": "Worked Solutions",
            "file": "solutions.pdf",
            "published": False,
            "indent": 0,
        },
        0,
    )

    file.update.assert_called_once_with(locked=True, hidden=False)


def test_existing_file_item_is_unpublished_on_sync(
    make_sync: MakeSync, course: MagicMock, tmp_path: Path
) -> None:
    (tmp_path / "solutions.pdf").write_bytes(b"%PDF")
    file = canvas_file(
        id=5, filename="solutions.pdf", locked=False, hidden=False
    )
    sync = make_sync(course=course)
    sync.upload_file = MagicMock(return_value=(False, file))  # type: ignore[method-assign]
    item = obj(title="Worked Solutions", type="File", content_id=5)

    assert sync.sync_module_item(
        MagicMock(),
        {
            "title": "Worked Solutions",
            "file": "solutions.pdf",
            "published": False,
            "indent": 0,
        },
        0,
        item,
    )

    file.update.assert_called_once_with(locked=True, hidden=False)
