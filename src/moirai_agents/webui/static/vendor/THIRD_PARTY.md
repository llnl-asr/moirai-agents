# Third-party code in the web UI

Loaded only when a diagram is on screen (never at page load). All MIT. To update one: `npm pack <name>@<version>`, replace the file, change the version and hash here.

| File | What | Version | License file | sha256 |
|---|---|---|---|---|
| `cytoscape.min.js` | Graph drawing, layout hooks, interaction | 3.33.1 | `LICENSE-cytoscape` | `f55947f3daa3bae53209d4b885c195c157f595c225e508a6b382598d9452d6e2` |
| `dagre.min.js` | Layered graph layout (needed by cytoscape-dagre) | 0.8.5 | `LICENSE-dagre` | `62eb9787ccfdbdf4148d4d99d31dbf9ee4770eafee81e637d759b52aac22cd51` |
| `cytoscape-dagre.js` | Cytoscape adapter for dagre | 2.5.0 | `LICENSE-cytoscape-dagre` | `bf70fe402991dcbff33e05a7e4a5271c78020bb75e85d1c80ab7538e4157112e` |
| `jspdf.umd.min.js` | PDF writer for the download button | 3.0.3 | `LICENSE-jspdf` | `509a1b5b1f3402b81e28510e46145618373a9cba61c94c119d5e9242a86261e1` |
| `svg2pdf.umd.min.js` | Vector SVG to PDF for jsPDF | 2.5.0 | `LICENSE-svg2pdf` | `f8dcc344ae1eafac36e72b4b3cd10e9a993a558a6e9980ebd501382fb63baebe` |

Not vendored: BokehJS. The server serves the copy inside the installed `bokeh` package (`/vendor/bokeh/*`), so it always matches the Python version that builds the plots. `cytoscape-svg` is GPLv3 and is not used: the SVG download is written by `diagrams.js`.
