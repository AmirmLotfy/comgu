"""Record the hackathon demo video by driving the live product.

Nothing here is a mockup or a slideshow of stills. A real browser walks the
deployed site, the real DataHub instance and the real product, triggers a real
run against the live catalog, waits out the actual 39 seconds it takes, and
approves it. What you see is what a judge sees.

    python web/video.py            # record, then mux to mp4
    python web/video.py --keep-webm

Two things worth knowing:

  * There is no narration. Devpost requires the video show the project
    functioning; it does not require a voice track, and a wrong-sounding
    synthetic voice is worse than none. Beats are captioned on screen instead,
    so the story reads without audio. Add a voiceover over the top if you want.
  * DataHub's own login is done in a throwaway context and carried across as
    storage state, so the recording does not open on a login form.

The run in the middle is live, so the total length moves by a few seconds
between takes. Target is ~2:40, and the script prints the final duration —
the hackathon cap is 3:00.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "video"
VM = "35.240.72.53"
SITE = "https://comgu.site"
APP = "https://app.comgu.site"
CATALOG = "https://context.comgu.site"
PASS = "northstar-2026"

W, H = 1920, 1080
SHOPIFY = ("urn:li:dataset:(urn:li:dataPlatform:shopify,"
           "northstar_home.catalog.products,PROD)")
MANIFEST = ("urn:li:dataset:(urn:li:dataPlatform:s3,"
            "northstar_home.ai.shopping_manifest,PROD)")

# A caption bar, injected on every page so it survives navigation. Styled to
# match the site rather than looking like a debug overlay.
CAPTION_JS = """
window.__cap = (text, sub) => {
  let el = document.getElementById('__cap');
  if (!el) {
    el = document.createElement('div');
    el.id = '__cap';
    el.style.cssText = [
      'position:fixed', 'left:0', 'right:0', 'bottom:0', 'z-index:2147483647',
      'padding:22px 40px', 'background:rgba(26,28,31,.94)', 'color:#F6F2E9',
      'font:600 30px/1.35 system-ui,-apple-system,Segoe UI,Roboto,sans-serif',
      'letter-spacing:-.01em', 'transition:opacity .35s', 'opacity:0',
      'box-shadow:0 -8px 32px rgba(0,0,0,.18)',
      // Without this the caption bar covers whatever sits at the bottom of the
      // viewport and swallows the click. It ate the Approve button on take 4.
      'pointer-events:none',
    ].join(';');
    document.body.appendChild(el);
  }
  el.innerHTML = text
    ? text + (sub ? `<div style="font:400 21px/1.4 system-ui;color:#A9A49A;
        margin-top:7px;letter-spacing:0">${sub}</div>` : '')
    : '';
  el.style.opacity = text ? '1' : '0';
};
"""


def token() -> str:
    req = urllib.request.Request(
        f"{APP}/api/auth/demo-token",
        data=json.dumps({"passphrase": PASS}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["token"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-webm", action="store_true")
    args = ap.parse_args()

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    raw = OUT / "raw"
    tok = token()

    resolver = (f"--host-resolver-rules=MAP app.comgu.site {VM},"
                f"MAP context.comgu.site {VM}")

    with sync_playwright() as p:
        # channel="chromium" is required: the default headless-shell binary
        # cannot record video.
        browser = p.chromium.launch(channel="chromium",
                                    args=[resolver, "--force-device-scale-factor=1"])

        # --- sign into DataHub off-camera, keep the session ---------------------
        warm = browser.new_context(
            viewport={"width": W, "height": H},
            http_credentials={"username": "judge", "password": PASS},
        )
        wp = warm.new_page()
        wp.goto(CATALOG, wait_until="domcontentloaded", timeout=90000)
        wp.wait_for_timeout(4000)
        if wp.locator('input[placeholder="Enter username"]').count():
            wp.fill('input[placeholder="Enter username"]', "datahub")
            wp.fill('input[type="password"]', "datahub")
            wp.click('button:has-text("Login")')
            wp.wait_for_timeout(8000)
        state = warm.storage_state()
        warm.close()
        print("  DataHub session warmed")

        # --- the recorded take -------------------------------------------------
        ctx = browser.new_context(
            viewport={"width": W, "height": H},
            storage_state=state,
            http_credentials={"username": "judge", "password": PASS},
            record_video_dir=str(raw),
            record_video_size={"width": W, "height": H},
        )
        ctx.add_init_script(CAPTION_JS)
        ctx.add_init_script(
            f"try{{localStorage.setItem('comgu_token',{json.dumps(tok)});}}catch(e){{}}")
        pg = ctx.new_page()

        def go(url: str, settle: int = 3000) -> bool:
            """Navigate without letting one slow page abort the recording.

            Video encoding competes for CPU with page rendering, and DataHub's
            lineage view is the heaviest page in the demo — a strict
            domcontentloaded wait timed out mid-take. `commit` returns as soon
            as the navigation is committed; the explicit settle covers render.
            """
            try:
                pg.goto(url, wait_until="commit", timeout=120000)
                pg.wait_for_timeout(settle)
                return True
            except Exception as e:
                print(f"    (slow page, continuing: {type(e).__name__})")
                pg.wait_for_timeout(1500)
                return False

        def cap(text: str, sub: str = "", hold: int = 0) -> None:
            pg.evaluate("([t,s]) => window.__cap && window.__cap(t,s)", [text, sub])
            if hold:
                pg.wait_for_timeout(hold)

        def glide(to: int, ms: int = 1400) -> None:
            """Smooth scroll — a jump cut reads as a broken recording."""
            pg.evaluate("(y) => window.scrollTo({top:y, behavior:'smooth'})", to)
            pg.wait_for_timeout(ms)

        # Always reach ctx.close(): that is what flushes the video file.
        # A take that dies at minute two must still leave usable footage.
        try:
            # 1. The problem ---------------------------------------------------
            go(SITE, 2432)
            cap("A merchant changes one price.",
                "Checkout updates. The product feed does not.", 2998)
            cap("Nothing alerts, because nothing is broken.",
                "Every system is faithfully emitting values that were true yesterday.", 3283)
            glide(520)
            cap("Five surfaces project from one catalog record. Four now disagree.",
                "", 2998)

            # 2. Start the real run now, so it executes during the next section.
            #    Waiting out 39 seconds on camera cost a quarter of the runtime
            #    and showed nothing; the catalog tour covers it instead.
            go(f"{APP}/app", 2432)
            cap("Comgu — continuous integration for commerce operations.",
                "No account needed. The judge passphrase is on the sign-in screen.", 2712)
            try:
                pg.locator('button:has-text("Trigger commerce change")').click(timeout=8000)
                cap("Trigger the change: $89 → $109, 12 units → 3.",
                    "The run starts now — it takes 39 seconds against the live catalog.", 2570)
            except Exception:
                pass

            # 3. Meanwhile: the catalog it is reasoning about --------------------
            go(f"{CATALOG}/dataset/{SHOPIFY}/Lineage", 7137)
            pg.keyboard.press("Escape")
            pg.wait_for_timeout(700)
            cap("Our own DataHub Core instance — 1,267 entities.",
                "This lineage is what Comgu walks with get_lineage. It is not a diagram.", 4425)
            cap("comgu.authority marks which asset is the source of truth.",
                "Strip it and the engine halts rather than guessing.", 3711)

            go(f"{CATALOG}/dataset/{SHOPIFY}/Properties", 4639)
            pg.keyboard.press("Escape")
            cap("Price 109.00 · inventory 3 · 30-day returns.",
                "What every downstream surface is supposed to agree with.", 3711)

            go(f"{CATALOG}/dataset/{MANIFEST}/Ownership", 6000)
            pg.keyboard.press("Escape")
            cap("The AI shopping manifest has no owner.",
                "Comgu turns that absence into a finding instead of guessing a recipient.", 6000)

            # 4. Back to a run that has finished on its own ----------------------
            go(f"{APP}/app", 2432)
            # Re-select the run after the round trip. Run cards are
            # <button class="run"> inside #runs — matching on the status TEXT
            # picks a non-clickable node and times out, which is what silently
            # left three takes with no run selected and no approve button.
            selected = False
            for _ in range(20):
                try:
                    pg.locator("#runs button.run").first.click(timeout=4000)
                    pg.wait_for_selector("#app", state="attached", timeout=6000)
                    selected = True
                    break
                except Exception:
                    pg.wait_for_timeout(2000)
            print(f"    run selected, approve control present: {selected}")
            cap("Six findings. Nothing has been touched yet.", "", 2712)

            def to_section(title: str) -> None:
                y = pg.evaluate(
                    """(t) => {const h=[...document.querySelectorAll('#detail h3')]
                        .find(e=>e.textContent.trim().startsWith(t));
                        return h? h.getBoundingClientRect().top+window.scrollY-90 : null;}""",
                    title)
                if y is not None:
                    glide(int(y))

            to_section("DataHub context")
            cap("Every DataHub call is recorded.",
                "get_lineage returned 10 downstream assets — the blast radius is derived, not hardcoded.",
                6500)
            to_section("Blast radius")
            cap("Severity comes from catalog criticality.",
                "The AI shopping manifest has no owner. That absence is its own finding.", 4140)
            to_section("Findings")
            cap("Expected versus observed, with the customer impact spelled out.", "", 4140)

            # 5. The human gate -------------------------------------------------
            to_section("Remediation plan")
            cap("Gemini writes the plan, constrained to registered templates.",
                "A plan citing a finding Comgu did not produce is rejected outright.", 3711)
            # The approval bar renders OUTSIDE #detail, with id="app".
            # Three takes silently filmed empty proof sections because this
            # step failed without saying so — it now reports what it saw.
            approved = False
            try:
                pg.wait_for_selector("#app", state="attached", timeout=25000)
                btn = pg.locator("#app")
                btn.scroll_into_view_if_needed(timeout=8000)
                pg.wait_for_timeout(700)
                cap("Nothing happens until a human approves.", "", 2432)
                btn.click(timeout=10000)
                approved = True
                print("    approve: clicked")
            except Exception as e:
                print(f"    approve: click failed ({type(e).__name__}) — trying DOM")
                try:
                    approved = bool(pg.evaluate(
                        "() => {const b=document.querySelector('#app');"
                        " if(!b) return false; b.click(); return true;}"))
                    print(f"    approve: DOM click -> {approved}")
                except Exception as e2:
                    print(f"    approve: DOM click failed ({type(e2).__name__})")

            if approved:
                cap("Approved. Now it patches, validates and writes back.", "", 0)
                done = False
                for _ in range(12):
                    pg.wait_for_timeout(1500)
                    if "COMPLETED" in pg.evaluate(
                            "() => (document.querySelector('#detail')||{}).textContent||''"):
                        done = True
                        break
                print(f"    run reached COMPLETED on camera: {done}")
                pg.wait_for_timeout(1000)
            else:
                print("    !! approve never registered — proof sections will be empty")
                cap("The approval gate — the run waits here for a person.", "", 2676)

            # 6. The proof ------------------------------------------------------
            to_section("Generated diff")
            cap("Five files patched from registered templates.",
                "The comment explaining why the value is pinned survives the edit.", 4140)
            to_section("Validation")
            cap("A real test run: 6 failed before, 7 passed after.",
                "A failing validation blocks the pull request. There is no override.", 4140)
            to_section("DataHub write-back")
            cap("The resolution goes back into the catalog.",
                "Structured properties, a tag and a Decision document — each read back and verified.",
                6200)

            # 7. Close ----------------------------------------------------------
            go(SITE, 2432)
            cap("Comgu catches commerce changes before customers do.",
                "Apache-2.0 · comgu.site · built on DataHub Core and the DataHub MCP server", 4283)
            cap("", "", 800)
        except Exception as e:
            print(f'    (take ended early: {type(e).__name__}: {e})')

        video = pg.video
        ctx.close()
        browser.close()
        src = Path(video.path())

    webm = OUT / "comgu-demo.webm"
    shutil.move(str(src), webm)
    shutil.rmtree(raw, ignore_errors=True)

    dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(webm)],
        capture_output=True, text=True).stdout.strip()
    secs = float(dur or 0)
    print(f"  raw: {webm.name}  {secs//60:.0f}m{secs%60:04.1f}s  "
          f"{webm.stat().st_size/1024/1024:.1f} MB")

    # The take length moves with network and DataHub render time, so the cap is
    # enforced here rather than hoped for. A <=8% speed-up is imperceptible and
    # keeps every beat, which trimming the tail would not.
    LIMIT = 174.0
    vf = []
    if secs > LIMIT:
        factor = LIMIT / secs
        vf = ["-filter:v", f"setpts={factor:.4f}*PTS"]
        print(f"  normalising {secs:.0f}s -> {LIMIT:.0f}s ({1/factor:.2f}x)")

    mp4 = OUT / "comgu-demo.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(webm), *vf, "-c:v", "libx264", "-preset", "slow",
         "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
         "-r", "30", str(mp4)],
        capture_output=True, check=True)
    final = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(mp4)],
        capture_output=True, text=True).stdout.strip()
    fsec = float(final or 0)
    print(f"  mp4: {mp4.name}  {fsec//60:.0f}m{fsec%60:04.1f}s  "
          f"{mp4.stat().st_size/1024/1024:.1f} MB")
    print("  within the 3:00 limit" if fsec <= 180 else f"  ! STILL OVER at {fsec:.0f}s")
    if not args.keep_webm:
        webm.unlink()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
