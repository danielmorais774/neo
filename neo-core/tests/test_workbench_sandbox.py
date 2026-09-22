"""Workbench sandbox + executor — path safety, stream demux, tool payloads, stall watch.

Pure-logic tests (no Docker): the Engine-API layer is not exercised here; the
sandbox client is driven through a fake to check the executor's payload shaping,
and `_exec`/`_probe` are stubbed to check how `run` tells a stall, the wall
ceiling and an out-of-memory kill apart.
"""

import asyncio
import struct

import pytest

from core.liveness import TurnLiveness, bind_liveness
from core.workbench.executor import (
    _FOREGROUND_CEILING,
    _STALL_DEFAULT,
    _STALL_MAX,
    _STALL_MIN,
    WorkbenchExecutor,
)
from core.workbench.sandbox import (
    SandboxResult,
    WorkbenchSandbox,
    _ProgressMonitor,
    _demux,
    _truncate,
)


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


# ── progress monitor (stall ≠ slow) ─────────────────────────────────────────
class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


_SAMPLE = (2, 100, 5000)


def test_monitor_changing_sample_is_progress_and_never_kills():
    # The c4f3f7de ffmpeg: CPU and bytes keep moving for minutes.
    clock = _Clock()
    m = _ProgressMonitor(None, "id", stall_timeout=90, clock=clock)
    for i in range(20):
        clock.now += 60
        assert m.observe((2, 100 + i, 5000 + i)) is False


def test_monitor_kills_only_after_the_stall_window_without_change():
    clock = _Clock()
    m = _ProgressMonitor(None, "id", stall_timeout=90, clock=clock)
    clock.now = 5
    assert m.observe(_SAMPLE) is False  # first sample counts as progress
    clock.now = 60
    assert m.observe(_SAMPLE) is False  # still for 55s
    clock.now = 95
    assert m.observe(_SAMPLE) is True  # still for 90s


def test_monitor_output_resets_the_stall_clock():
    clock = _Clock()
    m = _ProgressMonitor(None, "id", stall_timeout=90, clock=clock)
    clock.now = 5
    m.observe(_SAMPLE)
    clock.now = 80
    m.saw_output()
    clock.now = 120
    assert m.observe(_SAMPLE) is False
    clock.now = 170
    assert m.observe(_SAMPLE) is True


def test_monitor_failed_probe_is_no_evidence_either_way():
    clock = _Clock()
    m = _ProgressMonitor(None, "id", stall_timeout=90, clock=clock)
    clock.now = 5
    m.observe(_SAMPLE)
    clock.now = 500
    assert m.observe(None) is False


def test_monitor_without_stall_timeout_never_kills():
    clock = _Clock()
    m = _ProgressMonitor(None, "id", stall_timeout=None, clock=clock)
    m.observe(_SAMPLE)
    clock.now = 10_000
    assert m.observe(_SAMPLE) is False


@pytest.mark.asyncio
async def test_monitor_progress_feeds_the_turn_liveness():
    clock = _Clock()
    liveness = TurnLiveness(clock=clock)
    bind_liveness(liveness)
    m = _ProgressMonitor(None, "id", stall_timeout=90, clock=clock)
    clock.now = 50
    assert liveness.idle_seconds() == 50
    m.observe(_SAMPLE)
    assert liveness.idle_seconds() == 0


@pytest.mark.asyncio
async def test_watch_kills_a_stalled_tree():
    clock = _Clock()

    class _ProbeSandbox:
        def __init__(self):
            self.kills = []

        async def _probe(self, marker, *, kill=False):
            self.kills.append(kill)
            clock.now += 60
            return _SAMPLE

    sb = _ProbeSandbox()
    m = _ProgressMonitor(sb, "id", stall_timeout=90, clock=clock, interval=0)
    await asyncio.wait_for(m.watch(), timeout=1)
    assert m.stalled is True
    # sample (progress) → sample (still 60s) → sample (still 120s) → kill
    assert sb.kills == [False, False, False, True]


# ── run(): how a kill is classified ─────────────────────────────────────────
class _StubExec(WorkbenchSandbox):
    def __init__(self, result, *, stall=False, delay=0.0):
        super().__init__(container="neo-workbench", workspace_root="/workspace")
        self._result = result
        self._stall = stall
        self._delay = delay
        self.seen = {}

    async def _exec(self, cmd, *, workdir, env=None, wall_timeout=90, monitor=None):
        self.seen = {"cmd": cmd, "env": env, "monitor": monitor}
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._stall and monitor is not None:
            monitor.stalled = True
        return SandboxResult(
            exit_code=self._result.exit_code,
            stdout=self._result.stdout,
            stderr=self._result.stderr,
        )


def _killed(stdout=""):
    return SandboxResult(exit_code=137, stdout=stdout, stderr="")


@pytest.mark.asyncio
async def test_run_marks_the_process_tree_for_the_probe():
    sb = _StubExec(SandboxResult(exit_code=0, stdout="ok", stderr=""))
    await sb.run("echo ok", stall_timeout=30)
    assert sb.seen["cmd"][:3] == ["timeout", "--signal=KILL", "60"]
    assert len(sb.seen["env"]["NEO_EXEC_ID"]) == 32


@pytest.mark.asyncio
async def test_run_stall_kill_is_flagged_even_with_partial_output():
    sb = _StubExec(_killed(stdout="frame=  55 fps=0.5"), stall=True)
    res = await sb.run("ffmpeg ...", timeout=900, stall_timeout=90)
    assert res.timed_out is True
    assert res.stalled is True


@pytest.mark.asyncio
async def test_run_quick_sigkill_without_output_is_not_a_timeout():
    # 137 long before the ceiling = the OOM killer, not our clock.
    sb = _StubExec(_killed())
    res = await sb.run("python3 big.py", timeout=900, stall_timeout=90)
    assert res.timed_out is False
    assert res.stalled is False


@pytest.mark.asyncio
async def test_run_wall_ceiling_is_detected_by_the_clock():
    sb = _StubExec(_killed(stdout="frame= 112"), delay=1.05)
    res = await sb.run("ffmpeg ...", timeout=1, stall_timeout=90)
    assert res.timed_out is True
    assert res.stalled is False


@pytest.mark.asyncio
async def test_run_outside_a_turn_without_stall_does_not_probe():
    # Background jobs: nobody waits on a heartbeat and nothing asked for stall kills.
    sb = _StubExec(SandboxResult(exit_code=0, stdout="", stderr=""))
    await sb.run("make", timeout=1800)
    assert sb.seen["monitor"] is None


@pytest.mark.asyncio
async def test_run_inside_a_turn_probes_for_liveness_without_killing():
    bind_liveness(TurnLiveness())
    sb = _StubExec(SandboxResult(exit_code=0, stdout="", stderr=""))
    await sb.run("cp big.mov /workspace/files/", timeout=300)
    monitor = sb.seen["monitor"]
    assert monitor is not None
    assert monitor._stall_timeout is None


@pytest.mark.asyncio
async def test_cancelled_run_kills_the_process_tree():
    # Stop on the desktop cancels the turn; the exec must not outlive it.
    class _Hanging(WorkbenchSandbox):
        def __init__(self):
            super().__init__()
            self.marker = None
            self.killed = []
            self.started = asyncio.Event()

        async def _exec(self, cmd, *, workdir, env=None, wall_timeout=90, monitor=None):
            self.marker = env["NEO_EXEC_ID"]
            self.started.set()
            await asyncio.sleep(3600)

        async def _probe(self, marker, *, kill=False):
            self.killed.append((marker, kill))
            return (0, 0, 0)

    sb = _Hanging()
    task = asyncio.create_task(sb.run("python3 ocr_frames.py", timeout=900, stall_timeout=300))
    await sb.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sb.killed == [(sb.marker, True)]


@pytest.mark.asyncio
async def test_probe_parses_the_sample_and_swallows_failures():
    class _S(WorkbenchSandbox):
        def __init__(self, outcome):
            super().__init__()
            self.outcome = outcome
            self.cmds = []

        async def _exec(self, cmd, *, workdir, env=None, wall_timeout=90, monitor=None):
            self.cmds.append(cmd)
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return SandboxResult(exit_code=0, stdout=self.outcome, stderr="")

    ok = _S("3 120 4096\n")
    assert await ok._probe("abc") == (3, 120, 4096)
    assert ok.cmds[0][0] == "python3" and ok.cmds[0][-1] == "sample"
    assert await _S("")._probe("abc") is None
    assert await _S(RuntimeError("boom"))._probe("abc") is None
    killer = _S("0 0 0")
    await killer._probe("abc", kill=True)
    assert killer.cmds[0][-1] == "kill"


# ── executor payloads (fake sandbox) ────────────────────────────────────────
class _FakeSandbox:
    def __init__(self, result=None):
        self.result = result or SandboxResult(exit_code=0, stdout="done", stderr="")
        self.calls = []

    def resolve_path(self, path):
        return f"/workspace/{(path or '').lstrip('/')}"

    async def run(self, command, *, cwd=None, timeout=None, stall_timeout=None):
        self.calls.append(("run", command, cwd, timeout, stall_timeout))
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
@pytest.mark.parametrize(
    "arg,expected",
    [(None, _STALL_DEFAULT), (120, 120), (10, _STALL_MIN), (9999, _STALL_MAX), ("abc", _STALL_DEFAULT)],
)
async def test_neo_bash_timeout_arg_is_a_stall_window_not_a_wall_clock(arg, expected):
    fake = _FakeSandbox()
    ex = WorkbenchExecutor(fake)
    args = {"command": "ffmpeg -i x.mov frames/%04d.jpg"}
    if arg is not None:
        args["timeout"] = arg
    await ex.execute("neo_bash", args)
    _, _, _, wall, stall = fake.calls[0]
    assert stall == expected
    assert wall == _FOREGROUND_CEILING


def test_neo_bash_schema_timeout_outlives_the_sandbox_ceiling():
    from core.workbench.tools import WORKBENCH_TOOLS

    schema = next(t for t in WORKBENCH_TOOLS if t.name == "neo_bash")
    # The sandbox exec gets ceiling + 10s; the global wait_for must never win.
    assert schema.timeout > _FOREGROUND_CEILING + 10


@pytest.mark.asyncio
async def test_neo_bash_empty_command_errors():
    ex = WorkbenchExecutor(_FakeSandbox())
    out = await ex.execute("neo_bash", {"command": "   "})
    assert out["ok"] is False


@pytest.mark.asyncio
async def test_neo_bash_stall_maps_to_stuck_error():
    fake = _FakeSandbox(
        SandboxResult(exit_code=137, stdout="", stderr="", timed_out=True, stalled=True)
    )
    out = await WorkbenchExecutor(fake).execute("neo_bash", {"command": "sleep 999"})
    assert out["ok"] is False
    assert out["timed_out"] is True
    assert out["stalled"] is True
    assert "sem nenhum progresso" in out["error"]


@pytest.mark.asyncio
async def test_neo_bash_ceiling_maps_to_background_hint():
    fake = _FakeSandbox(SandboxResult(exit_code=137, stdout="", stderr="", timed_out=True))
    out = await WorkbenchExecutor(fake).execute("neo_bash", {"command": "sleep 999"})
    assert out["ok"] is False
    assert out["timed_out"] is True
    assert "stalled" not in out
    assert "neo_bash_background" in out["error"]


@pytest.mark.asyncio
async def test_neo_bash_early_sigkill_maps_to_memory_hint():
    fake = _FakeSandbox(SandboxResult(exit_code=137, stdout="", stderr=""))
    out = await WorkbenchExecutor(fake).execute("neo_bash", {"command": "python3 big.py"})
    assert out["ok"] is False
    assert "timed_out" not in out
    assert "memória" in out["error"]


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
        async def run(self, command, *, cwd=None, timeout=None, stall_timeout=None):
            raise WorkbenchUnavailableError("socket off")

    ex = WorkbenchExecutor(_Down())
    out = await ex.execute("neo_bash", {"command": "echo hi"})
    assert out["ok"] is False
    assert out["unavailable"] is True


# ── leitura de arquivo que corta avisa que cortou (session-1fcaa440) ────────
@pytest.mark.asyncio
async def test_read_file_reports_truncation_and_real_size():
    class _S(WorkbenchSandbox):
        async def _exec(self, cmd, *, workdir, env=None, wall_timeout=90, monitor=None):
            return SandboxResult(exit_code=0, stdout="64452\n" + "x" * 100, stderr="")

    res = await _S().read_file("ocr/transcricao.txt", max_bytes=100)
    assert res.truncated is True
    assert res.size_bytes == 64452
    assert res.stdout == "x" * 100


@pytest.mark.asyncio
async def test_neo_read_file_tells_the_model_it_only_got_part():
    class _Parcial(_FakeSandbox):
        async def read_file(self, path, *, max_bytes=None):
            return SandboxResult(exit_code=0, stdout="abc", stderr="", truncated=True, size_bytes=64452)

    out = await WorkbenchExecutor(_Parcial()).execute("neo_read_file", {"path": "ocr/transcricao.txt"})
    assert out["truncated"] is True and out["size_bytes"] == 64452
    assert "NÃO trate isto como o arquivo inteiro" in out["note"]
