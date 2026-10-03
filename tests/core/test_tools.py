"""core.tools - finding the external tools and running them: a missing tool, a failure or a hang is reported as a
ToolError, never a crash, and the CPU time a tool used is counted."""
import os
import sys

import pytest

from core import strategies, tools


def test_missing_tools_are_reported_not_crashed(monkeypatch, tmp_path, samples):
    monkeypatch.setenv("STORAGEARCH_JXL_TOOLS_DIR", str(tmp_path / "empty"))
    for name in ("jpeg_xl", "bmp_jxl"):
        ok, reason = strategies.get(name).available()
        assert not ok and "cjxl" in reason
    (tmp_path / "w").mkdir()
    with pytest.raises(tools.ToolError):
        strategies.get("jpeg_xl").compress(samples["image.jpg"].path, str(tmp_path / "w"))


# --- where the tools are looked for ---------------------------------------------------------------------

def test_without_the_setting_the_tools_folder_is_next_to_the_code(monkeypatch):
    monkeypatch.delenv("STORAGEARCH_JXL_TOOLS_DIR", raising=False)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(tools.__file__)))
    assert tools.tools_dir() == os.path.join(repo_root, "tools")


def test_the_built_exe_looks_in_its_bundled_data(monkeypatch, tmp_path):
    monkeypatch.delenv("STORAGEARCH_JXL_TOOLS_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert tools.tools_dir() == os.path.join(str(tmp_path), "tools")


# --- running a tool -------------------------------------------------------------------------------------
# The "tool" is the Python interpreter itself, so each test runs a real process with a known behaviour. The base
# interpreter is used because a virtual environment's python.exe is a launcher that runs the real one as a child.

@pytest.fixture
def python_as_tool(monkeypatch):
    interpreter = getattr(sys, "_base_executable", None) or sys.executable
    monkeypatch.setattr(tools, "tool_path", lambda name: interpreter)


def test_found_tools_are_reported_available(monkeypatch, tmp_path):
    for name in ("cjxl.exe", "djxl.exe"):
        (tmp_path / name).write_bytes(b"")
    monkeypatch.setenv("STORAGEARCH_JXL_TOOLS_DIR", str(tmp_path))
    assert tools.available("cjxl", "djxl") == (True, "")


def test_a_tool_that_succeeds_returns_and_its_cpu_time_is_counted(python_as_tool):
    before = tools.cpu_seconds()
    proc = tools.run("py", ["-c", "sum(i * i for i in range(3_000_000))"])
    assert proc.returncode == 0
    assert tools.cpu_seconds() > before


def test_a_path_argument_is_made_absolute_so_it_cannot_read_as_an_option(python_as_tool, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = "import sys; open(sys.argv[2], 'w').write(sys.argv[1])"
    tools.run("py", ["-c", script, "-looks_like_an_option.jpg", "seen.txt"], paths=(2,))
    assert (tmp_path / "seen.txt").read_text() == str(tmp_path / "-looks_like_an_option.jpg")


def test_a_tool_that_fails_reports_its_exit_code_and_last_error_line(python_as_tool):
    with pytest.raises(tools.ToolError, match=r"py exited with 3: second line"):
        tools.run("py", ["-c", "import sys; sys.stderr.write('first line\\nsecond line\\n'); sys.exit(3)"])


def test_a_tool_that_fails_silently_still_reports_its_exit_code(python_as_tool):
    with pytest.raises(tools.ToolError, match=r"py exited with 2: $"):
        tools.run("py", ["-c", "import sys; sys.exit(2)"])


def test_a_tool_that_hangs_is_killed_at_its_timeout(python_as_tool):
    with pytest.raises(tools.ToolError, match=r"timed out after 0.5s"):
        tools.run("py", ["-c", "import time; time.sleep(60)"], timeout_s=0.5)


def test_a_file_that_is_not_a_program_cannot_start(monkeypatch, tmp_path):
    (tmp_path / "cjxl.exe").write_text("not a program", encoding="utf-8")
    monkeypatch.setenv("STORAGEARCH_JXL_TOOLS_DIR", str(tmp_path))
    with pytest.raises(tools.ToolError, match=r"cjxl could not start"):
        tools.run("cjxl", [])


# --- the CPU time of a process --------------------------------------------------------------------------

class _NoHandle:
    pass


class _ClosedHandle:
    _handle = 0x7FFFFFF0  # not an open process handle


def test_cpu_time_is_zero_when_it_cannot_be_read():
    assert tools._process_cpu_seconds(_NoHandle()) == 0.0
    assert tools._process_cpu_seconds(_ClosedHandle()) == 0.0
