"""Discussion topic module items."""

from unittest.mock import MagicMock

from canvassync.sync import item_type

from .conftest import MakeSync, obj

HTML = "<p>Introduce yourself.</p>"

ITEM = {
    "title": "Find a Project Partner",
    "discussion": "discussions/partners.md",
    "published": True,
    "unlock_at": "1/4/2027 12:00am",
    "pinned": True,
    "indent": 0,
}


def topic(**attrs: object) -> MagicMock:
    """A mock discussion topic."""
    mock = MagicMock(spec=[*attrs, "update"])
    for key, value in attrs.items():
        setattr(mock, key, value)
    return mock


def test_item_type_discussion() -> None:
    assert item_type({"title": "x", "discussion": "d.md"}) == "Discussion"


def test_missing_discussion_is_created(
    make_sync: MakeSync, course: MagicMock
) -> None:
    course.get_discussion_topics.return_value = []
    course.create_discussion_topic.return_value = topic(id=7)
    sync = make_sync(course=course)
    sync.render_markdown = MagicMock(return_value=HTML)  # type: ignore[method-assign]

    result = sync.sync_discussion_item(ITEM)

    assert result.id == 7
    kwargs = course.create_discussion_topic.call_args.kwargs
    assert kwargs["title"] == "Find a Project Partner"
    assert kwargs["message"] == HTML
    assert kwargs["discussion_type"] == "threaded"
    assert kwargs["published"] == "1"
    assert kwargs["pinned"] == "1"
    assert kwargs["delayed_post_at"].startswith("2027-01-04T00:00:00")


def test_unchanged_discussion_is_not_updated(
    make_sync: MakeSync, course: MagicMock
) -> None:
    existing = topic(
        id=7,
        title="Find a Project Partner",
        message=HTML,
        discussion_type="threaded",
        published=True,
        pinned=True,
        delayed_post_at=None,
    )
    course.get_discussion_topics.return_value = [existing]
    sync = make_sync(course=course)
    sync.render_markdown = MagicMock(return_value=HTML)  # type: ignore[method-assign]

    item = {k: v for k, v in ITEM.items() if k != "unlock_at"}
    assert sync.sync_discussion_item(item) is existing

    existing.update.assert_not_called()
    course.create_discussion_topic.assert_not_called()


def test_changed_message_and_settings_are_updated(
    make_sync: MakeSync, course: MagicMock
) -> None:
    existing = topic(
        id=7,
        title="Find a Project Partner",
        message="<p>Old prompt.</p>",
        discussion_type="threaded",
        published=True,
        pinned=False,
        delayed_post_at="2027-01-04T05:00:00Z",
    )
    course.get_discussion_topics.return_value = [existing]
    sync = make_sync(course=course)
    sync.render_markdown = MagicMock(return_value=HTML)  # type: ignore[method-assign]

    item = {k: v for k, v in ITEM.items() if k != "unlock_at"}
    sync.sync_discussion_item(item)

    existing.update.assert_called_once_with(message=HTML, pinned="1")


def test_discussion_module_item_created(
    make_sync: MakeSync, course: MagicMock
) -> None:
    course.get_discussion_topics.return_value = []
    course.create_discussion_topic.return_value = topic(id=7)
    sync = make_sync(course=course)
    sync.render_markdown = MagicMock(return_value=HTML)  # type: ignore[method-assign]
    module = MagicMock()
    module.create_module_item.return_value = obj(id=1)

    sync.create_module_item(module, ITEM, 0)

    attrs = module.create_module_item.call_args.kwargs["module_item"]
    assert attrs["type"] == "Discussion"
    assert attrs["content_id"] == 7
