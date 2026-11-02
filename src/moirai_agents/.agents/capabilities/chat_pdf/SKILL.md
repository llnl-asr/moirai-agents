---
name: chat_pdf
description: Export a whole chat as a shareable PDF, in light mode, with every tool call and output expanded. Load when changing how exports look, when Export PDF fails, or when a user reports something missing from a PDF.
---

# Chat to PDF

The Export PDF menu under the chat takes a snapshot of the chat as drawn (diagrams as images), and the web server calls `chat_pdf_render` and `chat_pdf_check` on it, whichever version of this capability is active. The look of the page is `PAGE_CSS` in `tool.py`, on top of the web UI's `style.css`.

## Rules
- There are two versions. Full opens every block. Compact (`exportPdf` in `index.html`) leaves each tool call as its header row and says so under the title.
- A PDF is complete: never summarize, shorten or leave out a message, or a tool call's header row. Besides what compact folds, controls that do nothing on paper (feedback bar, answer boxes, diagram toolbars) are the only things hidden.
- Always light mode, whatever the page was showing.
- Change the look in `PAGE_CSS`, not in `style.css`, so that the change comes through this capability and its review.
- Run `chat_pdf_check` after every render.

## Constraints
- Needs Chrome or Chromium on the server, or `MOIRAI_AGENTS_CHROME`. Chrome keeps its own sandbox on: do not add `--no-sandbox`.
- A snapshot holds whatever the tools printed, so the page must stay under `CSP`: no scripts and no network, images only as data URIs.
- It can only show what the chat stored. The server keeps the first 8000 characters of a tool's output and the first 6000 of each string in its input (`runtime.py`), and those cuts show in the PDF as they do in the chat.

## Corner cases
- A diagram that cannot be turned into an image stays in the PDF as its error text.
- A chat that is still running exports as far as it has got.
- The day lines say "Today" and "Yesterday" as on screen: the date under the title anchors them.

## Corrector
Predictor `chat_pdf_render`, validator `chat_pdf_check`. A failed verdict comes here: fix the original tool or its input, then predict and validate again. The same failure twice stops the loop: change approach, or ask.

| Failed check | Correct |
|---|---|
| no snapshot or stylesheet | pass the right path |
| not a complete PDF | Chrome stopped early: read its error and render again |
| no pages | the snapshot is empty: export again from the open chat |

## Self-heal
- `exporting needs Chrome`: install it on the server or set `MOIRAI_AGENTS_CHROME`.
- Chrome fails to start: try it on a small file; a machine without user namespaces needs the setuid sandbox fixed by its admin.
- Clipped content in a PDF: some rule still sets `overflow` or `max-height`; add that selector to `PAGE_CSS`.

## Self-learn
- Anything new explored here (workload, software, tool, system) becomes a capability: `capability_builder`, Rules.
- A complaint about a PDF: `lesson_add` on this capability with what was wrong, then refine `PAGE_CSS`.
