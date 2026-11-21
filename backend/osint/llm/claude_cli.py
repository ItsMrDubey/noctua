"""Local Claude Code CLI (``claude -p``) as a structured-extraction backend.

The instruction and JSON schema are passed as argv entries (never through a shell); the
article text goes in on stdin. Runs on the user's Claude plan, so callers must rate-limit.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

from osint.llm.base import LLMError, LLMResult, LLMTimeout

log = logging.getLogger(__name__)

# process-wide cap on concurrent ``claude`` processes (extraction + translation share it:
# each CLI process holds ~250-500 MB). A threading semaphore polled from asyncio, so it
# works across event loops and threads and is never leaked by a cancelled waiter.
MAX_CLI_PROCESSES = 2
_CLI_SLOTS = threading.BoundedSemaphore(MAX_CLI_PROCESSES)


@contextlib.asynccontextmanager
async def cli_slot() -> AsyncIterator[None]:
    while not _CLI_SLOTS.acquire(blocking=False):
        await asyncio.sleep(0.1)
    try:
        yield
    finally:
        _CLI_SLOTS.release()


async def kill_tree(proc) -> None:
    """Kill the CLI and its children: on Windows ``claude.exe`` spawns node workers that
    survive ``proc.kill()``, so ``taskkill /T /F`` (no shell) takes the whole tree."""
    if os.name == "nt" and getattr(proc, "pid", None):
        try:
            await asyncio.to_thread(
                subprocess.run, ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError):
            log.warning("taskkill of claude CLI pid %s failed", proc.pid, exc_info=True)
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass

# npm installs a claude.cmd shim next to node_modules; batch files must never receive
# untrusted argv (cmd.exe re-parses it), so we resolve the real executable behind it.
_SHIM_TARGETS = (
    Path("node_modules") / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe",
    Path("node_modules") / "@anthropic-ai" / "claude-code" / "bin" / "claude",
)


def resolve_cli(explicit: str | None = None) -> str:
    """Path of a directly executable ``claude`` binary (not a .cmd/.bat shim)."""
    cand = explicit or shutil.which("claude")
    if not cand:
        raise LLMError("claude CLI not found on PATH (set cli_path in config/extraction.yaml)")
    path = Path(cand)
    if path.suffix.lower() in (".cmd", ".bat", ".ps1", "") and os.name == "nt":
        for rel in _SHIM_TARGETS:
            real = path.parent / rel
            if real.is_file():
                return str(real)
        if path.suffix.lower() in (".cmd", ".bat", ".ps1"):
            raise LLMError(f"refusing to run batch shim {path}; set cli_path to claude.exe")
    return str(path)


def build_argv(exe: str, instruction: str, schema: dict, model: str) -> list[str]:
    return [
        exe, "-p", instruction,
        "--model", model,
        "--output-format", "json",
        "--json-schema", json.dumps(schema, separators=(",", ":")),
        "--tools", "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--setting-sources", "",
    ]


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.DOTALL)


def _loads_object(raw: str) -> dict | None:
    raw = _FENCE_RE.sub("", raw.strip())
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else None
    except ValueError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        try:
            val = json.loads(raw[start:end + 1])
            return val if isinstance(val, dict) else None
        except ValueError:
            return None
    return None


def parse_cli_output(stdout: str) -> tuple[dict, dict]:
    """(structured data, envelope) from ``--output-format json`` output.

    Handles both shapes: a ``structured_output`` object, or the JSON as a string in
    ``result`` (possibly fenced). Raises LLMError on errors or unparseable output."""
    envelope = _loads_object(stdout)
    if envelope is None:
        # stream noise before the object: take the last line that parses
        for line in reversed(stdout.strip().splitlines()):
            envelope = _loads_object(line)
            if envelope is not None:
                break
    if envelope is None:
        raise LLMError(f"unparseable CLI output: {stdout[:200]!r}")
    if envelope.get("is_error") or envelope.get("subtype", "success") != "success":
        msg = envelope.get("result") or envelope.get("subtype") or "error"
        raise LLMError(f"CLI reported error: {str(msg)[:300]}")
    data = envelope.get("structured_output")
    if not isinstance(data, dict):
        result = envelope.get("result")
        data = result if isinstance(result, dict) else (
            _loads_object(result) if isinstance(result, str) else None)
    if not isinstance(data, dict):
        raise LLMError("CLI output has no structured_output / JSON result")
    return data, envelope


def _model_name(envelope: dict, fallback: str) -> str:
    usage = envelope.get("modelUsage")
    if isinstance(usage, dict) and usage:
        return next(iter(usage))
    return fallback


class ClaudeCliBackend:
    name = "claude_cli"

    def __init__(self, model: str = "haiku", *, timeout_s: float = 90.0, concurrency: int = 2,
                 cli_path: str | None = None, cwd: Path | None = None,
                 spawn=None) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self.cli_path = cli_path
        self.cwd = cwd
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._spawn = spawn or asyncio.create_subprocess_exec
        self._exe: str | None = None

    def argv(self, instruction: str, schema: dict) -> list[str]:
        if self._exe is None:
            self._exe = resolve_cli(self.cli_path)
        return build_argv(self._exe, instruction, schema, self.model)

    async def extract(self, instruction: str, text: str, schema: dict) -> LLMResult:
        argv = self.argv(instruction, schema)
        async with self._sem, cli_slot():
            t0 = time.monotonic()
            if self.cwd is not None:
                self.cwd.mkdir(parents=True, exist_ok=True)
            proc = await self._spawn(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.cwd) if self.cwd is not None else None)
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(text.encode("utf-8")), self.timeout_s)
            except (TimeoutError, asyncio.CancelledError) as exc:
                await kill_tree(proc)
                try:
                    await asyncio.wait_for(proc.wait(), 10)
                except TimeoutError:
                    log.warning("claude CLI did not exit after kill")
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise LLMTimeout(f"claude CLI timed out after {self.timeout_s:.0f}s") from None
            latency = time.monotonic() - t0
        stdout = out.decode("utf-8", "replace")
        if proc.returncode != 0 and not stdout.strip():
            raise LLMError(f"claude CLI exit {proc.returncode}: "
                           f"{err.decode('utf-8', 'replace')[:300]}")
        data, env = parse_cli_output(stdout)
        cost = env.get("total_cost_usd")
        return LLMResult(
            data=data, cost_usd=float(cost) if isinstance(cost, (int, float)) else 0.0,
            model=_model_name(env, self.model), latency_s=latency,
            usage=env.get("usage") if isinstance(env.get("usage"), dict) else {})
