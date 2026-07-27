"""Build the static marketing site for Vercel.

The source of truth stays `apps/api/static/marketing/` — the API serves those
same files, so a judge reaching either host sees identical content. This script
only adapts paths for static hosting:

  * the stylesheet moves to `/site.css` (no `/static` mount off the API)
  * `/app` becomes an absolute URL, because the product lives on the VM and
    cannot be served from a CDN

Run it before deploying:  python web/build.py
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "apps" / "api" / "static" / "marketing"
DIST = ROOT / "web" / "dist"

# Where the product actually runs. Overridden once comgu.site resolves.
DEFAULT_APP_URL = os.environ.get("COMGU_APP_URL", "https://app.35-240-72-53.sslip.io")

PAGES = ["index.html", "datahub.html", "security.html", "open-source.html"]


def adapt(html: str, app_url: str) -> str:
    html = html.replace('href="/static/marketing/site.css"', 'href="/site.css"')
    # Only the app link is absolute; internal marketing routes stay relative so
    # the same markup works on both hosts.
    html = re.sub(r'href="/app"', f'href="{app_url}/app"', html)
    return html


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the Comgu marketing site")
    ap.add_argument("--app-url", default=DEFAULT_APP_URL)
    args = ap.parse_args()

    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    shutil.copy(SRC / "site.css", DIST / "site.css")
    for page in PAGES:
        out = adapt((SRC / page).read_text(), args.app_url.rstrip("/"))
        (DIST / page).write_text(out)

    # A judge who loses the VM should still be told what they are looking at.
    (DIST / "404.html").write_text(
        (DIST / "index.html").read_text().replace(
            "<title>Comgu", "<title>Not found — Comgu"
        )
    )

    print(f"built {len(PAGES) + 2} files into {DIST}")
    print(f"  app links -> {args.app_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
