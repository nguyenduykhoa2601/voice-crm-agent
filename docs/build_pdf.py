"""Render docs/TECHNICAL_NOTES.md to docs/TECHNICAL_NOTES.pdf (Markdown -> HTML -> Chromium PDF).

Usage:
    uv run python docs/build_pdf.py [--src docs/TECHNICAL_NOTES.md] [--out docs/TECHNICAL_NOTES.pdf] [--html]

Needs the dev dependency `markdown` and Playwright's Chromium (`uv run playwright install chromium`).
One short-lived browser is opened and closed again.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import markdown
from playwright.async_api import async_playwright

DOCS = Path(__file__).resolve().parent

CSS = """
@page { size: A4; margin: 17mm 16mm 17mm 16mm; }
html { font-size: 11pt; }
body {
  font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
  color: #1f2328; line-height: 1.38; margin: 0;
}
h1 { font-size: 19pt; margin: 0 0 4pt; letter-spacing: -0.2pt; }
h1 + p { color: #57606a; margin-top: 0; }
h2 {
  font-size: 13pt; margin: 14pt 0 4pt; padding-bottom: 2pt;
  border-bottom: 1px solid #d0d7de; break-after: avoid;
}
h3 { font-size: 11pt; margin: 10pt 0 3pt; break-after: avoid; }
p { margin: 0 0 6pt; }
ul, ol { margin: 0 0 6pt; padding-left: 16pt; }
li { margin: 0 0 2pt; }
code {
  font-family: "SFMono-Regular", Menlo, Consolas, monospace; font-size: 8.8pt;
  background: #f3f4f6; padding: 0 2pt; border-radius: 3px;
}
pre {
  background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 4px;
  padding: 6pt 8pt; margin: 4pt 0 8pt; overflow: hidden;
  white-space: pre; break-inside: avoid; line-height: 1.25;
}
pre code { background: none; padding: 0; font-size: 7.6pt; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0 8pt; font-size: 9.2pt; }
tr { break-inside: avoid; }
th, td { border: 1px solid #d0d7de; padding: 3pt 5pt; text-align: left; vertical-align: top; }
th { background: #f6f8fa; }
strong { color: #111; }
a { color: #0969da; text-decoration: none; }
"""


def render_html(src: Path) -> str:
    body = markdown.markdown(
        src.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code", "sane_lists"],
        output_format="html5",
    )
    title = "Voice CRM Agent: technical notes"
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{title}</title><style>{CSS}</style></head><body>{body}</body></html>"
    )


async def to_pdf(html: str, out: Path) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="load")
            await page.pdf(
                path=str(out),
                format="A4",
                print_background=True,
                display_header_footer=True,
                header_template="<span></span>",
                footer_template=(
                    '<div style="font-size:7pt;color:#8c959f;width:100%;text-align:center;">'
                    '<span class="pageNumber"></span> / <span class="totalPages"></span></div>'
                ),
                margin={"top": "17mm", "bottom": "17mm", "left": "16mm", "right": "16mm"},
            )
        finally:
            await browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", default=str(DOCS / "TECHNICAL_NOTES.md"))
    ap.add_argument("--out", default=str(DOCS / "TECHNICAL_NOTES.pdf"))
    ap.add_argument("--html", action="store_true", help="also write the intermediate HTML next to the PDF")
    args = ap.parse_args()
    src, out = Path(args.src), Path(args.out)
    html = render_html(src)
    if args.html:
        out.with_suffix(".html").write_text(html, encoding="utf-8")
    asyncio.run(to_pdf(html, out))
    print(f"wrote {out.name}")


if __name__ == "__main__":
    main()
