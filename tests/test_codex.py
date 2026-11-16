import asyncio
import json
import shutil

import pytest

from saysso.cache import Cache
from saysso.llm import Budget, CodexCLI, LLMError

SCHEMA = {"type": "object", "properties": {"results": {"type": "array"}}}


class FakeProc:
    def __init__(self, out_path, rc=0, err=b""):
        self.out_path, self.returncode, self._err, self.stdin_data = out_path, rc, err, None

    async def communicate(self, data=None):
        self.stdin_data = data
        if self.returncode == 0 and self.out_path:
            open(self.out_path, "w").write(json.dumps({"results": [{"claim_id": "c1", "verdict": "SUPPORTED"}]}))
        return b"", self._err

    def kill(self):
        pass


def make_llm(model="gpt-x"):
    return CodexCLI(model, Budget(5), Cache(enabled=False), None)


def test_codex_command_is_safe_and_uses_stdin(monkeypatch):
    seen = {}
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex")

    async def fake_exec(*args, **kw):
        if args[1:3] == ("login", "status"):
            return FakeProc(None, rc=0)
        seen["args"], seen["kw"] = list(args), kw
        proc = FakeProc(args[args.index("-o") + 1])
        seen["proc"] = proc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    out = asyncio.run(make_llm().complete_json("SYS", "<source>text</source>", "<claim>c</claim>", SCHEMA))
    a = seen["args"]
    assert a[:2] == ["/usr/bin/codex", "exec"]
    for flag in ("--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules", "--output-schema"):
        assert flag in a
    assert a[a.index("--sandbox") + 1] == "read-only"
    assert a[a.index("-m") + 1] == "gpt-x" and a[-1] == "-"  # prompt goes over stdin, not argv
    assert b"<claim>c</claim>" in seen["proc"].stdin_data and b"Do not run commands" in seen["proc"].stdin_data
    assert "shell" not in seen["kw"] and out["results"][0]["verdict"] == "SUPPORTED"


def test_codex_missing_binary_and_login_hint(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(LLMError, match="Codex CLI not found"):
        asyncio.run(make_llm().complete_json("S", "p", "s", SCHEMA))

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex")

    async def failing(*args, **kw):
        if args[1:3] == ("login", "status"):
            return FakeProc(None, rc=0)
        return FakeProc(args[args.index("-o") + 1], rc=1, err=b"Error: not logged in. Please authenticate.")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failing)
    with pytest.raises(LLMError, match="codex login"):
        asyncio.run(make_llm().complete_json("S", "p", "s", SCHEMA))


def test_codex_not_signed_in_fails_fast_without_running_exec(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex")
    calls = []

    async def fake_exec(*args, **kw):
        calls.append(args[1:3])
        return FakeProc(None, rc=1, err=b"Not logged in")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(LLMError, match="not signed in"):
        asyncio.run(make_llm().complete_json("S", "p", "s", SCHEMA))
    assert calls == [("login", "status")]  # exec never started


def test_codex_preflight_can_be_skipped_for_other_auth(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setenv("SAYSSO_CODEX_SKIP_PREFLIGHT", "1")
    calls = []

    async def fake_exec(*args, **kw):
        calls.append(args[1])
        return FakeProc(args[args.index("-o") + 1])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(make_llm().complete_json("S", "p", "s", SCHEMA))
    assert calls == ["exec"]
