"""The project metadata is the single source of the application version."""

import tomllib
from pathlib import Path

VERSION: str = tomllib.loads(Path(__file__).with_name("pyproject.toml").read_text())[
    "project"
]["version"]
