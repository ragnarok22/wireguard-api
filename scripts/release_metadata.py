"""Validate release identity and emit monotonic Docker alias metadata.

Run from the repository root with Python 3.11+:
    python -m scripts.release_metadata >> "$GITHUB_OUTPUT"

Aliases follow the newest known stable Git tag, including unpublished tags.
SemVer build metadata does not affect precedence. Docker tags encode '+' as '_'
to preserve the complete version identity in Docker's restricted tag alphabet.
"""

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import tomllib

from version import VERSION

_NUMBER = r"(?:0|[1-9][0-9]*)"
_PRERELEASE_ID = rf"(?:{_NUMBER}|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
_BUILD_ID = r"[0-9A-Za-z-]+"
_SEMVER_TAG = re.compile(
    rf"v({_NUMBER})\.({_NUMBER})\.({_NUMBER})"
    rf"(?:-({_PRERELEASE_ID}(?:\.{_PRERELEASE_ID})*))?"
    rf"(?:\+({_BUILD_ID}(?:\.{_BUILD_ID})*))?"
)


@dataclass(frozen=True)
class SemVer:
    version: str
    core: tuple[int, int, int]
    prerelease: bool


def parse_tag(tag: str) -> SemVer:
    """Accept exactly vMAJOR.MINOR.PATCH with SemVer prerelease/build suffixes."""
    match = _SEMVER_TAG.fullmatch(tag)
    if match is None:
        raise ValueError(f"Invalid SemVer release tag: {tag!r}")
    return SemVer(
        version=tag[1:],
        core=(int(match[1]), int(match[2]), int(match[3])),
        prerelease=match[4] is not None,
    )


def generate_metadata(
    tag: str, api_version: str, project_version: str, known_tags: list[str]
) -> dict[str, str]:
    candidate = parse_tag(tag)
    if candidate.version != api_version or candidate.version != project_version:
        raise ValueError(
            f"Release version mismatch: tag={candidate.version!r}, "
            f"version.VERSION={api_version!r}, project.version={project_version!r}"
        )
    immutable_tag = candidate.version.replace("+", "_")
    if len(immutable_tag) > 128:
        raise ValueError("Release version exceeds the 128-character Docker tag limit")

    stable_versions = [candidate.core]
    for known_tag in known_tags:
        try:
            known = parse_tag(known_tag)
        except ValueError:
            continue
        if not known.prerelease:
            stable_versions.append(known.core)

    major, minor, _ = candidate.core
    major_enabled = not candidate.prerelease and candidate.core == max(
        core for core in stable_versions if core[0] == major
    )
    minor_enabled = not candidate.prerelease and candidate.core == max(
        core for core in stable_versions if core[:2] == (major, minor)
    )
    latest_enabled = not candidate.prerelease and candidate.core == max(stable_versions)
    return {
        "version": candidate.version,
        "immutable_tag": immutable_tag,
        "major_tag": str(major),
        "minor_tag": f"{major}.{minor}",
        "major_enabled": str(major_enabled).lower(),
        "minor_enabled": str(minor_enabled).lower(),
        "latest_enabled": str(latest_enabled).lower(),
        "prerelease": str(candidate.prerelease).lower(),
    }


def main() -> None:
    project_version = tomllib.loads(Path("pyproject.toml").read_text())["project"][
        "version"
    ]
    tags = subprocess.run(
        ["git", "tag", "--list"],
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    ).stdout.splitlines()
    metadata = generate_metadata(
        os.environ["GITHUB_REF_NAME"], VERSION, project_version, tags
    )
    print("\n".join(f"{key}={value}" for key, value in metadata.items()))


if __name__ == "__main__":
    main()
