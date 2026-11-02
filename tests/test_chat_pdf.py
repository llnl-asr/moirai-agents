"""The chat_pdf capability: a snapshot becomes a whole, styled PDF, and the validator catches what is not one."""
import shutil
import subprocess

import pytest

from moirai_agents.core import capstore as S
from moirai_agents.webui import runtime as R

CHROME = any(map(shutil.which, ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")))
LINES = "\n".join(f"line {i} of the output" for i in range(400))
SNAP = f"""<div class="msg assistant"><div class="who">moirai-agents</div><div class="bubble"><details class="tool" open><summary><span class="nm">Bash</span>
<span class="ds">run it</span></summary><div class="tin res"><div class="k">output</div><pre>{LINES}</pre></div></details>
<span class="pfull">FULL_SUBAGENT_TEXT</span><script>document.title='ran'</script></div></div>"""


def call(tool, **a):
    return S.call("chat_pdf", tool, **a)


def test_the_validator_rejects_what_is_not_a_whole_pdf(tmp_path):
    bad = tmp_path / "x.pdf"
    bad.write_bytes(b"%PDF-1.4 cut off")
    assert call("chat_pdf_check", pdf=str(bad))["status"] == "error"
    assert call("chat_pdf_check", pdf=str(tmp_path / "none.pdf"))["status"] == "error"


def test_a_missing_chrome_is_named(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("MOIRAI_AGENTS_CHROME", raising=False)
    snap = tmp_path / "s.html"
    snap.write_text(SNAP)
    r = call("chat_pdf_render", snapshot=str(snap), out=str(tmp_path / "o.pdf"))
    assert r["status"] == "error" and "MOIRAI_AGENTS_CHROME" in r["error"]


@pytest.mark.skipif(not CHROME, reason="no Chrome on this machine")
def test_a_long_tool_output_is_printed_in_full(tmp_path):
    snap, pdf = tmp_path / "s.html", tmp_path / "o.pdf"
    snap.write_text(SNAP)
    r = call("chat_pdf_render", snapshot=str(snap), out=str(pdf), title="My chat", css=str(R.STATIC / "style.css"), when="exported today")
    assert r["status"] == "ok", r
    v = call("chat_pdf_check", pdf=str(pdf))
    assert v["status"] == "ok", v
    if shutil.which("pdftotext"):
        text = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True).stdout
        assert "line 399 of the output" in text and "FULL_SUBAGENT_TEXT" in text and "My chat" in text
