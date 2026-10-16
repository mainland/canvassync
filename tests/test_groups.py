"""Group sets (Canvas group categories) and group assignments."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from .conftest import MakeSync, obj


def category(**attrs: Any) -> MagicMock:
    """A mock group category with the methods sync_group_sets uses."""
    mock = MagicMock(
        spec=list(attrs) + ["update", "get_groups", "create_group"]
    )
    for key, value in attrs.items():
        setattr(mock, key, value)
    mock.get_groups.return_value = []
    return mock


PAIRS = {
    "name": "Project Pairs",
    "self_signup": True,
    "group_limit": 2,
    "groups": 3,
}


def test_create_group_set_with_empty_groups(
    make_sync: MakeSync, course: MagicMock
) -> None:
    course.get_group_categories.return_value = []
    created = category(id=5, name="Project Pairs")
    course.create_group_category.return_value = created

    make_sync(config={"group_sets": [PAIRS]}, course=course).sync_group_sets()

    course.create_group_category.assert_called_once_with(
        "Project Pairs", self_signup="enabled", group_limit=2
    )
    assert [c.kwargs["name"] for c in created.create_group.call_args_list] == [
        "Project Pairs 1",
        "Project Pairs 2",
        "Project Pairs 3",
    ]


def test_existing_group_set_is_left_alone_when_unchanged(
    make_sync: MakeSync, course: MagicMock
) -> None:
    existing = category(
        id=5, name="Project Pairs", self_signup="enabled", group_limit=2
    )
    existing.get_groups.return_value = [
        obj(name="Project Pairs 1"),
        obj(name="Team Rocket"),  # renamed or student-made groups count
        obj(name="Project Pairs 3"),
    ]
    course.get_group_categories.return_value = [existing]

    make_sync(config={"group_sets": [PAIRS]}, course=course).sync_group_sets()

    course.create_group_category.assert_not_called()
    existing.update.assert_not_called()
    existing.create_group.assert_not_called()


def test_group_set_settings_updated_and_groups_topped_up(
    make_sync: MakeSync, course: MagicMock
) -> None:
    existing = category(
        id=5, name="Project Pairs", self_signup=None, group_limit=3
    )
    existing.get_groups.return_value = [obj(name="Project Pairs 1")]
    course.get_group_categories.return_value = [existing]

    make_sync(config={"group_sets": [PAIRS]}, course=course).sync_group_sets()

    existing.update.assert_called_once_with(
        self_signup="enabled", group_limit=2
    )
    # "Project Pairs 1" exists, so the next free names are used
    created = existing.create_group.call_args_list
    assert [c.kwargs["name"] for c in created] == [
        "Project Pairs 2",
        "Project Pairs 3",
    ]


def test_group_assignment_settings(
    make_sync: MakeSync, course: MagicMock
) -> None:
    course.get_group_categories.return_value = [
        category(id=5, name="Project Pairs")
    ]
    sync = make_sync(course=course)

    item = {"title": "Project Proposal", "group_set": "Project Pairs"}
    assert sync.assignment_settings(item) == {
        "group_category_id": 5,
        "grade_group_students_individually": False,
    }

    current = obj(group_category_id=5, grade_group_students_individually=False)
    assert sync.assignment_settings(item, current) == {}

    item["grade_group_students_individually"] = True
    assert sync.assignment_settings(item, current) == {
        "grade_group_students_individually": True
    }


def test_unknown_group_set_exits(
    make_sync: MakeSync, course: MagicMock
) -> None:
    course.get_group_categories.return_value = []
    sync = make_sync(course=course)

    with pytest.raises(SystemExit):
        sync.assignment_settings({"title": "X", "group_set": "Missing"})
