"""Workbench sandbox + executor — path safety, stream demux, tool payloads.

Pure-logic tests (no Docker): the Engine-API layer is not exercised here; the
sandbox client is driven through a fake to check the executor's payload shaping.
"""

import struct

import pytest

from core.workbench.sandbox import SandboxResult, WorkbenchSandbox, _demux, _truncate
from core.workbench.executor import WorkbenchExecutor


# ── _demux / _truncate ──────────────────────────────────────────────────────
def _frame(stream_type: int, payload: bytes) -> bytes:
    return bytes([stream_type, 0, 0, 0]) + struct.pack(">I", len(payload)) + payload


def test_demux_separates_stdout_and_stderr():
    raw = _frame(1, b"hello ") + _frame(2, b"ERR") + _frame(1, b"world")
    assert _demux(raw) == ("hello world", "ERR")


def test_demux_non_multiplexed_falls_back_to_stdout():
    # A payload whose first byte is not a valid stream type must not be parsed as
    # a frame header (regression: bytes were misread as a frame size).
    assert _demux(b"plain output") == ("plain output", "")


def test_demux_empty():
    assert _demux(b"") == ("", "")


def test_truncate_marks_when_over_limit():
    text, truncated = _truncate("x" * 100, 10)
    assert truncated is True
    assert text.startswith("x" * 10)
    assert _truncate("short", 100) == ("short", False)


# ── path safety ─────────────────────────────────────────────────────────────
@pytest.fixture
def sandbox():
    return WorkbenchSandbox(container="neo-workbench", workspace_root="/workspace")


def test_resolve_path_relative_and_absolute(sandbox):
    assert sandbox.resolve_path("a/b.py") == "/workspace/a/b.py"
    assert sandbox.resolve_path("/workspace/x") == "/workspace/x"
    assert sandbox.resolve_path("") == "/workspace"
    assert sandbox.resolve_path("./foo/../bar") == "/workspace/bar"


@pytest.mark.parametrize(
    "bad",
    ["../etc/passwd", "/etc/passwd", "/workspace/../secret", "a/../../b"],
)
def test_resolve_path_rejects_escape(sandbox, bad):
    with pytest.raises(ValueError):
        sandbox.resolve_path(bad)


# ── executor payloads (fake sandbox) ────────────────────────────────────────
class _FakeSandbox:
    def __init__(self, result=None):
        self.result = result or SandboxResult(exit_code=0, stdout="done", stderr="")
        self.calls = []

    def resolve_path(self, path):
        return f"/workspace/{(path or '').lstrip('/')}"

    async def run(self, command, *, cwd=None, timeout=None):
        self.calls.append(("run", command, cwd, timeout))
        return self.result

    async def write_file(self, path, content):
        self.calls.append(("write", path, content))
        return SandboxResult(exit_code=0, stdout="", stderr="")

    async def read_file(self, path, *, max_bytes=None):
        self.calls.append(("read", path))
        return SandboxResult(exit_code=0, stdout="file contents", stderr="")

    async def list_files(self, path=None):
        self.calls.append(("list", path))
        return SandboxResult(exit_code=0, stdout="total 0", stderr="")


@pytest.mark.asyncio
async def test_neo_bash_success_payload():
    fake = _FakeSandbox(SandboxResult(exit_code=0, stdout="42\n", stderr=""))
    ex = WorkbenchExecutor(fake)
    out = await ex.execute("neo_bash", {"command": "echo 42"})
    assert out["ok"] is True
    assert out["exit_code"] == 0
    assert out["stdout"] == "42\n"


@pytest.mark.asyncio
async def test_neo_bash_caps_timeout_to_foreground_max():
    fake = _FakeSandbox()
    ex = WorkbenchExecutor(fake)
    await ex.execute("neo_bash", {"command": "sleep 999", "timeout": 9999})
    # timeout arg is clamped to the foreground ceiling before hitting the sandbox.
    _, _, _, passed_timeout = fake.calls[0]
    assert passed_timeout == 120


@pytest.mark.asyncio
async def test_neo_bash_empty_command_errors():
    ex = WorkbenchExecutor(_FakeSandbox())
    out = await ex.execute("neo_bash", {"command": "   "})
    assert out["ok"] is False


@pytest.mark.asyncio
async def test_neo_bash_timeout_flag_maps_to_error():
    fake = _FakeSandbox(SandboxResult(exit_code=137, stdout="", stderr="", timed_out=True))
    ex = WorkbenchExecutor(fake)
    out = await ex.execute("neo_bash", {"command": "sleep 999"})
    assert out["ok"] is False
    assert out["timed_out"] is True


@pytest.mark.asyncio
async def test_write_read_list_roundtrip_shapes():
    fake = _FakeSandbox()
    ex = WorkbenchExecutor(fake)
    w = await ex.execute("neo_write_file", {"path": "x.py", "content": "print(1)"})
    assert w["ok"] is True and w["bytes"] == 8
    r = await ex.execute("neo_read_file", {"path": "x.py"})
    assert r["ok"] is True and r["content"] == "file contents"
    ls = await ex.execute("neo_list_files", {})
    assert ls["ok"] is True and "total" in ls["listing"]


@pytest.mark.asyncio
async def test_unavailable_sandbox_returns_clean_error():
    from core.workbench.sandbox import WorkbenchUnavailableError

    class _Down(_FakeSandbox):
        async def run(self, command, *, cwd=None, timeout=None):
            raise WorkbenchUnavailableError("socket off")

    ex = WorkbenchExecutor(_Down())
    out = await ex.execute("neo_bash", {"command": "echo hi"})
    assert out["ok"] is False
    assert out["unavailable"] is True
