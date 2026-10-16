"""Assignment creation, settings and assignment groups."""

from unittest.mock import MagicMock

import pytest
from canvasapi.exceptions import BadRequest

from .conftest import MakeSync, obj


def homework_group() -> MagicMock:
    return obj(name="Homework", id=7, position=1, group_weight=40, rules={})


def test_missing_assignment_exits_without_flag(
    make_sync: MakeSync, course: MagicMock
) -> None:
    sync = make_sync(course=course)

    with pytest.raises(SystemExit):
        sync.sync_assignment_item({"title": "HW1", "assignment": "HW1"})

    course.create_assignment.assert_not_called()


def test_create_assignment_with_settings(
    make_sync: MakeSync, course: MagicMock
) -> None:
    course.get_assignment_groups.return_value = [homework_group()]
    created = obj(
        id=55,
        name="HW1: Cellular",
        description=None,
        due_at=None,
        points_possible=20,
    )
    course.create_assignment.return_value = created

    sync = make_sync(course=course)
    sync.create_assignments = True

    item = {
        "title": "HW1: Cellular",
        "assignment": "HW1: Cellular",
        "published": True,
        "points_possible": 20,
        "submission_types": ["online_upload"],
        "allowed_extensions": ["ipynb"],
        "grading_type": "points",
        "assignment_group": "Homework",
        "peer_reviews": {"anonymous": True, "automatic": True, "count": 2},
        "due_at": "1/10/2027 11:59pm",
    }

    assignment = sync.sync_assignment_item(item)

    assert assignment is created
    course.create_assignment.assert_called_once_with(
        assignment={
            "name": "HW1: Cellular",
            "published": True,
            "points_possible": 20,
            "grading_type": "points",
            "submission_types": ["online_upload"],
            "allowed_extensions": ["ipynb"],
            "assignment_group_id": 7,
            "peer_reviews": True,
            "anonymous_peer_reviews": True,
            "automatic_peer_reviews": True,
            "peer_review_count": 2,
        }
    )
    # The due date is then applied by the regular sync logic
    created.edit.assert_called_once()
    assert "due_at" in created.edit.call_args.kwargs["assignment"]


def test_create_assignment_from_config_flag(
    make_sync: MakeSync, course: MagicMock
) -> None:
    course.create_assignment.return_value = obj(id=1, name="Quiz Essay")
    sync = make_sync({"create_assignments": True}, course=course)

    # Falls back to the title when assignment isn't a name
    sync.sync_assignment_item({"title": "Quiz Essay", "assignment": True})

    attrs = course.create_assignment.call_args.kwargs["assignment"]
    assert attrs == {"name": "Quiz Essay", "published": False}


def test_create_assignment_named_by_assignment_key(
    make_sync: MakeSync, course: MagicMock
) -> None:
    course.create_assignment.return_value = obj(id=1, name="Real Name")
    sync = make_sync(course=course)

    sync.sync(create_assignments=True)
    sync.sync_assignment_item({"title": "Shown", "assignment": "Real Name"})

    attrs = course.create_assignment.call_args.kwargs["assignment"]
    assert attrs["name"] == "Real Name"


def test_existing_assignment_updates_only_differences(
    make_sync: MakeSync, course: MagicMock
) -> None:
    course.get_assignment_groups.return_value = [homework_group()]
    existing = obj(
        id=55,
        name="HW1",
        points_possible=20.0,
        submission_types=["online_upload"],
        allowed_extensions=["pdf", "ipynb"],
        grading_type="points",
        assignment_group_id=3,
        published=False,
        peer_reviews=False,
        anonymous_peer_reviews=False,
        automatic_peer_reviews=False,
    )
    course.get_assignments.return_value = [existing]

    sync = make_sync(course=course)
    sync.sync_assignment_item(
        {
            "title": "HW1",
            "assignment": "HW1",
            "published": True,
            "points_possible": 20,
            "submission_types": ["online_upload"],
            "allowed_extensions": ["ipynb", "pdf"],
            "grading_type": "points",
            "assignment_group": "Homework",
            "peer_reviews": {"automatic": False},
        }
    )

    course.create_assignment.assert_not_called()
    # published is only applied on creation
    existing.edit.assert_called_once_with(
        assignment={"assignment_group_id": 7, "peer_reviews": True}
    )


def test_existing_assignment_in_sync_is_not_edited(
    make_sync: MakeSync, course: MagicMock
) -> None:
    existing = obj(id=55, name="HW1", points_possible=20.0)
    course.get_assignments.return_value = [existing]

    sync = make_sync(course=course)
    sync.sync_assignment_item(
        {"title": "HW1", "assignment": "HW1", "points_possible": 20}
    )

    existing.edit.assert_not_called()


def test_unknown_assignment_group_exits(
    make_sync: MakeSync, course: MagicMock
) -> None:
    course.get_assignments.return_value = [obj(id=1, name="HW1")]
    sync = make_sync(course=course)

    with pytest.raises(SystemExit):
        sync.sync_assignment_item(
            {"title": "HW1", "assignment": "HW1", "assignment_group": "Nope"}
        )


def test_assignment_groups_create_and_update(
    make_sync: MakeSync, course: MagicMock
) -> None:
    homework = homework_group()
    course.get_assignment_groups.return_value = [homework]
    course.apply_assignment_group_weights = False
    quizzes = obj(name="Quizzes", id=8)
    course.create_assignment_group.return_value = quizzes

    sync = make_sync(
        {
            "apply_assignment_group_weights": True,
            "assignment_groups": [
                {
                    "name": "Homework",
                    "position": 1,
                    "group_weight": 50,
                    "drop_lowest": 1,
                },
                {
                    "name": "Quizzes",
                    "position": 2,
                    "group_weight": 20,
                    "drop_lowest": 2,
                },
            ],
        },
        course=course,
    )

    sync.sync_assignment_groups()

    course.update.assert_called_once_with(
        course={"apply_assignment_group_weights": True}
    )
    # Drop rules wait until the groups have assignments (second pass)
    homework.edit.assert_called_once_with(group_weight=50)
    course.create_assignment_group.assert_called_once_with(
        name="Quizzes", position=2, group_weight=20
    )
    # New groups are cached for resolving names in items
    assert sync.get_assignment_group_id("Quizzes") == 8
    course.get_assignment_groups.assert_called_once()

    homework.edit.reset_mock()
    sync.sync_assignment_groups(rules=True)

    homework.edit.assert_called_once_with(rules={"drop_lowest": 1})
    quizzes.edit.assert_called_once_with(rules={"drop_lowest": 2})


def test_assignment_group_rules_too_few_assignments_warns(
    make_sync: MakeSync, course: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    quizzes = obj(name="Quizzes", id=8, rules={})
    quizzes.edit.side_effect = BadRequest(
        '{"errors":{"rules":[{"message":"Drop rules cannot be higher than '
        'the number of assignments"}]}}'
    )
    course.get_assignment_groups.return_value = [quizzes]

    sync = make_sync(
        {"assignment_groups": [{"name": "Quizzes", "drop_lowest": 1}]},
        course=course,
    )
    sync.sync_assignment_groups(rules=True)

    assert "too few" in " ".join(capsys.readouterr().out.split())


def test_assignment_groups_unchanged(
    make_sync: MakeSync, course: MagicMock
) -> None:
    homework = obj(
        name="Homework",
        id=7,
        position=1,
        group_weight=40.0,
        rules={"drop_lowest": 1},
    )
    course.get_assignment_groups.return_value = [homework]
    course.apply_assignment_group_weights = True

    sync = make_sync(
        {
            "apply_assignment_group_weights": True,
            "assignment_groups": [
                {
                    "name": "Homework",
                    "position": 1,
                    "group_weight": 40,
                    "drop_lowest": 1,
                }
            ],
        },
        course=course,
    )
    sync.sync_assignment_groups()

    course.update.assert_not_called()
    homework.edit.assert_not_called()
    course.create_assignment_group.assert_not_called()


def test_assignment_group_rules_preserve_other_rule(
    make_sync: MakeSync, course: MagicMock
) -> None:
    homework = obj(
        name="Homework", id=7, rules={"drop_lowest": 1, "drop_highest": 1}
    )
    course.get_assignment_groups.return_value = [homework]

    sync = make_sync(
        {"assignment_groups": [{"name": "Homework", "drop_lowest": 2}]},
        course=course,
    )
    sync.sync_assignment_groups(rules=True)

    homework.edit.assert_called_once_with(
        rules={"drop_lowest": 2, "drop_highest": 1}
    )
