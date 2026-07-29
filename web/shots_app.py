"""Capture the product screens as composed images rather than arbitrary crops.

The first attempt clipped fixed 1440x960 windows out of a long scrolling page.
That cut cards in half, and because the run list beside `#detail` is short, it
also framed a lot of empty sidebar. Both look like accidents.

This measures each section instead — an `h3` plus the siblings that follow it
up to the next `h3` — clips exactly that, and centres it on a 3:2 canvas.
Nothing is ever cut mid-card, every image has the same margins, and the
sidebar is excluded because the clip is limited to the `#detail` column.
"""

from __future__ import annotations

import io
import json
import urllib.request
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "screenshots"
APP = "https://app.comgu.site"
VM = "35.240.72.53"
PASS = "northstar-2026"

CW, CH = 2880, 1920      # 3:2 at 2x
DSF = 3                  # capture at 3x so filling the canvas never upscales past 1x
MARGIN = 56              # breathing room inside the canvas
BG = (255, 255, 255)

# Section heading -> output name. `None` means the run header block.
# (first heading, last heading, output name). Short sections are grouped:
# alone on a 3:2 canvas a 170pt card is mostly whitespace, and Validation ->
# pull request -> write-back is one story anyway.
SECTIONS = [
    # Findings is six stacked cards, ~1750pt tall — on a 3:2 canvas the whole
    # set shrinks to a third of the frame. Three cards still span critical to
    # high and fill it properly.
    ("Findings",        "Findings",          "01-run-findings.png", 3),
    ("DataHub context", "DataHub context",   "02-mcp-tool-trace.png", None),
    ("Blast radius",    "Blast radius",      "03-blast-radius.png", None),
    ("Generated diff",  "Generated diff",    "07-generated-diff.png", None),
    ("Validation",      "DataHub write-back", "08-validation-pr-writeback.png", None),
    ("Remediation plan", "Approval",         "23-remediation-plan.png", None),
    ("Timeline",        "Timeline",          "24-run-timeline.png", None),
]

# Measure an h3 together with everything up to the next h3, inside #detail only.
BOX_JS = """
([first, last, maxItems]) => {
  const detail = document.querySelector('#detail');
  const kids = [...detail.children];
  const i = kids.findIndex(e => e.tagName === 'H3' &&
                                e.textContent.trim().startsWith(first));
  if (i < 0) return null;
  const endIdx = kids.findIndex(e => e.tagName === 'H3' &&
                                     e.textContent.trim().startsWith(last));
  let j = endIdx + 1;
  const group = [kids[i]];
  for (let k = i + 1; k < kids.length; k++) {
    if (k > endIdx && kids[k].tagName === 'H3') break;
    if (maxItems && group.length > maxItems) break;
    group.push(kids[k]);
  }
  const rects = group.map(e => e.getBoundingClientRect());
  const top    = Math.min(...rects.map(r => r.top))    + window.scrollY;
  const bottom = Math.max(...rects.map(r => r.bottom)) + window.scrollY;
  const left   = Math.min(...rects.map(r => r.left))   + window.scrollX;
  const right  = Math.max(...rects.map(r => r.right))  + window.scrollX;
  return {x: left, y: top, width: right - left, height: bottom - top};
}
"""


def compose(raw: bytes, name: str) -> None:
    """Fit a section onto the 3:2 canvas without distorting or cropping it."""
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    box_w, box_h = CW - 2 * MARGIN, CH - 2 * MARGIN
    # No 1.0 cap: the capture is at 3x, so scaling up to fill a 2x canvas
    # still lands above 1x density and stays sharp.
    scale = min(box_w / img.width, box_h / img.height)
    if scale != 1.0:
        img = img.resize((round(img.width * scale), round(img.height * scale)),
                         Image.LANCZOS)
    canvas = Image.new("RGB", (CW, CH), BG)
    canvas.paste(img, ((CW - img.width) // 2, (CH - img.height) // 2))
    canvas.save(OUT / name, "PNG", optimize=True)
    kb = (OUT / name).stat().st_size / 1024
    fill = round(100 * img.width / CW)
    print(f"  {name:34} fills {fill:>3}% width -> {kb:7.1f} KB")


def token() -> str:
    req = urllib.request.Request(
        f"{APP}/api/auth/demo-token",
        data=json.dumps({"passphrase": PASS}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["token"]


def main() -> int:
    OUT.mkdir(exist_ok=True)
    tok = token()
    with sync_playwright() as p:
        b = p.chromium.launch(args=[f"--host-resolver-rules=MAP app.comgu.site {VM}"])
        c = b.new_context(viewport={"width": 1440, "height": 960},
                          device_scale_factor=DSF)
        c.add_init_script(
            f"try{{localStorage.setItem('comgu_token',{json.dumps(tok)});}}catch(e){{}}")
        pg = c.new_page()
        pg.goto(f"{APP}/app", wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(3000)

        pg.locator("#runlist").locator("text=COMPLETED").first.click()
        pg.wait_for_timeout(2500)
        for first, last, name, cap in SECTIONS:
            box = pg.evaluate(BOX_JS, [first, last, cap])
            if not box:
                print(f"    (no section '{first}') — skipped")
                continue
            pad = 24
            clip = {"x": max(0, box["x"] - pad), "y": max(0, box["y"] - pad),
                    "width": box["width"] + 2 * pad, "height": box["height"] + pad}
            compose(pg.screenshot(full_page=True, clip=clip), name)

        # The approval controls sit after the last heading on the pending run.
        pg.locator("#runlist").locator("text=AWAITING_APPROVAL").first.click()
        pg.wait_for_timeout(2500)
        box = pg.evaluate("""() => {
            const b = [...document.querySelectorAll('#detail button')]
                .find(e => e.textContent.trim().startsWith('Approve remediation'));
            if (!b) return null;
            const card = b.closest('.card') || b.parentElement;
            const r = card.getBoundingClientRect();
            return {x:r.left+scrollX, y:r.top+scrollY, width:r.width, height:r.height};
        }""")
        if box:
            pad = 24
            compose(pg.screenshot(full_page=True, clip={
                "x": max(0, box["x"] - pad), "y": max(0, box["y"] - pad),
                "width": box["width"] + 2 * pad, "height": box["height"] + 2 * pad,
            }), "05-approval-gate.png")

        # Whole-screen views need no composition — they already fill the frame.
        for screen, name in [("overview", "04-overview.png"),
                             ("incidents", "16-incidents.png"),
                             ("rules", "17-rules.png"),
                             ("audit", "18-audit.png")]:
            pg.locator(f'[data-screen="{screen}"]').click()
            pg.wait_for_timeout(1800)
            pg.screenshot(path=str(OUT / name))
            print(f"  {name:34} full viewport")
        b.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
