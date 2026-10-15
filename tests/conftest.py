"""Shared fixtures for CanvasSync tests.

No test talks to Canvas: the ``canvas`` and ``course`` cached properties
of :class:`CanvasSync` are replaced with mocks.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from canvassync.sync import CanvasSync

MakeSync = Callable[..., CanvasSync]


@pytest.fixture
def make_sync(tmp_path: Path) -> MakeSync:
    """Return a factory for CanvasSync objects with a mocked course."""

    def factory(
        config: dict[str, Any] | None = None,
        course: MagicMock | None = None,
        **kwargs: Any,
    ) -> CanvasSync:
        cfg: dict[str, Any] = {
            "api_url": "https://canvas.example.edu",
            "api_key": "secret-token",
            "course_sis": "SIS 101",
            "modules": [],
        }
        cfg.update(config or {})

        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.safe_dump(cfg), encoding="utf8")

        sync = CanvasSync(config_path, **kwargs)
        sync.__dict__["canvas"] = MagicMock(name="canvas")
        if course is not None:
            sync.__dict__["course"] = course

        return sync

    return factory
