"""One version number everywhere: the core, what the cloud client reports, the details inside the exe file and
the build's pinned dependencies all come from, or agree with, core.CORE_VERSION."""
import os
import re

import core
from local_cloud import build_channel

REPO = os.path.dirname(os.path.dirname(os.path.abspath(core.__file__)))


def _read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return f.read()


def test_the_version_is_a_plain_release_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+", core.CORE_VERSION), "a release has no -beta or -dev suffix"


def test_the_cloud_client_reports_the_same_version():
    assert build_channel.APP_VERSION == core.CORE_VERSION == build_channel.BUILD_VERSION


def test_the_exe_details_are_built_from_the_same_number():
    spec = _read("StorageArch.spec")
    assert "CORE_VERSION" in spec and "version=version_info" in spec
    assert not re.search(r"\d+\.\d+\.\d+", spec.split("version_info = ")[1]), "the spec must not carry a number of its own"


def test_the_workflows_install_only_the_pinned_dependencies():
    lock = _read("requirements-lock.txt")
    pinned = re.findall(r"^([A-Za-z0-9_.-]+)==[^ ]+ \\\n    --hash=sha256:[0-9a-f]{64}$", lock, re.MULTILINE)
    assert len(pinned) >= 20 and len(pinned) == sum(1 for line in lock.splitlines() if "==" in line)
    names = {name.lower().replace("_", "-") for name in pinned}
    for requirements in ("requirements.txt", "requirements-dev.txt"):  # everything asked for is pinned
        for line in _read(requirements).splitlines():
            wanted = re.match(r"([A-Za-z0-9_.-]+)\s*>=", line)
            if wanted:
                assert wanted.group(1).lower().replace("_", "-") in names, line
    for workflow in ("tests.yml", "release.yml"):
        text = _read(".github", "workflows", workflow)
        installs = [line.strip() for line in text.splitlines() if "pip install" in line]
        assert installs == ["run: python -m pip install --require-hashes -r requirements-lock.txt"], (workflow, installs)
        for action in re.findall(r"uses: (\S+)", text):  # every action is pinned to a commit, not to a tag that can move
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action), (workflow, action)
