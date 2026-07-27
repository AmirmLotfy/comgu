"""Mint a Comgu access token.

    python -m apps.api.scripts.issue_token --role judge
    python -m apps.api.scripts.issue_token --role owner --ttl-days 30

Roles above `judge` are only issuable here, never over HTTP: anything holding
`live:mutate` should require shell access to the deployment.
"""

from __future__ import annotations

import argparse
import os
import sys

from apps.api.auth import ROLES, capabilities_for, issue_token
from apps.api.db.session import init_db, session


def main() -> int:
    ap = argparse.ArgumentParser(description="Issue a Comgu token")
    ap.add_argument("--role", choices=ROLES, default="judge")
    ap.add_argument("--subject", default=None, help="who the token identifies")
    ap.add_argument("--ttl-days", type=int, default=90)
    ap.add_argument("--base-url", default=os.environ.get("COMGU_PUBLIC_URL", ""))
    args = ap.parse_args()

    if not os.environ.get("COMGU_AUTH_SECRET"):
        print(
            "COMGU_AUTH_SECRET is not set. A token issued now will stop working "
            "when the API restarts.\nSet it on the deployment first.",
            file=sys.stderr,
        )

    init_db()
    with session() as db:
        from apps.api.main import ensure_demo_tenant

        org, _ = ensure_demo_tenant(db)

    subject = args.subject or f"{args.role}@comgu.site"
    token = issue_token(args.role, org.id, subject, ttl_seconds=args.ttl_days * 86400)

    print(f"role:         {args.role}")
    print(f"subject:      {subject}")
    print(f"organisation: {org.id}")
    print(f"expires in:   {args.ttl_days} days")
    print(f"capabilities: {', '.join(sorted(capabilities_for(args.role)))}")
    print(f"\ntoken:\n{token}")
    if args.base_url:
        print(f"\nsign-in link:\n{args.base_url.rstrip('/')}/app?token={token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
