"""Rubric payload encoding and association logic."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from canvasapi.util import combine_kwargs

from canvassync.sync import (
    load_rubric,
    rubric_differences,
    rubric_differs,
    rubric_payload,
)

from .conftest import MakeSync, obj

RUBRIC = {
    "title": "Project Report Rubric",
    "free_form_criterion_comments": True,
    "use_for_grading": True,
    "criteria": [
        {
            "description": "Technical content",
            "long_description": "Correct and complete analysis",
            "ratings": [
                {"points": 10, "description": "Excellent"},
                {"points": 5, "description": "Adequate"},
                {"points": 0, "description": "Missing"},
            ],
        },
        {
            "description": "Writing",
            "ratings": [
                {
                    "points": 5,
                    "description": "Clear",
                    "long_description": "Well organized",
                },
                {"points": 0, "description": "Unclear"},
            ],
        },
    ],
}


def canvas_data() -> list[dict[str, object]]:
    """Rubric data as Canvas returns it."""
    return [
        {
            "id": "_1",
            "description": "Technical content",
            "long_description": "Correct and complete analysis",
            "points": 10.0,
            "ratings": [
                {
                    "id": "a",
                    "points": 10.0,
                    "description": "Excellent",
                    "long_description": "",
                },
                {
                    "id": "b",
                    "points": 5.0,
                    "description": "Adequate",
                    "long_description": "",
                },
                {
                    "id": "c",
                    "points": 0.0,
                    "description": "Missing",
                    "long_description": "",
                },
            ],
        },
        {
            "id": "_2",
            "description": "Writing",
            "long_description": None,
            "points": 5.0,
            "ratings": [
                {
                    "id": "d",
                    "points": 5.0,
                    "description": "Clear",
                    "long_description": "Well organized",
                },
                {
                    "id": "e",
                    "points": 0.0,
                    "description": "Unclear",
                    "long_description": "",
                },
            ],
        },
    ]


@pytest.fixture
def rubric_file(tmp_path: Path) -> Path:
    path = tmp_path / "rubrics" / "report.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(RUBRIC), encoding="utf8")
    return path


def test_rubric_payload_nested_encoding() -> None:
    params = dict(
        combine_kwargs(
            rubric=rubric_payload(RUBRIC),
            rubric_association={"association_id": 5},
        )
    )

    assert params["rubric[title]"] == "Project Report Rubric"
    assert params["rubric[free_form_criterion_comments]"] is True
    assert params["rubric[criteria][0][description]"] == "Technical content"
    assert params["rubric[criteria][0][points]"] == 10
    assert params["rubric[criteria][0][ratings][1][points]"] == 5
    assert params["rubric[criteria][0][ratings][1][description]"] == (
        "Adequate"
    )
    assert params["rubric[criteria][1][points]"] == 5
    assert params["rubric[criteria][1][ratings][0][long_description]"] == (
        "Well organized"
    )
    assert params["rubric_association[association_id]"] == 5
    # No list-style "[]" keys, which Canvas would not index by position
    assert not any("[]" in key for key in params)


def test_criterion_points_are_max_rating() -> None:
    rubric = {
        "title": "t",
        "criteria": [
            {
                "description": "c",
                "ratings": [{"points": 2}, {"points": 7}, {"points": 4}],
            }
        ],
    }
    assert rubric_payload(rubric)["criteria"]["0"]["points"] == 7


def test_load_rubric_validates(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"title": "x", "criteria": []}))

    with pytest.raises(ValueError, match="at least one criterion"):
        load_rubric(path)


def test_rubric_differs() -> None:
    same = obj(free_form_criterion_comments=True, data=canvas_data())
    assert not rubric_differs(RUBRIC, same)

    changed = canvas_data()
    changed[1]["description"] = "Style"
    assert rubric_differs(
        RUBRIC, obj(free_form_criterion_comments=True, data=changed)
    )

    assert rubric_differs(
        RUBRIC, obj(free_form_criterion_comments=False, data=canvas_data())
    )


def test_rubric_differs_ignores_html_entities_and_whitespace() -> None:
    config = {
        **RUBRIC,
        "criteria": [
            {
                **RUBRIC["criteria"][0],
                "long_description": 'The "what if" & more',
            },
            RUBRIC["criteria"][1],
        ],
    }
    data = canvas_data()
    data[0]["long_description"] = "The &quot;what if&quot; &amp;  more "

    assert not rubric_differs(
        config, obj(free_form_criterion_comments=True, data=data)
    )


def test_rubric_differences_names_the_criterion() -> None:
    changed = canvas_data()
    changed[1]["description"] = "Style"

    differences = rubric_differences(
        RUBRIC, obj(free_form_criterion_comments=True, data=changed)
    )

    assert len(differences) == 1
    assert "criterion 2" in differences[0]
    assert "Style" in differences[0]


def test_rubric_without_data_is_fetched_in_full(
    make_sync: MakeSync, course: MagicMock, rubric_file: Path
) -> None:
    listed = obj(id=300, title="Project Report Rubric")
    course.get_rubrics.return_value = [listed]
    course.get_rubric.return_value = obj(
        id=300,
        title="Project Report Rubric",
        free_form_criterion_comments=True,
        data=canvas_data(),
    )
    sync = make_sync(course=course)
    assignment = obj(id=55, name="Project Report", rubric_settings={"id": 300})

    sync.sync_rubric(Path("rubrics/report.yaml"), assignment)

    course.get_rubric.assert_called_once_with(300)
    course.create_rubric.assert_not_called()


def test_create_rubric_with_association(
    make_sync: MakeSync, course: MagicMock, rubric_file: Path
) -> None:
    sync = make_sync(course=course)
    assignment = obj(id=55, name="Project Report")

    sync.sync_rubric(Path("rubrics/report.yaml"), assignment)

    course.create_rubric.assert_called_once_with(
        rubric=rubric_payload(RUBRIC),
        rubric_association={
            "association_type": "Assignment",
            "association_id": 55,
            "purpose": "grading",
            "use_for_grading": True,
            "hide_score_total": False,
        },
    )
    course.create_rubric_association.assert_not_called()


def test_associate_existing_rubric(
    make_sync: MakeSync, course: MagicMock, rubric_file: Path
) -> None:
    existing = obj(
        id=300,
        title="Project Report Rubric",
        free_form_criterion_comments=True,
        data=canvas_data(),
    )
    course.get_rubrics.return_value = [
        obj(id=1, title="Other"),
        existing,
    ]
    sync = make_sync(course=course)
    assignment = obj(id=55, name="Project Report")

    sync.sync_rubric(Path("rubrics/report.yaml"), assignment)

    course.create_rubric.assert_not_called()
    course.create_rubric_association.assert_called_once_with(
        rubric_association={
            "rubric_id": 300,
            "association_type": "Assignment",
            "association_id": 55,
            "purpose": "grading",
            "use_for_grading": True,
            "hide_score_total": False,
        }
    )


def test_already_associated_rubric(
    make_sync: MakeSync, course: MagicMock, rubric_file: Path
) -> None:
    course.get_rubrics.return_value = [
        obj(
            id=300,
            title="Project Report Rubric",
            free_form_criterion_comments=True,
            data=canvas_data(),
        )
    ]
    sync = make_sync(course=course)
    assignment = obj(id=55, name="Project Report", rubric_settings={"id": 300})

    sync.sync_rubric(Path("rubrics/report.yaml"), assignment)

    course.create_rubric.assert_not_called()
    course.create_rubric_association.assert_not_called()


def test_changed_rubric_warns_and_does_not_update(
    make_sync: MakeSync,
    course: MagicMock,
    rubric_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    changed = canvas_data()
    changed[0]["points"] = 20.0
    existing = obj(
        id=300,
        title="Project Report Rubric",
        free_form_criterion_comments=True,
        data=changed,
    )
    course.get_rubrics.return_value = [existing]
    sync = make_sync(course=course)
    assignment = obj(id=55, name="Project Report", rubric_settings={"id": 300})

    sync.sync_rubric(Path("rubrics/report.yaml"), assignment)

    assert "differs" in capsys.readouterr().out
    existing.edit.assert_not_called()
    course.create_rubric.assert_not_called()


def test_assignment_item_syncs_rubric(
    make_sync: MakeSync, course: MagicMock, rubric_file: Path
) -> None:
    course.get_assignments.return_value = [obj(id=55, name="Report")]
    sync = make_sync(course=course)

    sync.sync_assignment_item(
        {
            "title": "Report",
            "assignment": "Report",
            "rubric": "rubrics/report.yaml",
        }
    )

    course.create_rubric.assert_called_once()
