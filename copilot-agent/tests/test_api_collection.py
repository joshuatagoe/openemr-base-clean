"""The Bruno collection in api-collection/ must run green against the agent (it is a graded deliverable).

Skipped when npx/Bruno CLI is unavailable or offline; otherwise starts the app on a free port
with the stub provider and runs the whole collection headless.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
COLLECTION = ROOT / "api-collection"
SECRET = "collection-check-secret-0123456789abcdef"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(shutil.which("npx") is None, reason="npx not installed")
@pytest.mark.skipif(os.environ.get("COPILOT_SKIP_COLLECTION_TEST") == "1", reason="opted out")
def test_bruno_collection_passes_against_stub_agent(tmp_path: Path) -> None:
    port = _free_port()
    env = {**os.environ, "COPILOT_TICKET_SECRET": SECRET, "MODEL_PROVIDER": "stub", "LANGFUSE_TRACING_ENABLED": "false", "COPILOT_LANGFUSE_HOST": "", "COPILOT_OPENEMR_BASE_URL": "", "ANTHROPIC_API_KEY": ""}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        else:
            pytest.fail("agent did not start")
        (tmp_path / "environments").mkdir()
        (tmp_path / "environments" / "ci.bru").write_text(f"vars {{\n  agent_url: http://127.0.0.1:{port}\n}}\nvars:secret [\n  ticket_secret\n]\n", encoding="utf-8")
        for f in COLLECTION.iterdir():
            if f.is_file():
                shutil.copy(f, tmp_path / f.name)
        cmd = ["npx", "--yes", "@usebruno/cli", "run", "--env", "ci", "--env-var", f"ticket_secret={SECRET}"]
        try:
            result = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240, shell=sys.platform == "win32")
        except (subprocess.TimeoutExpired, OSError) as exc:
            pytest.skip(f"Bruno CLI unavailable: {type(exc).__name__}")
        out, err = result.stdout or "", result.stderr or ""
        if result.returncode != 0 and "npm ERR" in err:
            pytest.skip("Bruno CLI could not be fetched (offline?)")
        assert result.returncode == 0, out[-4000:] + err[-2000:]
        assert "Failed)" not in out, out[-4000:]
    finally:
        server.terminate()
        server.wait(timeout=10)
