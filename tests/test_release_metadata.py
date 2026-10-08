"""Release identity, strict SemVer, and monotonic Docker alias policy."""

import os
import re
import runpy
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from scripts import release_metadata


@pytest.mark.parametrize(
    "tag,prerelease",
    [
        ("v0.4.2", False),
        ("v1.20.300-alpha.0.rc-1", True),
        ("v1.2.3-0+001.build-x", True),
        ("v1.2.3+build-with-hyphens.001", False),
    ],
)
def test_strict_valid_semver(tag, prerelease):
    parsed = release_metadata.parse_tag(tag)
    assert parsed.version == tag[1:]
    assert parsed.prerelease is prerelease
    assert parsed.core == tuple(
        int(part) for part in tag[1:].split("-")[0].split("+")[0].split(".")
    )


@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",
        "V1.2.3",
        "v1.2",
        "v1.2.3.4",
        "v01.2.3",
        "v1.02.3",
        "v1.2.03",
        "v1.2.3-01",
        "v1.2.3-alpha.00",
        "v1.2.3-",
        "v1.2.3+",
        "v1.2.3-alpha..1",
        "v1.2.3+build..1",
        "v1.2.3+build+other",
        "v1.2.3_rc1",
        "v1.2.3-α",
        "v١.2.3",
        "v1.2.3\n",
        " v1.2.3",
    ],
)
def test_rejects_non_semver_tags(tag):
    with pytest.raises(ValueError, match="SemVer"):
        release_metadata.parse_tag(tag)


@pytest.mark.parametrize(
    "api,project",
    [
        ("0.4.1", "0.4.2"),
        ("0.4.2", "0.4.1"),
        ("v0.4.2", "0.4.2"),
        ("0.4.2", "0.4.2+build"),
    ],
)
def test_rejects_every_version_mismatch(api, project):
    with pytest.raises(ValueError, match="version mismatch"):
        release_metadata.generate_metadata("v0.4.2", api, project, [])


@pytest.mark.parametrize(
    "known,expected",
    [
        ([], (True, True, True)),
        (["v0.4.1", "v0.4.2", "v0.3.99"], (True, True, True)),
        (["v0.4.3"], (False, False, False)),
        (["v0.5.0"], (False, True, False)),
        (["v1.0.0"], (True, True, False)),
        (["v0.10.0"], (False, True, False)),
        (["v0.4.10"], (False, False, False)),
        (["v9.0.0-rc.1", "garbage", "v01.2.3", "1.0.0"], (True, True, True)),
        (["v0.4.2+other-build", "v0.3.9+build-with-hyphen"], (True, True, True)),
        (["v0.4.3+build-with-hyphen"], (False, False, False)),
    ],
)
def test_aliases_never_move_behind_newest_known_stable_tag(known, expected):
    metadata = release_metadata.generate_metadata("v0.4.2", "0.4.2", "0.4.2", known)
    assert metadata == {
        "version": "0.4.2",
        "immutable_tag": "0.4.2",
        "major_tag": "0",
        "minor_tag": "0.4",
        "major_enabled": str(expected[0]).lower(),
        "minor_enabled": str(expected[1]).lower(),
        "latest_enabled": str(expected[2]).lower(),
        "prerelease": "false",
    }


def test_prerelease_preserves_full_identity_without_aliases():
    version = "0.4.2-rc.1+build-42.001"
    metadata = release_metadata.generate_metadata(f"v{version}", version, version, [])
    assert metadata["immutable_tag"] == "0.4.2-rc.1_build-42.001"
    assert metadata["prerelease"] == "true"
    assert all(
        metadata[key] == "false"
        for key in ("major_enabled", "minor_enabled", "latest_enabled")
    )


def test_build_hyphen_is_not_a_prerelease():
    version = "0.4.2+build-with-hyphens"
    metadata = release_metadata.generate_metadata(f"v{version}", version, version, [])
    assert metadata["immutable_tag"] == "0.4.2_build-with-hyphens"
    assert metadata["prerelease"] == "false"
    assert metadata["latest_enabled"] == "true"


def test_docker_tag_length_limit():
    version = "0.4.2+" + "a" * 123
    with pytest.raises(ValueError, match="Docker tag"):
        release_metadata.generate_metadata(f"v{version}", version, version, [])


def test_main_outputs_metadata_and_reads_project_and_git(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.4.2"\n')
    monkeypatch.setenv("GITHUB_REF_NAME", "v0.4.2")
    monkeypatch.setattr(release_metadata, "VERSION", "0.4.2")

    def git_tags(command, **kwargs):
        assert command == ["git", "tag", "--list"]
        assert kwargs == {
            "check": True,
            "capture_output": True,
            "text": True,
            "shell": False,
        }
        return subprocess.CompletedProcess(command, 0, "v0.4.1\nv0.5.0\n", "")

    monkeypatch.setattr(subprocess, "run", git_tags)
    release_metadata.main()
    assert capsys.readouterr().out == (
        "version=0.4.2\nimmutable_tag=0.4.2\nmajor_tag=0\nminor_tag=0.4\n"
        "major_enabled=false\nminor_enabled=true\nlatest_enabled=false\n"
        "prerelease=false\n"
    )


def test_main_fails_without_writing_outputs(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.4.2"\n')
    monkeypatch.setenv("GITHUB_REF_NAME", "v0.4.1")
    monkeypatch.setattr(release_metadata, "VERSION", "0.4.2")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", "")
    )
    with pytest.raises(ValueError, match="version mismatch"):
        release_metadata.main()
    assert capsys.readouterr().out == ""


def test_module_entrypoint_from_repository_root(monkeypatch, capsys):
    import version

    monkeypatch.setenv("GITHUB_REF_NAME", f"v{version.VERSION}")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", "")
    )
    runpy.run_path(str(Path(release_metadata.__file__)), run_name="__main__")
    assert f"version={version.VERSION}\n" in capsys.readouterr().out


def publishing_job(name):
    workflow = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath(".github/workflows/publish-docker.yml")
        .read_text()
    )
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z]+:|\Z)", workflow, re.M | re.S)
    assert match is not None, f"Missing publishing job: {name}"
    return match[1]


def test_publication_cannot_update_moving_aliases_before_smoke_tests():
    publish = publishing_job("publish")
    assert "type=raw,value=${{ needs.metadata.outputs.immutable_tag }}" in publish
    assert all(
        key not in publish
        for key in ("major_tag", "minor_tag", "latest_enabled", "value=latest")
    )
    assert "imagetools create" not in publish


def test_promotion_and_release_require_successful_native_verification():
    promote = publishing_job("promote")
    assert "needs: [metadata, publish, verify]" in promote
    assert "    if:" not in promote
    assert "always()" not in promote
    assert "needs: [metadata, publish, verify, promote]" in publishing_job("release")
    verify = publishing_job("verify")
    assert "platform: linux/amd64" in verify
    assert "platform: linux/arm64" in verify
    assert "${{ needs.publish.outputs.digest }}" in verify


@pytest.mark.parametrize(
    "flags,aliases",
    [
        (("false", "false", "false"), []),
        (("true", "true", "true"), ["0", "0.4", "latest"]),
        (("false", "true", "false"), ["0.4"]),
        (("true", "true", "false"), ["0", "0.4"]),
    ],
)
def test_promotion_shell_copies_only_enabled_aliases_of_verified_index(
    flags, aliases, tmp_path
):
    script = dedent(publishing_job("promote").split("        run: |\n", 1)[1])
    assert "${{" not in script
    digest = "sha256:" + "a" * 64
    images = ["ghcr.io/example/wireguard-api", "example/wireguard-api"]
    result = subprocess.run(
        [
            "bash",
            "-e",
            "-o",
            "pipefail",
            "-c",
            "docker() { printf '%s\\n' \"$*\"; }\n" + script,
        ],
        env={
            **os.environ,
            "DIGEST": digest,
            "GHCR_IMAGE": images[0],
            "HUB_IMAGE": images[1],
            "MAJOR_TAG": "0",
            "MINOR_TAG": "0.4",
            "MAJOR_ENABLED": flags[0],
            "MINOR_ENABLED": flags[1],
            "LATEST_ENABLED": flags[2],
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        },
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    assert result.stdout.splitlines() == [
        f"buildx imagetools create --tag {image}:{alias} {image}@{digest}"
        for image in images
        for alias in aliases
    ]
    summary = (tmp_path / "summary").read_text()
    if aliases:
        assert all(
            f"{image}:{alias}" in summary for image in images for alias in aliases
        )
        assert digest in summary
    else:
        assert "No moving aliases" in summary


def test_promotion_shell_stops_on_registry_failure(tmp_path):
    script = dedent(publishing_job("promote").split("        run: |\n", 1)[1])
    result = subprocess.run(
        [
            "bash",
            "-e",
            "-o",
            "pipefail",
            "-c",
            "docker() { printf '%s\\n' \"$*\"; return 1; }\n" + script,
        ],
        env={
            **os.environ,
            "DIGEST": "sha256:" + "a" * 64,
            "GHCR_IMAGE": "ghcr.io/example/wireguard-api",
            "HUB_IMAGE": "example/wireguard-api",
            "MAJOR_TAG": "0",
            "MINOR_TAG": "0.4",
            "MAJOR_ENABLED": "true",
            "MINOR_ENABLED": "true",
            "LATEST_ENABLED": "true",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        },
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    assert result.returncode == 1
    assert len(result.stdout.splitlines()) == 1
