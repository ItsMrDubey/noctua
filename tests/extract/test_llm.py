import asyncio
import json
import os

import pytest

from osint.llm import (
    AlterLabByokBackend,
    AnthropicApiBackend,
    CallRateLimiter,
    ClaudeCliBackend,
    LLMError,
    LLMTimeout,
    make_backend,
)
from osint.llm.claude_cli import build_argv, parse_cli_output, resolve_cli

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}


def envelope(**kw) -> str:
    base = {"type": "result", "subtype": "success", "is_error": False,
            "total_cost_usd": 0.0012, "usage": {"input_tokens": 5, "output_tokens": 7},
            "modelUsage": {"claude-haiku-5-5": {"costUSD": 0.0012}}, "result": ""}
    base.update(kw)
    return json.dumps(base)


def test_build_argv_exact():
    argv = build_argv("/bin/claude", "do it", SCHEMA, "haiku")
    assert argv == [
        "/bin/claude", "-p", "do it", "--model", "haiku", "--output-format", "json",
        "--json-schema", ('{"type":"object","properties":{"a":{"type":"integer"}},'
                          '"required":["a"]}'),
        "--tools", "", "--strict-mcp-config", "--no-session-persistence",
        "--disable-slash-commands", "--setting-sources", ""]


def test_parse_structured_output_shape():
    data, env = parse_cli_output(envelope(structured_output={"a": 1}, result='{"a": 2}'))
    assert data == {"a": 1} and env["total_cost_usd"] == 0.0012


def test_parse_result_string_shape_and_fences():
    assert parse_cli_output(envelope(result='{"a": 2}'))[0] == {"a": 2}
    assert parse_cli_output(envelope(result='```json\n{"a": 3}\n```'))[0] == {"a": 3}
    assert parse_cli_output(envelope(result='Here you go: {"a": 4} done'))[0] == {"a": 4}
    # log noise before the envelope line
    assert parse_cli_output("warning: x\n" + envelope(result='{"a": 5}'))[0] == {"a": 5}


@pytest.mark.parametrize("out", [
    "not json", envelope(is_error=True, result="rate limited"),
    envelope(subtype="error_max_turns"), envelope(result="no json here")])
def test_parse_errors(out):
    with pytest.raises(LLMError):
        parse_cli_output(out)


class FakeProc:
    def __init__(self, stdout: bytes = b"", rc: int = 0, hang: bool = False,
                 delay: float = 0.0) -> None:
        self.stdout_bytes, self.returncode, self.hang, self.delay = stdout, rc, hang, delay
        self.killed = False
        self.stdin_data: bytes | None = None
        self._done = asyncio.Event()

    async def communicate(self, data: bytes):
        self.stdin_data = data
        if self.hang:
            await self._done.wait()
        await asyncio.sleep(self.delay)
        return self.stdout_bytes, b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._done.set()

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode


def fake_spawn(procs: list, calls: list):
    async def spawn(*argv, **kw):
        calls.append((argv, kw))
        return procs.pop(0) if len(procs) > 1 else procs[0]
    return spawn


async def test_cli_backend_exec_no_shell_text_on_stdin(tmp_path):
    calls: list = []
    proc = FakeProc(envelope(structured_output={"a": 1}).encode())
    be = ClaudeCliBackend("haiku", cli_path=str(tmp_path / "claude.exe"),
                          cwd=tmp_path / "cwd", spawn=fake_spawn([proc], calls))
    be._exe = str(tmp_path / "claude.exe")
    res = await be.extract("instr", "Article text é", SCHEMA)
    argv, kw = calls[0]
    assert list(argv) == build_argv(str(tmp_path / "claude.exe"), "instr", SCHEMA, "haiku")
    assert "shell" not in kw and kw["cwd"] == str(tmp_path / "cwd")
    assert proc.stdin_data == "Article text é".encode()
    assert res.data == {"a": 1} and res.cost_usd == 0.0012
    assert res.model == "claude-haiku-5-5" and res.usage["output_tokens"] == 7


async def test_cli_backend_timeout_kills_process(tmp_path):
    proc = FakeProc(hang=True)
    be = ClaudeCliBackend(timeout_s=0.05, spawn=fake_spawn([proc], []))
    be._exe = "claude"
    with pytest.raises(LLMTimeout):
        await be.extract("i", "t", SCHEMA)
    assert proc.killed


async def test_cli_backend_nonzero_exit_without_output():
    be = ClaudeCliBackend(spawn=fake_spawn([FakeProc(b"", rc=1)], []))
    be._exe = "claude"
    with pytest.raises(LLMError):
        await be.extract("i", "t", SCHEMA)


async def test_cli_backend_concurrency_limit():
    state = {"now": 0, "peak": 0}
    out = envelope(structured_output={"a": 1}).encode()

    async def spawn(*argv, **kw):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])

        class P(FakeProc):
            async def communicate(self, data):
                await asyncio.sleep(0.02)
                state["now"] -= 1
                return out, b""
        return P()

    be = ClaudeCliBackend(concurrency=2, spawn=spawn)
    be._exe = "claude"
    await asyncio.gather(*(be.extract("i", "t", SCHEMA) for _ in range(6)))
    assert state["peak"] == 2


@pytest.mark.skipif(os.name != "nt", reason="npm .cmd shim resolution is Windows-only")
def test_resolve_cli_skips_cmd_shim(tmp_path):
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off")
    with pytest.raises(LLMError):
        resolve_cli(str(shim))
    real = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"")
    assert resolve_cli(str(shim)) == str(real)


async def test_stub_backends_raise_clear_errors():
    for be in (AnthropicApiBackend(), AlterLabByokBackend()):
        with pytest.raises(NotImplementedError, match="claude_cli"):
            await be.extract("i", "t", SCHEMA)
    assert isinstance(make_backend("anthropic_api"), AnthropicApiBackend)
    with pytest.raises(ValueError):
        make_backend("nope")


def test_rate_limiter_hour_and_day_caps():
    t = {"now": 100_000.0}
    lim = CallRateLimiter(per_hour=2, per_day=3, clock=lambda: t["now"])
    assert lim.try_acquire() and lim.try_acquire()
    assert not lim.try_acquire()  # hour cap
    t["now"] += 3601
    assert lim.try_acquire()
    assert not lim.try_acquire()  # day cap (3)
    assert lim.counts() == {"hour": 1, "day": 3}
    t["now"] += 86400
    assert lim.try_acquire() and lim.counts() == {"hour": 1, "day": 1}


def test_rate_limiter_seeded_from_history():
    lim = CallRateLimiter(per_hour=2, per_day=10, clock=lambda: 100_000.0,
                          history=[99_000.0, 99_500.0, 1.0])
    assert lim.counts() == {"hour": 2, "day": 2}  # 1.0 is older than a day
    assert not lim.try_acquire()
