import datetime
import hashlib
import html
import importlib.resources
import logging
import re
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from functools import cached_property
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast
from urllib.parse import urlparse

import css_inline
import dateutil.parser
import pypandoc
import requests
import tzlocal
import yaml
from bs4 import BeautifulSoup
from canvasapi import Canvas
from canvasapi.assignment import Assignment, AssignmentGroup
from canvasapi.course import Course
from canvasapi.discussion_topic import DiscussionTopic
from canvasapi.exceptions import BadRequest, ResourceDoesNotExist
from canvasapi.file import File
from canvasapi.folder import Folder
from canvasapi.group import GroupCategory
from canvasapi.module import Module, ModuleItem
from canvasapi.page import Page
from canvasapi.quiz import Quiz
from canvasapi.rubric import Rubric
from jinja2 import Environment, FileSystemLoader
from rich import print
from rich.markup import escape

TextSource: TypeAlias = Path | str
"""A text source.

Either a path to a file or a raw string.
"""

ConfigDict: TypeAlias = dict[str, Any]
"""A course configuration dictionary."""

ModuleItemConfig: TypeAlias = dict[str, Any]


def normalize_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")

    return soup.prettify()


def html_equiv(a: str, b: str) -> bool:
    return normalize_html(a) == normalize_html(b)


def parse_datetime(date: str) -> datetime.datetime:
    """Parse a string date/time using local time zone.

    Args:
        date (str): A date to parse

    Returns:
        datetime.datetime: Parse datetime object
    """
    default_date = datetime.datetime.combine(
        datetime.datetime.now(),
        datetime.time(0, tzinfo=tzlocal.get_localzone()),
    )

    return dateutil.parser.parse(date, default=default_date)


def make_filter(
    keywords: list[str] | None, regexps: list[str] | None = None
) -> Callable[[str], bool] | None:
    """Create a string filter predicate.

    The returned predicate returns true for any string that matches any of the
    specified keywords or regular expressions.

    Args:
        keywords (Optional[List[str]]): Keywords to match.
        regexps (Optional[List[str]], optional): Regular expressions to match.
          Defaults to None.

    Returns:
        Optional[Callable[[str], bool]]: Returns a filter or None if no
          keywords or regular expressions were specified.
    """
    if keywords is None:
        keywords = []

    if regexps is None:
        regexps = []

    if len(keywords) == 0 and len(regexps) == 0:
        return None

    regexp = "|".join([re.escape(k) for k in keywords] + regexps)
    pat = re.compile(regexp)

    return lambda s: bool(pat.search(s))


def flatten_items(
    items: list[ModuleItemConfig], indent: int = 0
) -> list[ModuleItemConfig]:
    """Flatten nested module items."""
    result: list[ModuleItemConfig] = []

    for item in items:
        flat_item = dict(item)
        flat_item["indent"] = indent

        subitems = []
        if "items" in flat_item:
            subitems = flatten_items(flat_item["items"], indent=indent + 1)
            del flat_item["items"]

        result.append(flat_item)
        result += subitems

    return result


def date_differs(value: str, current: str | None) -> bool:
    """Determine whether a configured date differs from a Canvas date.

    Args:
        value (str): Configured date/time string (local time zone).
        current (Optional[str]): ISO 8601 date/time from Canvas, if any.

    Returns:
        bool: True if the dates differ or Canvas has no date set.
    """
    if not current:
        return True

    return parse_datetime(value) != dateutil.parser.isoparse(current)


def list_differs(value: list[Any], current: list[Any] | None) -> bool:
    """Determine whether two lists differ, ignoring order."""
    return sorted(value) != sorted(current or [])


ItemType = Literal[
    "Page",
    "ExternalUrl",
    "SubHeader",
    "File",
    "Assignment",
    "Quiz",
    "Discussion",
]


def item_type(item: ModuleItemConfig) -> ItemType:
    if "page" in item:
        return "Page"

    if "page_contents" in item:
        return "Page"

    if "url" in item:
        return "ExternalUrl"

    if "file" in item:
        return "File"

    if "quiz" in item:
        return "Quiz"

    if "discussion" in item:
        return "Discussion"

    if "assignment" in item:
        return "Assignment"

    return "SubHeader"


SHA256_MARKER_RE = re.compile(
    r"<p[^>]*\bdata-canvassync-sha256=\"([0-9a-fA-F]{64})\"[^>]*>\s*</p>\s*"
)
"""Regular expression matching the QTI sha256 marker in a quiz description."""


def file_sha256(path: Path) -> str:
    """Compute the hex sha256 digest of a file."""
    digest = hashlib.sha256()

    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)

    return digest.hexdigest()


def get_sha256_marker(description: str | None) -> str | None:
    """Extract the CanvasSync sha256 marker from a quiz description.

    Args:
        description (Optional[str]): Quiz description HTML.

    Returns:
        Optional[str]: The stored sha256, or None if there is no marker.
    """
    if not description:
        return None

    m = SHA256_MARKER_RE.search(description)
    if m is None:
        return None

    return m.group(1).lower()


def set_sha256_marker(description: str | None, sha256: str) -> str:
    """Return a quiz description with a CanvasSync sha256 marker at the end.

    Any existing marker is removed first.
    """
    html = SHA256_MARKER_RE.sub("", description or "").rstrip()
    marker = f'<p data-canvassync-sha256="{sha256}"></p>'

    return f"{html}\n{marker}" if html else marker


def load_rubric(path: Path) -> ConfigDict:
    """Load and validate a YAML rubric file.

    Args:
        path (Path): Path to the rubric YAML file.

    Returns:
        ConfigDict: The rubric configuration.
    """
    with path.open("r", encoding="utf8") as f:
        rubric = cast(ConfigDict, yaml.safe_load(f))

    if not isinstance(rubric, dict) or "title" not in rubric:
        raise ValueError(f"Rubric '{path:}' must have a title")

    criteria = rubric.get("criteria")
    if not isinstance(criteria, list) or len(criteria) == 0:
        raise ValueError(f"Rubric '{path:}' must have at least one criterion")

    for criterion in criteria:
        if "description" not in criterion:
            raise ValueError(
                f"Rubric '{path:}' has a criterion without a description"
            )

        if not criterion.get("ratings"):
            raise ValueError(
                f"Rubric '{path:}' criterion "
                f"'{criterion['description']:}' has no ratings"
            )

    return rubric


def rubric_criteria(rubric: ConfigDict) -> list[ConfigDict]:
    """Normalize the criteria of a rubric configuration.

    The points of each criterion are the maximum points of its ratings.
    """
    criteria: list[ConfigDict] = []

    for criterion in rubric["criteria"]:
        ratings = [
            {
                "points": rating["points"],
                "description": rating.get("description", ""),
                "long_description": rating.get("long_description", ""),
            }
            for rating in criterion["ratings"]
        ]

        criteria.append(
            {
                "description": criterion["description"],
                "long_description": criterion.get("long_description", ""),
                "points": max(rating["points"] for rating in ratings),
                "ratings": ratings,
            }
        )

    return criteria


def canvas_bool(value: Any) -> str:
    """Encode a boolean form parameter as Canvas's rubric endpoints expect.

    canvasapi sends Python booleans as "True"/"False", which some Canvas
    endpoints (notably rubric creation) do not read as true; "1"/"0" is
    understood everywhere.
    """
    return "1" if value else "0"


def rubric_payload(rubric: ConfigDict) -> ConfigDict:
    """Build the ``rubric`` parameter for Canvas's create rubric API.

    Canvas expects criteria and ratings as hashes indexed by position,
    e.g., ``rubric[criteria][0][ratings][0][points]``. canvasapi encodes
    Python lists as ``[]``, so lists are converted to dictionaries with
    string-integer keys.
    """
    criteria: ConfigDict = {}

    for i, criterion in enumerate(rubric_criteria(rubric)):
        criteria[str(i)] = {
            "description": criterion["description"],
            "long_description": criterion["long_description"],
            "points": criterion["points"],
            "ratings": {
                str(j): rating for j, rating in enumerate(criterion["ratings"])
            },
        }

    return {
        "title": rubric["title"],
        "free_form_criterion_comments": canvas_bool(
            rubric.get("free_form_criterion_comments", False)
        ),
        "criteria": criteria,
    }


def _rubric_text(value: Any) -> str:
    """Normalize rubric text for comparison.

    Canvas stores rubric text as HTML, so quotes and ampersands may come back
    as entities, and whitespace may change.
    """
    return " ".join(html.unescape(str(value or "")).split())


def _rubric_signature(
    free_form: bool, criteria: list[ConfigDict]
) -> tuple[Any, ...]:
    """Comparable representation of rubric content."""
    return (
        bool(free_form),
        tuple(
            (
                _rubric_text(criterion.get("description")),
                _rubric_text(criterion.get("long_description")),
                float(criterion.get("points") or 0),
                tuple(
                    (
                        float(rating.get("points") or 0),
                        _rubric_text(rating.get("description")),
                        _rubric_text(rating.get("long_description")),
                    )
                    for rating in criterion.get("ratings") or []
                ),
            )
            for criterion in criteria
        ),
    )


def rubric_differences(rubric: ConfigDict, canvas_rubric: Rubric) -> list[str]:
    """Describe how a Canvas rubric's content differs from a config.

    Returns:
        list[str]: Human-readable differences; empty if the content matches.
    """
    config_free, config_criteria = _rubric_signature(
        rubric.get("free_form_criterion_comments", False),
        rubric_criteria(rubric),
    )
    canvas_free, canvas_criteria = _rubric_signature(
        getattr(canvas_rubric, "free_form_criterion_comments", False),
        getattr(canvas_rubric, "data", None) or [],
    )

    differences: list[str] = []

    if config_free != canvas_free:
        differences.append(
            f"free-form comments: Canvas {canvas_free}, file {config_free}"
        )

    if len(config_criteria) != len(canvas_criteria):
        differences.append(
            f"{len(canvas_criteria)} criteria in Canvas, "
            f"{len(config_criteria)} in the file"
        )

    for i, (ours, theirs) in enumerate(
        zip(config_criteria, canvas_criteria, strict=False)
    ):
        if ours != theirs:
            differences.append(
                f"criterion {i + 1} ('{ours[0]}'): Canvas {theirs!r}, "
                f"file {ours!r}"
            )

    return differences


def rubric_differs(rubric: ConfigDict, canvas_rubric: Rubric) -> bool:
    """Determine whether a Canvas rubric's content differs from a config."""
    return bool(rubric_differences(rubric, canvas_rubric))


def get_page_source(item: ModuleItemConfig) -> TextSource:
    assert item_type(item) == "Page"

    if "page" in item:
        return Path(item["page"])
    else:
        return cast(str, item["page_contents"])


class CanvasSync:
    config: ConfigDict
    """Canvas sync configuration."""

    root: Path
    """Root path."""

    _folders: list[Folder] | None = None
    """Cached folders"""

    _assignment_groups: dict[str, AssignmentGroup] | None = None
    """Cached assignment groups, by name."""

    _group_categories: dict[str, GroupCategory] | None = None
    """Cached group sets (Canvas group categories), by name."""

    create_assignments: bool = False
    """Create assignments that do not exist in Canvas."""

    replace_quizzes: bool = False
    """Re-import quizzes whose QTI zip has changed."""

    migration_timeout: float = 600.0
    """Seconds to wait for a QTI content migration to finish."""

    migration_poll_interval: float = 2.0
    """Seconds between content migration status checks."""

    def __init__(
        self,
        config_path: Path,
        root: Path | None = None,
        api_key: str | None = None,
        course_id: int | None = None,
    ) -> None:
        with config_path.open("r", encoding="utf8") as f:
            self.config = cast(ConfigDict, yaml.safe_load(f))

        self._api_key = api_key
        self._course_id = course_id

        if root is None:
            self.root = config_path.parent
        else:
            self.root = root

        self.create_assignments = bool(
            self.config.get("create_assignments", False)
        )

        self._synced_quizzes: dict[str, Quiz] = {}
        self._status_callback: Callable[[str, str | None], None] | None = None
        self._current_module: str | None = None

    @property
    def api_url(self) -> str:
        """Canvas API URL."""
        return cast(str, self.config["api_url"])

    @property
    def api_key(self) -> str:
        """Canvas API key."""
        if self._api_key is not None:
            return self._api_key

        return cast(str, self.config["api_key"])

    @property
    def course_id(self) -> int | None:
        """Canvas course ID.

        The ``--course-id`` command line option overrides ``course_id``
        from the config file.
        """
        if self._course_id is not None:
            return self._course_id

        if self.config.get("course_id") is not None:
            return int(self.config["course_id"])

        return None

    @property
    def course_sis(self) -> str:
        """Course SIS."""
        if self.config.get("course_sis") is None:
            raise ValueError(
                "No course specified: set course_id or course_sis in the "
                "config, or pass --course-id"
            )

        return cast(str, self.config["course_sis"])

    @cached_property
    def canvas(self) -> Canvas:
        """Canvas API object."""
        return Canvas(self.api_url, self.api_key)

    @cached_property
    def course(self) -> Course:
        """Canvas course."""
        kwargs = {"include[]": "syllabus_body"}

        course_id = self.course_id
        if course_id is not None:
            logging.debug("Getting course by ID %d", course_id)
            return self.canvas.get_course(course_id, **kwargs)

        logging.debug("Getting course by SIS ID '%s'", self.course_sis)
        return self.canvas.get_course(
            self.course_sis, use_sis_id=True, **kwargs
        )

    def get_assignment(self, name: str) -> Assignment | None:
        assignments = self.course.get_assignments()

        for assignment in assignments:
            if assignment.name == name:
                return assignment

        return None

    @property
    def assignment_groups(self) -> dict[str, AssignmentGroup]:
        """Course assignment groups, by name (cached)."""
        if self._assignment_groups is None:
            self._assignment_groups = {
                group.name: group
                for group in self.course.get_assignment_groups()
            }

        return self._assignment_groups

    def get_assignment_group_id(self, name: str) -> int:
        """Resolve an assignment group name to its Canvas ID."""
        group = self.assignment_groups.get(name)

        if group is None:
            print(
                f"[red]Cannot find assignment group {name:}[/red]",
                file=sys.stderr,
            )
            sys.exit(1)

        return cast(int, group.id)

    def sync_assignment_groups(self, rules: bool = False) -> None:
        """Synchronize assignment groups and group weighting.

        Missing groups are created by name and existing groups are
        updated when their position or weight differ. Groups are never
        deleted.

        Canvas refuses drop rules larger than the number of assignments in a
        group, so drop rules are only applied when ``rules`` is true, which
        :meth:`sync` does after the modules (and so the assignments and
        quizzes) have been synchronized. A group that is still too small, for
        example during a ``--limit`` sync, gets a warning instead.

        Args:
            rules (bool): Apply drop rules instead of creating groups and
              updating positions and weights.
        """
        if rules:
            self.sync_assignment_group_rules()
            return

        if "apply_assignment_group_weights" in self.config:
            apply = bool(self.config["apply_assignment_group_weights"])

            if (
                getattr(self.course, "apply_assignment_group_weights", None)
                != apply
            ):
                logging.debug("Setting apply_assignment_group_weights")
                self.course.update(
                    course={"apply_assignment_group_weights": apply}
                )

        groups = cast(
            list[ConfigDict], self.config.get("assignment_groups", [])
        )

        for group_config in groups:
            name = group_config["name"]
            group = self.assignment_groups.get(name)

            attrs: ConfigDict = {}

            for attr in ["position", "group_weight"]:
                if attr in group_config and (
                    group is None
                    or getattr(group, attr, None) != group_config[attr]
                ):
                    attrs[attr] = group_config[attr]

            if group is None:
                logging.debug("Creating assignment group '%s'", name)
                group = self.course.create_assignment_group(name=name, **attrs)
                self.assignment_groups[name] = group
            elif attrs:
                logging.debug("Updating assignment group '%s'", name)
                group.edit(**attrs)

    @property
    def group_categories(self) -> dict[str, GroupCategory]:
        """Course group sets (group categories), by name (cached)."""
        if self._group_categories is None:
            self._group_categories = {
                category.name: category
                for category in self.course.get_group_categories()
            }

        return self._group_categories

    def get_group_category_id(self, name: str) -> int:
        """Resolve a group set name to its Canvas group category ID."""
        category = self.group_categories.get(name)

        if category is None:
            print(
                f"[red]Cannot find group set {name:}[/red]",
                file=sys.stderr,
            )
            sys.exit(1)

        return cast(int, category.id)

    def sync_group_sets(self) -> None:
        """Synchronize group sets (Canvas group categories).

        Each entry of the optional top-level ``group_sets`` list names a group
        set. Missing sets are created; existing sets are updated when
        ``self_signup`` or ``group_limit`` differ. If ``groups`` is given,
        empty groups named "<set name> <n>" are added until the set has at
        least that many groups, so students can join them (self sign-up).
        Group sets, groups and memberships are never deleted or otherwise
        changed.
        """
        sets = cast(list[ConfigDict], self.config.get("group_sets", []))

        for set_config in sets:
            name = set_config["name"]
            category = self.group_categories.get(name)

            attrs: ConfigDict = {}

            if "self_signup" in set_config:
                signup = "enabled" if set_config["self_signup"] else None

                current = getattr(category, "self_signup", None)

                if category is None or current != signup:
                    attrs["self_signup"] = signup

            if "group_limit" in set_config:
                limit = set_config["group_limit"]

                current = getattr(category, "group_limit", None)

                if category is None or current != limit:
                    attrs["group_limit"] = limit

            if category is None:
                logging.debug("Creating group set '%s'", name)
                category = self.course.create_group_category(name, **attrs)
                self.group_categories[name] = category
            elif attrs:
                logging.debug("Updating group set '%s'", name)
                category.update(**attrs)

            wanted = int(set_config.get("groups", 0))

            if wanted:
                existing = list(category.get_groups())
                names = {group.name for group in existing}
                n, count = 1, len(existing)

                while count < wanted:
                    group_name = f"{name} {n}"
                    n += 1

                    if group_name in names:
                        continue

                    logging.debug("Creating group '%s'", group_name)
                    category.create_group(name=group_name)
                    count += 1

    def sync_assignment_group_rules(self) -> None:
        """Apply the configured drop rules to existing assignment groups."""
        groups = cast(
            list[ConfigDict], self.config.get("assignment_groups", [])
        )

        for group_config in groups:
            name = group_config["name"]
            group = self.assignment_groups.get(name)

            if group is None:
                continue

            rules: ConfigDict = {}

            current_rules = (
                cast(ConfigDict, getattr(group, "rules", None) or {})
                if group is not None
                else {}
            )

            for rule in ["drop_lowest", "drop_highest"]:
                if (
                    rule in group_config
                    and current_rules.get(rule, 0) != group_config[rule]
                ):
                    rules[rule] = group_config[rule]

            if not rules:
                continue

            # Rules not given are reset by Canvas, so send all of them.
            all_rules = {
                **{
                    k: v
                    for k, v in current_rules.items()
                    if k in ("drop_lowest", "drop_highest")
                },
                **{
                    rule: group_config[rule]
                    for rule in ["drop_lowest", "drop_highest"]
                    if rule in group_config
                },
            }

            logging.debug("Setting drop rules on assignment group '%s'", name)
            try:
                group.edit(rules=all_rules)
            except BadRequest as exc:
                if "Drop rules cannot be higher" not in str(exc):
                    raise
                print(
                    f"[yellow]Not setting drop rules on assignment group "
                    f"'{name:}' yet: it has too few assignments. They are "
                    "set by a later sync.[/yellow]"
                )

    def get_folder(self, folderpath: str) -> Folder | None:
        if self._folders is None:
            self._folders = list(self.course.get_folders())

        for folder in self._folders:
            if re.sub(r"^course files/", "", folder.full_name) == folderpath:
                return folder

        return None

    def get_file(
        self, filename: str, parent_folder_path: str | None = None
    ) -> File | None:
        folder: Folder | None = None

        if parent_folder_path is not None:
            folder = self.get_folder(parent_folder_path)
            if folder is None:
                return None

        if folder is None:
            files = self.course.get_files()
        else:
            files = folder.get_files()

        for file in files:
            if file.filename == filename:
                # If folder was specified, make sure file is in folder
                if folder is not None:
                    if file.folder_id == folder.id:
                        return file
                else:
                    return file

        return None

    def upload_file(
        self, filepath: Path, check_contents: bool = False
    ) -> tuple[bool, File]:
        """Upload a file.

        If the file already exists on Canvas and does not need to be updated,
        return the existing File object.

        Args:
            filepath (Path): Path to file.
            check_contents (bool, optional): Always check file contents.
              Defaults to False.

        Returns:
            Tuple[bool, File]: flag indicating if file was uploaded and File
            object.
        """
        # Bail if the file doesn't exist
        if not filepath.exists():
            raise FileNotFoundError(f"File '{filepath:}' does not exist")

        # Determine file's parent folder
        parent_folder_path: str = str(filepath.relative_to(self.root).parent)

        # See if a file by this name exists
        file: File | None = self.get_file(
            filepath.name, parent_folder_path=parent_folder_path
        )

        # If so, check if file has been modified
        if file is not None:
            if check_contents:
                logging.debug("Getting file contents")
                old_contents = file.get_contents(binary=True)

                with filepath.open("rb") as f:
                    new_contents = f.read()

                # Contents differ. Delete the old file and force an upload.
                if new_contents != old_contents:
                    logging.debug("File contents differ")
                    file = None
                else:
                    logging.debug("File contents identical")
            else:
                modified = datetime.datetime.fromtimestamp(
                    filepath.stat().st_mtime, tz=datetime.timezone.utc
                )

                if modified > file.modified_at_date:
                    logging.debug("File modified")
                    file = None

        if file is not None:
            return False, file

        if file is None:
            logging.debug("Uploading '%s'", str(filepath))
            success, response = self.course.upload(
                filepath, parent_folder_path=parent_folder_path
            )
            if success:
                # If we created a new folder, invalidate folder cache
                if (
                    self._folders is not None
                    and self.get_folder(parent_folder_path) is None
                ):
                    self._folders = None

                return True, self.canvas.get_file(response["id"])

            raise ValueError(f"Could not upload {filepath:}")

    def get_page(self, id_or_url: str) -> Page:
        return self.course.get_page(id_or_url)

    def get_page_by_title(self, title: str) -> Page | None:
        pages = list(self.course.get_pages(search_term=title))
        if len(pages) == 0:
            return None
        elif len(pages) == 1:
            return self.get_page(pages[0].page_id)
        else:
            raise ValueError(f"Multiple pages match '{title:}'")

    def update_page_by_title(self, title: str, html: str) -> Page:
        page = self.get_page_by_title(title)
        if page is None:
            logging.debug("Creating page '%s'", title)
            return self.course.create_page(
                wiki_page={"title": title, "body": html}
            )

        if page.body is None or not html_equiv(html, page.body):
            logging.debug("Updating page '%s'", title)
            page.edit(wiki_page={"body": html})

        return page

    def render_template(
        self, source: TextSource, template_vars: ConfigDict | None = None
    ) -> str:
        """Render a template using Jinja."""
        if isinstance(source, Path):
            with open(self.root / source, encoding="utf8") as f:
                text = f.read()
        else:
            text = source

        loader = FileSystemLoader(self.root)
        env = Environment(
            loader=loader,
            comment_start_string="{%",
            comment_end_string="%}",
        )

        # Add filters
        env.filters["canvas_link"] = self.canvas_link

        # Add template variables
        env.globals.update(cast(ConfigDict, self.config.get("vars", {})))
        if template_vars is not None:
            env.globals.update(template_vars)

        # Create and render template
        template = env.from_string(text)

        return template.render(site=self.config.get("data", None))

    def canvas_link(self, value: str) -> str:
        page = self.get_page_by_title(value)
        if page is None:
            return ""

        return cast(str, page.html_url)

    def render_markdown(
        self, source: TextSource, template_vars: ConfigDict | None = None
    ) -> str:
        """Render Markdown (using pandoc)."""
        # First render Jinja templates in the Markdown source to get the final
        # Markdown text to convert to HTML with pandoc.
        jinja_text = self.render_template(source, template_vars)

        # Extra Pandoc arguments to use when rendering Markdown.
        extra_args: list[str] = []

        # Use MathJax to render math.
        extra_args = ["--mathjax"]

        # Pass metadata from config
        for key, value in self.config.get("pandoc_metadata", {}).items():
            extra_args += ["--metadata", f"{key}={value}"]

        # Collect bundled Lua filters
        filter_dir = importlib.resources.files("canvassync").joinpath(
            "filters"
        )

        with ExitStack() as stack:
            for item in sorted(filter_dir.iterdir(), key=lambda f: f.name):
                if item.name.endswith(".lua"):
                    path = stack.enter_context(
                        importlib.resources.as_file(item)
                    )
                    extra_args += ["--lua-filter", str(path)]

            # Render using pandoc
            html = pypandoc.convert_text(
                jinja_text,
                to="html5+raw_html+smart",
                format="md",
                extra_args=extra_args,
            )

        return self.postprocess_html(html, source)

    def postprocess_html(self, html: str, source: TextSource) -> str:
        """Postprocess rendered HTML for Canvas."""
        # Attach co-located CSS file if it exists.
        if isinstance(source, Path):
            css_path = self.root / source.with_suffix(".css")
            if css_path.exists():
                css = css_path.read_text(encoding="utf8")
                html = f"<style>\n{css}\n</style>\n{html}"

        # Inline co-located CSS file onto elements since Canvas strips <style>
        # blocks.
        html = css_inline.inline(html)

        # Upload all images
        soup = BeautifulSoup(html, "lxml")

        for img in soup.find_all("img"):
            # Upload image file
            assert isinstance(source, Path)

            img_src = cast(str, img["src"])
            _, file = self.upload_file(self.root / source.parent / img_src)

            # Replace image source with uploaded image
            file_url = f"/courses/{self.course.id:}/files/{file.id:}/preview"

            img["src"] = file_url

        return soup.prettify()

    def sync(
        self,
        limits: list[str] | None = None,
        status_callback: Callable[[str, str | None], None] | None = None,
        create_assignments: bool | None = None,
        replace_quizzes: bool = False,
    ) -> None:
        """Synchronize course.

        Args:
            limits (Optional[List[str]]): Only synchronize modules matching
              these keywords.
            status_callback (Optional[Callable]): Progress callback taking a
              module name and an optional item description.
            create_assignments (Optional[bool]): Create missing assignments.
              Defaults to the config's ``create_assignments``.
            replace_quizzes (bool): Re-import quizzes whose QTI zip changed.
        """
        if create_assignments:
            self.create_assignments = True

        self.replace_quizzes = replace_quizzes
        self._status_callback = status_callback
        self._synced_quizzes = {}

        self.sync_assignment_groups()
        self.sync_group_sets()

        self.sync_syllabus(self.course)

        course_modules = self.course.get_modules()

        for idx, module in enumerate(
            cast(list[ModuleItemConfig], self.config["modules"])
        ):
            if len(list(course_modules)) > idx:
                course_module = course_modules[idx]
            else:
                course_module = self.course.create_module(
                    module={"name": module["name"], "position": idx}
                )

            self.sync_module(
                module,
                course_module,
                pred=make_filter(limits),
                status_callback=status_callback,
            )

        self.sync_assignment_groups(rules=True)

    def sync_syllabus(self, course: Course) -> None:
        """Synchronize course syllabus."""
        if "syllabus" in self.config:
            logging.debug("Rendering syllabus")

            html = self.render_markdown(Path(self.config["syllabus"]))

            if course.syllabus_body is None or not html_equiv(
                course.syllabus_body, html
            ):
                logging.debug("Updating course syllabus")
                course.update(course={"syllabus_body": html})

    def find_module_item(
        self, item: ModuleItemConfig, idx: int, module_items: list[ModuleItem]
    ) -> ModuleItem | None:
        """Find a module item corresponding to an item dictionary.

        Args:
            item (Any): An item dictionary.
            idx (int): Item index.
            module_items (List[ModuleItem]): All module items.

        Returns:
            Optional[ModuleItem]: _description_
        """
        if len(module_items) > idx and module_items[idx].type == item_type(
            item
        ):
            # Items with the same title always match
            if module_items[idx].title == item["title"]:
                return module_items[idx]

            # Two sub-headers always match (we'll rename the subheader)
            if item_type(item) in ["SubHeader"]:
                return module_items[idx]

        # Don't match sub-headers by title if they're not in the same position
        # because the same sub-header may occur more than once, e.g., "Reading"
        if item_type(item) in ["SubHeader"]:
            return None

        for course_item in module_items:
            if (
                course_item.type == item_type(item)
                and course_item.title == item["title"]
            ):
                return course_item

        return None

    def sync_module(
        self,
        module: ModuleItemConfig,
        course_module: Module,
        pred: Callable[[str], bool] | None = None,
        status_callback: Callable[[str, str | None], None] | None = None,
    ) -> None:
        if pred is not None and not pred(course_module.name):
            return

        if status_callback is not None:
            status_callback(module["name"], None)

        self._current_module = module["name"]

        logging.debug("Synchronizing module '%s'", module["name"])
        course_module.edit(
            module={
                "name": module["name"],
                "published": module.get("published", False),
            }
        )

        # Set date attributes
        for date_attr in ["unlock_at"]:
            if date_attr in module:
                dt = parse_datetime(module[date_attr])

                if not hasattr(module, date_attr) or (
                    hasattr(module, date_attr)
                    and dt
                    != dateutil.parser.isoparse(getattr(module, date_attr))
                ):
                    course_module.edit(module={date_attr: dt.isoformat()})

        # Get all course module items
        course_module_items = list(course_module.get_module_items())

        # Flatten module items
        module_items = flatten_items(
            cast(list[ModuleItemConfig], module.get("items", []))
        )

        # Sync module items
        for idx, item in enumerate(module_items):
            if status_callback is not None:
                status_callback(module["name"], item["title"])

            logging.debug("Looking for %s", item["title"])
            course_module_item = self.find_module_item(
                item, idx, course_module_items
            )

            # If the item doesn't exist we need to create it. If it does exist
            # but we can't synchronize it (e.g., because the file has changed),
            # we need to delete and re-create it.
            create = False

            if course_module_item is None:
                logging.debug("Not found, creating")
                create = True
            else:
                if not self.sync_module_item(
                    course_module, item, idx, course_module_item
                ):
                    logging.debug(
                        "Cannot synchronize, deleting and re-creating"
                    )
                    try:
                        course_module_item.delete()
                    except ResourceDoesNotExist:
                        # Canvas may already have removed the item, e.g.,
                        # when its quiz was deleted.
                        logging.debug("Module item already deleted")
                    create = True

            if create:
                course_module_item = self.create_module_item(
                    course_module, item, idx
                )
                course_module_items.insert(idx, course_module_item)
                # Re-fetching module items reduces churn, but it is expensive,
                # so we suffer the temporary churn.
                # course_module_items = list(course_module.get_module_items())

        # Delete extra course items
        course_module_items = list(course_module.get_module_items())

        for course_module_item in course_module_items[len(module_items) :]:
            logging.debug("Deleting extra item: %s", course_module_item)
            course_module_item.delete()

    def create_module_item(
        self, course_module: Module, item: ModuleItemConfig, idx: int
    ) -> ModuleItem:
        the_type = item_type(item)

        logging.debug(
            "Creating module item (%s) '%s' at index %d",
            the_type,
            item["title"],
            idx,
        )

        title = item["title"]

        attrs = {
            "title": title,
            "type": the_type,
            "position": idx + 1,
            "indent": item["indent"],
        }

        if the_type == "Page":
            html = self.render_markdown(
                get_page_source(item), template_vars=item.get("vars")
            )

            page = self.update_page_by_title(title, html)

            attrs["page_url"] = page.url
        elif the_type == "ExternalUrl":
            attrs["external_url"] = item["url"]
            attrs["new_tab"] = item.get("new_tab", False)
        elif the_type == "SubHeader":
            pass
        elif the_type == "File":
            filepath: Path = self.root / Path(item["file"])
            _, file = self.upload_file(filepath)
            self.set_file_published(file, item.get("published", False))

            attrs["content_id"] = file.id
        elif the_type == "Assignment":
            assignment = self.sync_assignment_item(item)

            attrs["content_id"] = assignment.id
        elif the_type == "Quiz":
            quiz = self.sync_quiz_item(item)

            attrs["content_id"] = quiz.id
        elif the_type == "Discussion":
            topic = self.sync_discussion_item(item)

            attrs["content_id"] = topic.id
        else:
            raise ValueError(f"Can't create item type {the_type:}")

        course_item = course_module.create_module_item(module_item=attrs)

        # We must set the published flag after item creation
        course_item.edit(
            module_item={"published": item.get("published", False)}
        )

        return course_item

    def set_file_published(self, file: File, published: bool) -> None:
        """Publish or unpublish a course file.

        An unpublished file is locked, so students can neither see it in a
        module nor open it by its link. ("Hidden" alone only removes it from
        the Files list; anyone with the link can still open it.)
        """
        locked = not published

        if (
            getattr(file, "locked", None) != locked
            or getattr(file, "hidden", None) is not False
        ):
            logging.debug(
                "%s file '%s'",
                "Publishing" if published else "Unpublishing",
                file.filename,
            )
            file.update(locked=locked, hidden=False)

    def sync_module_item(
        self,
        course_module: Module,
        item: ModuleItemConfig,
        idx: int,
        course_item: ModuleItem,
    ) -> bool:
        the_type = item_type(item)

        logging.debug(
            "Synchronizing '%s' (%s) with '%s' (%s)",
            item["title"],
            the_type,
            course_item.title,
            course_item.type,
        )

        if the_type != course_item.type:
            raise ValueError(
                f"Cannot synchronize {the_type:} with {course_item.type:}"
            )

        # Determine item title, indent, and position
        title = item["assignment"] if "assignment" in item else item["title"]

        attrs = {
            "title": title,
            "indent": item["indent"],
            "position": idx + 1,
        }

        if the_type == "Page":
            html = self.render_markdown(
                get_page_source(item), template_vars=item.get("vars")
            )
            page = self.update_page_by_title(item["title"], html)

            attrs["page_url"] = page.url
        elif the_type == "ExternalUrl":
            if course_item.external_url != item["url"]:
                attrs["external_url"] = item["url"]

            if course_item.new_tab != item.get("new_tab", False):
                attrs["new_tab"] = item.get("new_tab", False)
        elif the_type == "SubHeader":
            pass
        elif the_type == "File":
            filepath: Path = self.root / Path(item["file"])

            if not filepath.exists():
                print(
                    "[red]Not updating because file "
                    f"'{filepath:}' does not exist[/red]"
                )
            else:
                _, file = self.upload_file(filepath)

                self.set_file_published(file, item.get("published", False))

                if course_item.content_id != file.id:
                    return False
        elif the_type == "Assignment":
            assignment = self.sync_assignment_item(item)

            attrs["content_id"] = assignment.id
        elif the_type == "Quiz":
            quiz = self.sync_quiz_item(item)

            # The quiz was replaced, so the module item must be re-created
            if course_item.content_id != quiz.id:
                return False
        elif the_type == "Discussion":
            topic = self.sync_discussion_item(item)

            if course_item.content_id != topic.id:
                return False
        else:
            raise ValueError(f"Can't handle item type {the_type:}")

        logging.debug("Updating %s '%s'", course_item.type, item["title"])

        # A file module item follows its file's published state (see
        # set_file_published); it can't be unpublished directly.
        if the_type != "File":
            attrs["published"] = item.get("published", False)

        course_item.edit(module_item=attrs)

        return True

    def assignment_settings(
        self, item: ModuleItemConfig, assignment: Assignment | None = None
    ) -> ConfigDict:
        """Compute assignment settings from an item that differ from Canvas.

        Args:
            item (ModuleItemConfig): Assignment item.
            assignment (Optional[Assignment]): Existing assignment, or None
              to compute settings for a new assignment.

        Returns:
            ConfigDict: Settings to send to Canvas.
        """

        def differs(attr: str, value: Any) -> bool:
            return (
                assignment is None or getattr(assignment, attr, None) != value
            )

        attrs: ConfigDict = {}

        for attr in ["points_possible", "grading_type"]:
            if attr in item and differs(attr, item[attr]):
                attrs[attr] = item[attr]

        for attr in ["submission_types", "allowed_extensions"]:
            if attr in item:
                value = list(item[attr])
                current = (
                    getattr(assignment, attr, None)
                    if assignment is not None
                    else None
                )

                if assignment is None or list_differs(value, current):
                    attrs[attr] = value

        if "assignment_group" in item:
            group_id = self.get_assignment_group_id(item["assignment_group"])

            if differs("assignment_group_id", group_id):
                attrs["assignment_group_id"] = group_id

        if "group_set" in item:
            category_id = self.get_group_category_id(item["group_set"])

            if differs("group_category_id", category_id):
                attrs["group_category_id"] = category_id

            individually = bool(
                item.get("grade_group_students_individually", False)
            )

            if differs("grade_group_students_individually", individually):
                attrs["grade_group_students_individually"] = individually

        if "peer_reviews" in item:
            peer_reviews = cast(ConfigDict, item["peer_reviews"] or {})
            automatic = bool(peer_reviews.get("automatic", False))

            wanted: ConfigDict = {
                "peer_reviews": True,
                "anonymous_peer_reviews": bool(
                    peer_reviews.get("anonymous", False)
                ),
                "automatic_peer_reviews": automatic,
            }

            if automatic and "count" in peer_reviews:
                wanted["peer_review_count"] = int(peer_reviews["count"])

            for attr, value in wanted.items():
                if differs(attr, value):
                    attrs[attr] = value

        return attrs

    def sync_assignment_item(self, item: ModuleItemConfig) -> Assignment:
        assignment = self.get_assignment(item["title"])

        name = item["assignment"]
        if not isinstance(name, str) or not name:
            name = item["title"]

        if assignment is None and name != item["title"]:
            assignment = self.get_assignment(name)

        if assignment is None:
            if not self.create_assignments:
                print(
                    f"[red]Cannot find assignment {item['title']:}[/red]",
                    file=sys.stderr,
                )
                sys.exit(1)

            attrs = self.assignment_settings(item)
            attrs["name"] = name
            attrs["published"] = item.get("published", False)

            logging.debug("Creating assignment '%s'", name)
            assignment = self.course.create_assignment(assignment=attrs)
        else:
            attrs = self.assignment_settings(item, assignment)

            if attrs:
                logging.debug(
                    "Updating assignment '%s' settings: %s",
                    name,
                    ", ".join(sorted(attrs)),
                )
                assignment.edit(assignment=attrs)

        if "description" in item:
            html = self.render_markdown(
                Path(item["description"]), template_vars=item.get("vars")
            )
        elif "description_contents" in item:
            html = self.render_markdown(
                cast(str, item["description_contents"]),
                template_vars=item.get("vars"),
            )
        else:
            html = None

        if html is not None and (
            getattr(assignment, "description", None) is None
            or not html_equiv(assignment.description, html)
        ):
            assignment.edit(assignment={"description": html})

        for date_attr in ["due_at", "lock_at", "unlock_at"]:
            if date_attr in item and date_differs(
                item[date_attr], getattr(assignment, date_attr, None)
            ):
                dt = parse_datetime(item[date_attr])
                assignment.edit(assignment={date_attr: dt.isoformat()})

        if "rubric" in item:
            self.sync_rubric(Path(item["rubric"]), assignment)

        return assignment

    def get_rubric_by_title(self, title: str) -> Rubric | None:
        """Find a course rubric with exactly this title."""
        for rubric in self.course.get_rubrics():
            if rubric.title == title:
                return rubric

        return None

    def sync_rubric(self, path: Path, assignment: Assignment) -> None:
        """Synchronize a YAML rubric and associate it with an assignment.

        A missing rubric is created and associated with the assignment for
        grading. An existing rubric is associated with the assignment if it
        isn't already. Rubric content is never updated because rubrics may
        have assessments; a warning is printed if it differs.

        Args:
            path (Path): Rubric YAML file, relative to the root.
            assignment (Assignment): Assignment to associate the rubric with.
        """
        rubric_config = load_rubric(self.root / path)
        title = rubric_config["title"]
        use_for_grading = bool(rubric_config.get("use_for_grading", False))

        association: ConfigDict = {
            "association_type": "Assignment",
            "association_id": assignment.id,
            "purpose": "grading",
            "use_for_grading": canvas_bool(use_for_grading),
            "hide_score_total": canvas_bool(False),
        }

        rubric = self.get_rubric_by_title(title)

        if rubric is None:
            logging.debug("Creating rubric '%s'", title)
            self.course.create_rubric(
                rubric=rubric_payload(rubric_config),
                rubric_association=association,
            )
            return

        # The rubric list may omit criteria; fetch the full rubric to compare.
        if getattr(rubric, "data", None) is None:
            rubric = self.course.get_rubric(rubric.id)

        differences = rubric_differences(rubric_config, rubric)
        if differences:
            details = "\n  ".join(escape(d) for d in differences[:3])
            print(
                f"[yellow]Rubric '{title:}' in Canvas differs from "
                f"'{path:}'; not updating it. Edit it in Canvas or give the "
                f"rubric a new title.\n  {details}[/yellow]"
            )

        rubric_settings = cast(
            ConfigDict, getattr(assignment, "rubric_settings", None) or {}
        )

        if rubric_settings.get("id") != rubric.id:
            logging.debug(
                "Associating rubric '%s' with assignment '%s'",
                title,
                assignment.name,
            )
            self.course.create_rubric_association(
                rubric_association={"rubric_id": rubric.id, **association}
            )

    def get_discussion_by_title(self, title: str) -> DiscussionTopic | None:
        """Find a course discussion topic with exactly this title."""
        for topic in self.course.get_discussion_topics(search_term=title):
            if topic.title == title:
                return topic

        return None

    def sync_discussion_item(self, item: ModuleItemConfig) -> DiscussionTopic:
        """Synchronize a discussion topic from a Markdown prompt.

        The topic is found by title (the item title) and created if missing.
        Its message (the rendered ``discussion:`` file) and settings are
        updated when they differ. Students' replies are never touched.

        Item keys: ``discussion`` (Markdown file), ``discussion_type``
        (``threaded``, the default, or ``side_comment``), ``unlock_at``
        (posted at), ``lock_at`` (closed for replies), ``pinned``,
        ``require_initial_post`` and ``published``.

        Args:
            item (ModuleItemConfig): Discussion module item.

        Returns:
            DiscussionTopic: The Canvas discussion topic.
        """
        title = item["title"]
        html = self.render_markdown(
            Path(item["discussion"]), template_vars=item.get("vars")
        )

        settings: ConfigDict = {
            "discussion_type": item.get("discussion_type", "threaded"),
            "published": bool(item.get("published", False)),
        }
        for flag in ["pinned", "require_initial_post"]:
            if flag in item:
                settings[flag] = bool(item[flag])

        dates = {"delayed_post_at": "unlock_at", "lock_at": "lock_at"}

        topic = self.get_discussion_by_title(title)

        if topic is None:
            logging.debug("Creating discussion '%s'", title)
            create: ConfigDict = {
                k: canvas_bool(v) if isinstance(v, bool) else v
                for k, v in settings.items()
            }
            for canvas_attr, item_attr in dates.items():
                if item_attr in item:
                    create[canvas_attr] = parse_datetime(
                        item[item_attr]
                    ).isoformat()

            return cast(
                DiscussionTopic,
                self.course.create_discussion_topic(
                    title=title, message=html, **create
                ),
            )

        changes: ConfigDict = {}

        current = getattr(topic, "message", None)
        if current is None or not html_equiv(current, html):
            changes["message"] = html

        for attr, value in settings.items():
            if getattr(topic, attr, None) != value:
                changes[attr] = (
                    canvas_bool(value) if isinstance(value, bool) else value
                )

        for canvas_attr, item_attr in dates.items():
            if item_attr in item and date_differs(
                item[item_attr], getattr(topic, canvas_attr, None)
            ):
                changes[canvas_attr] = parse_datetime(
                    item[item_attr]
                ).isoformat()

        if changes:
            logging.debug("Updating discussion '%s'", title)
            topic.update(**changes)

        return topic

    def get_quiz_by_title(self, title: str) -> Quiz | None:
        """Find a course quiz with exactly this title."""
        matches = [
            quiz
            for quiz in self.course.get_quizzes(search_term=title)
            if quiz.title == title
        ]

        if len(matches) == 0:
            return None

        if len(matches) > 1:
            print(
                f"[yellow]Multiple quizzes are titled '{title:}'; "
                f"using quiz {matches[0].id:}[/yellow]"
            )

        return matches[0]

    def _quiz_status(self, message: str) -> None:
        """Report quiz import progress."""
        logging.debug("%s", message)

        if self._status_callback is not None:
            self._status_callback(self._current_module or "", message)

    def upload_migration_file(
        self, pre_attachment: ConfigDict, filepath: Path
    ) -> None:
        """Upload a content migration file to Canvas.

        Implements Canvas's file upload flow for a migration's
        ``pre_attachment``: a multipart POST of the upload parameters and the
        file to the upload URL, followed by a confirmation GET if Canvas
        redirects.

        Args:
            pre_attachment (ConfigDict): The migration's ``pre_attachment``.
            filepath (Path): File to upload.
        """
        if "upload_url" not in pre_attachment:
            message = pre_attachment.get("message", pre_attachment)
            raise ValueError(f"Cannot upload {filepath:}: {message}")

        upload_url = cast(str, pre_attachment["upload_url"])
        upload_params = cast(
            ConfigDict, pre_attachment.get("upload_params", {})
        )

        logging.debug("Uploading '%s' to %s", filepath, upload_url)

        with filepath.open("rb") as f:
            response = requests.post(
                upload_url,
                data=upload_params,
                files={"file": (filepath.name, f)},
                allow_redirects=False,
                timeout=300,
            )

        if 300 <= response.status_code < 400:
            location = response.headers["Location"]
            logging.debug("Confirming upload at %s", location)
            # Only send the API token back to the Canvas host itself.
            same_host = (
                urlparse(location).netloc == urlparse(self.api_url).netloc
            )
            headers = (
                {"Authorization": f"Bearer {self.api_key:}"}
                if same_host
                else {}
            )
            response = requests.get(location, headers=headers, timeout=60)

        if not response.ok:
            raise ValueError(
                f"Upload of {filepath:} failed with status "
                f"{response.status_code:}: {response.text:}"
            )

    def import_qti(self, filepath: Path, title: str) -> Quiz:
        """Import a QTI zip into the course and return the new quiz.

        Args:
            filepath (Path): Path to the QTI zip.
            title (str): Quiz title, for progress messages.

        Returns:
            Quiz: The quiz created by the import.
        """
        if not filepath.exists():
            raise FileNotFoundError(f"File '{filepath:}' does not exist")

        before = {quiz.id for quiz in self.course.get_quizzes()}

        self._quiz_status(f"{title} (importing QTI)")
        migration = self.course.create_content_migration(
            migration_type="qti_converter",
            pre_attachment={
                "name": filepath.name,
                "size": filepath.stat().st_size,
            },
        )

        self.upload_migration_file(
            cast(ConfigDict, getattr(migration, "pre_attachment", None) or {}),
            filepath,
        )

        start = time.monotonic()
        state = getattr(migration, "workflow_state", None)

        while True:
            migration = self.course.get_content_migration(migration.id)
            state = migration.workflow_state

            if state in ("completed", "failed"):
                break

            elapsed = time.monotonic() - start
            if elapsed > self.migration_timeout:
                raise TimeoutError(
                    f"Import of {filepath:} did not finish within "
                    f"{self.migration_timeout:.0f}s (state: {state})"
                )

            self._quiz_status(
                f"{title} (importing QTI: {state}, {elapsed:.0f}s)"
            )
            time.sleep(self.migration_poll_interval)

        if state == "failed":
            raise ValueError(f"Import of {filepath:} failed")

        new_ids = [
            quiz.id
            for quiz in self.course.get_quizzes()
            if quiz.id not in before
        ]

        if len(new_ids) == 0:
            raise ValueError(f"Import of {filepath:} created no quiz")

        if len(new_ids) > 1:
            print(
                f"[yellow]Import of {filepath:} created {len(new_ids):} "
                f"quizzes; using quiz {new_ids[0]:}[/yellow]"
            )

        print(f"[green]Imported quiz '{title:}' from {filepath:}[/green]")

        return self.course.get_quiz(new_ids[0])

    def quiz_settings(
        self, item: ModuleItemConfig, quiz: Quiz | None = None
    ) -> ConfigDict:
        """Compute quiz settings from an item that differ from Canvas.

        Args:
            item (ModuleItemConfig): Quiz item.
            quiz (Optional[Quiz]): Existing quiz, or None for all settings.

        Returns:
            ConfigDict: Settings to send to Canvas.
        """

        def differs(attr: str, value: Any) -> bool:
            return quiz is None or getattr(quiz, attr, None) != value

        attrs: ConfigDict = {}

        if differs("title", item["title"]):
            attrs["title"] = item["title"]

        for attr in ["time_limit", "allowed_attempts", "shuffle_answers"]:
            if attr in item and differs(attr, item[attr]):
                attrs[attr] = item[attr]

        for date_attr in ["due_at", "unlock_at", "lock_at"]:
            if date_attr in item and (
                quiz is None
                or date_differs(
                    item[date_attr], getattr(quiz, date_attr, None)
                )
            ):
                attrs[date_attr] = parse_datetime(item[date_attr]).isoformat()

        if "assignment_group" in item:
            group_id = self.get_assignment_group_id(item["assignment_group"])

            if differs("assignment_group_id", group_id):
                attrs["assignment_group_id"] = group_id

        published = bool(item.get("published", False))
        if differs("published", published):
            attrs["published"] = published

        return attrs

    def sync_quiz_item(self, item: ModuleItemConfig) -> Quiz:
        """Synchronize a Classic Quiz imported from a QTI zip.

        Args:
            item (ModuleItemConfig): Quiz item.

        Returns:
            Quiz: The synchronized quiz.
        """
        title = item["title"]

        # A quiz may be synchronized twice when its module item is re-created
        if title in self._synced_quizzes:
            return self._synced_quizzes[title]

        filepath: Path = self.root / Path(item["quiz"])
        quiz = self.get_quiz_by_title(title)

        if quiz is not None and self.replace_quizzes:
            sha256 = file_sha256(filepath)
            stored = get_sha256_marker(getattr(quiz, "description", None))

            if stored != sha256:
                if list(quiz.get_submissions()):
                    print(
                        f"[red]Not replacing quiz '{title:}' because it has "
                        "student submissions[/red]",
                        file=sys.stderr,
                    )
                else:
                    logging.debug(
                        "Replacing quiz '%s' (stored sha256 %s)", title, stored
                    )
                    quiz.delete()
                    quiz = None

        if quiz is None:
            sha256 = file_sha256(filepath)
            quiz = self.import_qti(filepath, title)

            attrs = self.quiz_settings(item)
            attrs["description"] = set_sha256_marker(
                getattr(quiz, "description", None), sha256
            )
        else:
            attrs = self.quiz_settings(item, quiz)

        if attrs:
            logging.debug(
                "Updating quiz '%s' settings: %s",
                title,
                ", ".join(sorted(attrs)),
            )
            quiz = quiz.edit(quiz=attrs)

        self._synced_quizzes[title] = quiz

        return quiz
