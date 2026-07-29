"""Capture the project gallery screenshots from the live instance.

Everything here is a real screenshot of the deployed product and the real
DataHub instance — nothing is mocked or composited. Run it after staging a
completed run and one parked at the approval gate:

    python web/screenshots.py

Output lands in `screenshots/` at 1440x960 (3:2, which is what Devpost asks
for). Two details worth knowing:

  * The app keeps its judge token in localStorage, so the token is minted from
    the API and injected before the first paint. Otherwise every shot would be
    of the sign-in screen.
  * `app.comgu.site` and `context.comgu.site` are pinned to the VM with
    Chromium's host-resolver rules. That keeps the real hostnames in the shots
    without depending on whatever the local resolver has cached.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "screenshots"
VM_IP = os.environ.get("COMGU_VM_IP", "35.240.72.53")
APP = "https://app.comgu.site"
CATALOG = "https://context.comgu.site"
SITE = "https://comgu.site"
PASSPHRASE = os.environ.get("COMGU_DEMO_PASSPHRASE", "northstar-2026")

W, H = 1440, 960  # 3:2

# Real hostnames in the shots, resolved to the VM regardless of local DNS.
RESOLVER = (
    f"--host-resolver-rules=MAP app.comgu.site {VM_IP},"
    f"MAP context.comgu.site {VM_IP}"
)


def mint_token() -> str:
    """Ask the API for a judge token, the same way the sign-in screen does."""
    req = urllib.request.Request(
        f"{APP}/api/auth/demo-token",
        data=json.dumps({"passphrase": PASSPHRASE}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["token"]


def shoot(page, name: str, full: bool = False) -> None:
    path = OUT / name
    page.screenshot(path=str(path), full_page=full)
    kb = path.stat().st_size / 1024
    print(f"  {name:44} {kb:7.1f} KB")


def settle(page, ms: int = 1200) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def _anchor_y(page, text: str, selector: str) -> int | None:
    return page.evaluate(
        """([t, sel]) => {
            const el = [...document.querySelectorAll(sel)]
                .find(e => e.textContent.trim().startsWith(t));
            return el ? Math.round(el.getBoundingClientRect().top + window.scrollY) : null;
        }""",
        [text, selector],
    )


def region(page, name: str, text: str, selector: str = "#detail h2, #detail h3") -> bool:
    """Screenshot a 1440x960 window of the page starting at `text`.

    Clipping beats scrolling here. Scrolling cannot separate sections that share
    a viewport, and it cannot frame anything in the last screenful at all — the
    page simply runs out of scroll, so three different sections produced three
    identical images. Clip coordinates are in full-page space, so every section
    gets the same framing wherever it sits.
    """
    # Pad the bottom so the last sections can be framed the same way as the
    # first. Without it the clip clamps to the page end and every section in the
    # final screenful lands on an identical top — three sections, one image.
    page.evaluate(f"document.body.style.paddingBottom = '{H}px'")
    y = _anchor_y(page, text, selector)
    if y is None:
        print(f"    (no '{text}' on this run — skipped)")
        return False
    page.screenshot(path=str(OUT / name), full_page=True,
                    clip={"x": 0, "y": max(0, y - 60), "width": W, "height": H})
    kb = (OUT / name).stat().st_size / 1024
    print(f"  {name:44} {kb:7.1f} KB")
    return True


def open_run(page, status: str) -> bool:
    """Click the first run in the sidebar carrying `status`."""
    row = page.locator("#runlist").locator(f"text={status}").first
    try:
        row.click(timeout=6000)
        settle(page)
        return True
    except Exception:
        print(f"    (no run in state {status} — stage one first)")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-catalog", action="store_true",
                    help="skip the DataHub shots (slow to render)")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    token = mint_token()
    print(f"  judge token minted ({len(token)} chars)\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(args=[RESOLVER])

        # --- marketing: no auth ------------------------------------------------
        ctx = browser.new_context(viewport={"width": W, "height": H},
                                  device_scale_factor=2)
        page = ctx.new_page()
        for path, name in [
            ("/", "10-site-home.png"),
            ("/catalog", "11-site-catalog-access.png"),
            ("/datahub", "12-site-datahub.png"),
            ("/security", "13-site-security.png"),
            ("/open-source", "14-site-open-source.png"),
        ]:
            page.goto(SITE + path, wait_until="domcontentloaded", timeout=45000)
            settle(page)
            shoot(page, name)
        ctx.close()

        # --- product: token injected before first paint -------------------------
        ctx = browser.new_context(viewport={"width": W, "height": H},
                                  device_scale_factor=2)
        ctx.add_init_script(
            f"try{{localStorage.setItem('comgu_token', {json.dumps(token)});}}catch(e){{}}"
        )
        page = ctx.new_page()
        page.goto(f"{APP}/app", wait_until="domcontentloaded", timeout=45000)
        settle(page, 2500)

        if page.locator("#signin").is_visible():
            print("  ! still on sign-in — token not accepted")
            return 1

        # Completed run: findings, trace, diff, validation, write-back.
        if open_run(page, "COMPLETED"):
            for text, name in [
                ("Findings",           "01-run-findings.png"),
                ("MCP tool trace",     "02-mcp-tool-trace.png"),
                ("Blast radius",       "03-blast-radius.png"),
                ("DataHub context",    "06-datahub-context.png"),
                ("Generated diff",     "07-generated-diff.png"),
                ("Validation",         "08-validation.png"),
                ("Pull request",       "09-pull-request.png"),
                ("DataHub write-back", "19-datahub-writeback.png"),
                ("Remediation plan",   "23-remediation-plan.png"),
                ("Run ",               "24-run-timeline.png"),
            ]:
                region(page, name, text)
            shoot(page, "15-run-detail-full.png", full=True)

        # The approval gate. Here the anchor is the button, not a heading —
        # the pending run has no section past "Remediation plan".
        if open_run(page, "AWAITING_APPROVAL"):
            region(page, "05-approval-gate.png", "Approve remediation",
                   selector="#detail button")

        # The other screens.
        for screen, name in [
            ("overview",    "04-overview.png"),
            ("incidents",   "16-incidents.png"),
            ("rules",       "17-rules.png"),
            ("audit",       "18-audit.png"),
        ]:
            try:
                page.locator(f'[data-screen="{screen}"]').click(timeout=6000)
                settle(page)
                shoot(page, name)
            except Exception:
                print(f"    (screen {screen} unavailable — skipped)")
        ctx.close()

        # --- DataHub: basic auth in front, then DataHub's own login ------------
        if not args.skip_catalog:
            def dismiss_tour(pg) -> None:
                """Close DataHub's first-run tour — Escape only.

                Do NOT click generic close selectors here. On an asset page
                `[aria-label="Close"]` and `.ant-modal-close` also match the
                small x controls that REMOVE a domain, tag or owner from the
                asset. An earlier version of this clicked one and opened a
                "Confirm Domain Removal" dialog on the live catalog. Escape
                dismisses the tour and cannot mutate anything.
                """
                for _ in range(3):
                    pg.keyboard.press("Escape")
                    pg.wait_for_timeout(600)
            ctx = browser.new_context(
                viewport={"width": W, "height": H}, device_scale_factor=2,
                http_credentials={"username": "judge", "password": PASSPHRASE},
            )
            page = ctx.new_page()
            shopify = ("urn:li:dataset:(urn:li:dataPlatform:shopify,"
                       "northstar_home.catalog.products,PROD)")
            manifest = ("urn:li:dataset:(urn:li:dataPlatform:s3,"
                        "northstar_home.ai.shopping_manifest,PROD)")
            for url, name, wait in [
                (f"{CATALOG}/dataset/{shopify}/Lineage", "20-datahub-lineage.png", 6000),
                (f"{CATALOG}/dataset/{shopify}/Properties", "21-datahub-properties.png", 4000),
                (f"{CATALOG}/dataset/{manifest}", "22-datahub-unowned-asset.png", 4000),
            ]:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    settle(page, wait)
                    shoot(page, name)
                except Exception as e:
                    print(f"    ({name} failed: {type(e).__name__}) — skipped")
            ctx.close()

        browser.close()

    files = sorted(OUT.glob("*.png"))
    total = sum(f.stat().st_size for f in files) / 1024 / 1024
    print(f"\n  {len(files)} images, {total:.1f} MB total -> {OUT}")
    over = [f.name for f in files if f.stat().st_size > 5 * 1024 * 1024]
    if over:
        print(f"  ! over Devpost's 5 MB limit: {over}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
