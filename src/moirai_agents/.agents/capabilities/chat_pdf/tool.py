"""Chat to PDF: a snapshot of the chat as the web UI drew it, printed by headless Chrome. How the page looks lives here (PAGE_CSS), so a refinement of this capability changes every export."""
import html as H
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

CHROMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
TIMEOUT_S = 180
# Nothing in a snapshot may run or reach out: it holds whatever the agent and its tools printed.
CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
# On top of the web UI's style.css: the chat alone, over as many pages as it needs, nothing clipped.
PAGE_CSS = """
@page{margin:12mm}
*{-webkit-print-color-adjust:exact;print-color-adjust:exact;animation:none!important}
html,body{height:auto;overflow:visible;background:var(--bg)}
#printhead{display:block;margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
#printhead b{display:block;font-size:16px}#printhead span{color:var(--muted);font-size:12px}
.pfull{display:inline}
#log{overflow:visible;padding:0}
.msg,.dayline{max-width:none}
.who,summary{break-after:avoid}
.fb,.ask textarea,.ask .acts,.gate,.dgbar,.dgedit,.dgmsg{display:none!important}
.tool,.tool *,.bubble pre,.tbl,.ask .askd .ctx,.dginfo{max-height:none!important;overflow:visible!important}
.bubble pre,.tool pre,.tool .live{white-space:pre-wrap;overflow-wrap:anywhere}
.tool .tin .v,.bubble td,.bubble th,.bubble code{overflow-wrap:anywhere}
details.tool summary{flex-wrap:wrap}details.tool summary .ds{white-space:normal;flex-basis:100%;order:9}
.bubble table{width:100%}
.dgv img{display:block;max-width:100%;height:auto}
"""


def _chrome():
    c = os.environ.get("MOIRAI_AGENTS_CHROME")
    return c if c else next(filter(None, map(shutil.which, CHROMES)), None)


def _page(body: str, title: str, css: str, when: str) -> str:
    return (f'<!doctype html><html lang="en" data-theme="light"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{CSP}">'
            f"<title>{H.escape(title)}</title><style>{css}{PAGE_CSS}</style></head><body>"
            f'<div id="printhead"><b>{H.escape(title)}</b><span>{H.escape(when)}</span></div><div id="log">{body}</div></body></html>')


def register(mcp):
    @mcp.tool()
    def chat_pdf_render(snapshot: str, out: str, title: str = "moirai-agents chat", css: str = "", when: str = "") -> dict:
        """Print a chat snapshot to a PDF, light mode, every page of it. The web UI's Export PDF button calls this; use it to re-export a saved snapshot.
        snapshot: path of an HTML file holding the inner HTML of the chat log. out: path of the PDF to write. title: shown at the top and as the PDF's title.
        css: path of the web UI's style.css; empty means unstyled. when: the line under the title, such as the export date. Then run chat_pdf_check.
        Returns {status, pdf, bytes}, or {status: "error", error}."""
        src = Path(snapshot)
        if not src.is_file():
            return {"status": "error", "error": f"no snapshot at {snapshot}"}
        if css and not Path(css).is_file():
            return {"status": "error", "error": f"no stylesheet at {css}"}
        chrome = _chrome()
        if not chrome:
            return {"status": "error", "error": f"exporting needs Chrome or Chromium on the server: none of {', '.join(CHROMES)} is on PATH; install one or set MOIRAI_AGENTS_CHROME"}
        with tempfile.TemporaryDirectory(prefix="moirai-pdf-") as d:
            page = Path(d) / "chat.html"
            page.write_text(_page(src.read_text(errors="replace"), title, Path(css).read_text() if css else "", when))
            try:
                r = subprocess.run([chrome, "--headless=new", "--disable-gpu", f"--user-data-dir={d}/profile", "--no-pdf-header-footer", f"--print-to-pdf={out}", page.as_uri()],
                                   capture_output=True, text=True, timeout=TIMEOUT_S)
            except subprocess.TimeoutExpired:
                return {"status": "error", "error": f"Chrome did not finish within {TIMEOUT_S} s"}
        if r.returncode or not Path(out).is_file():
            return {"status": "error", "error": f"Chrome exited with {r.returncode}: {r.stderr.strip()[-400:]}"}
        return {"status": "ok", "pdf": out, "bytes": Path(out).stat().st_size}

    @mcp.tool()
    def chat_pdf_check(pdf: str) -> dict:
        """Rule on a PDF from chat_pdf_render: it exists, is a whole PDF and has at least one page. Use after every render, before handing the file to anyone.
        pdf: path of the PDF. Returns {status, pages, bytes}, or {status: "error", error}."""
        p = Path(pdf)
        if not p.is_file():
            return {"status": "error", "error": f"no PDF at {pdf}"}
        b = p.read_bytes()
        if not b.startswith(b"%PDF-") or b"%%EOF" not in b[-1024:]:
            return {"status": "error", "error": f"{pdf} is not a complete PDF ({len(b)} bytes)"}
        pages = [int(n) for n in re.findall(rb"/Count (\d+)", b)]
        if not pages:
            cmd = shutil.which("pdfinfo")
            out = subprocess.run([cmd, pdf], capture_output=True, text=True).stdout if cmd else ""
            pages = [int(n) for n in re.findall(r"^Pages:\s+(\d+)", out, re.M)]
        if pages and not max(pages):
            return {"status": "error", "error": f"{pdf} has no pages"}
        return {"status": "ok", "pages": max(pages) if pages else None, "bytes": len(b)}
