import asyncio
import json

import pytest

from saysso.mcp_server import compact, verify_report_impl


def test_compact_summary_only_failures(server, monkeypatch):
    monkeypatch.setenv("SAYSSO_CACHE_DIR", "/tmp/cp-mcp-test")
    monkeypatch.delenv("SAYSSO_MODEL", raising=False)
    md = (f"The tower is 330 metres tall and was completed in 1889 [1]. The tower is 512 metres tall [1].\n\n"
          f"## Sources\n1. Tower {server}/tower\n")
    from saysso import config as cfgmod
    orig = cfgmod.Config.from_env.__func__

    def patched(cls, **kw):
        kw.setdefault("allow_private", True)
        kw.setdefault("wayback", False)
        kw.setdefault("retraction_check", False)
        return orig(cls, **kw)

    monkeypatch.setattr(cfgmod.Config, "from_env", classmethod(patched))
    out = asyncio.run(verify_report_impl(md))
    assert out["summary"]["claims"] == 2 and out["summary"]["passing"] == 1
    assert [i["verdict"] for i in out["issues"]] == ["CONTRADICTED"]
    assert "512" in out["issues"][0]["claim"] and out["issues"][0]["quote"]
    assert len(json.dumps(out)) < 2500  # compact on purpose


def test_server_registers_three_tools():
    pytest.importorskip("mcp")
    from saysso.mcp_server import build_server

    server = build_server()
    tools = asyncio.run(server.list_tools())
    assert sorted(t.name for t in tools) == ["check_claim", "locate_quote", "verify_report"]


def test_mcp_command_without_the_sdk_fails_politely(monkeypatch, capsys):
    import sys

    from saysso.cli import main

    monkeypatch.setitem(sys.modules, "mcp", None)  # makes `import mcp` raise ImportError
    assert main(["mcp"]) == 2
    err = capsys.readouterr().err
    assert "saysso[mcp]" in err and "Traceback" not in err
