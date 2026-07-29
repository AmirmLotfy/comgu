# comgu.site

**Done.** Registered at Namecheap on 27 July 2026, nameservers delegated to
Vercel, and the product and catalog cut over on 29 July 2026.

| Host | Serves | Where |
| --- | --- | --- |
| `comgu.site`, `www.comgu.site` | Marketing | Vercel |
| `app.comgu.site` | Product + API | GCE VM `35.240.72.53` |
| `context.comgu.site` | DataHub UI | GCE VM (behind basic auth) |

The sslip.io hosts are **deliberately still live** and listed alongside the new
names in the Caddyfile, so every URL published before the cutover keeps working.
Do not remove them.

## Two things the original staging got wrong

Recorded because both would have broken the switch for anyone following it:

1. **A wildcard `ALIAS *` in the Vercel zone swallows every subdomain.** Adding
   the domain to Vercel creates `* -> cname.vercel-dns-016.com`, so
   `app.comgu.site` resolved to Vercel and failed the TLS handshake. Explicit
   `A` records for `app` and `context` override it — without them Caddy never
   receives an ACME challenge.

2. **The staged Caddyfile used `{$COMGU_JUDGE_PASSWORD_HASH}` and listed the
   apex.** Caddy on this host has no such environment variable — the hash is
   inlined — so basic auth would have broken. And `comgu.site`/`www` resolve to
   Vercel, so listing them here makes Caddy fail their ACME challenges
   repeatedly and burn Let's Encrypt rate limits. Neither is in the live config.

## What runs where, and why

| Host | Serves | Where |
| --- | --- | --- |
| `comgu.site`, `www.comgu.site` | Marketing | **Vercel** |
| `app.comgu.site` | Product + API | **GCE VM** `35.240.72.53` |
| `context.comgu.site` | DataHub UI | **GCE VM** (behind basic auth) |

Only the marketing site can be static, so only it belongs on a CDN. The product
holds a long-lived MCP session, spawns `pytest` against a git checkout, and does
60–90 seconds of work after the HTTP response returns — none of which a
serverless function can do. DataHub is a four-container stack needing ~8 GB.

Putting marketing on Vercel is not only tidiness: if the VM has trouble during
judging, `comgu.site` still loads and explains the project.

## 1. Register the domain

Any `.site` registrar. Roughly $3–30/year.

## 2. DNS records

| Type | Name | Value | For |
| --- | --- | --- | --- |
| `A` | `@` | `76.76.21.21` | Vercel apex |
| `CNAME` | `www` | `cname.vercel-dns.com` | Vercel www |
| `A` | `app` | `35.240.72.53` | Product |
| `A` | `context` | `35.240.72.53` | DataHub |

Vercel's apex IP can change — confirm against the value the dashboard shows for
this project rather than trusting this table.

Both `comgu.site` and `www.comgu.site` are already attached to the Vercel
project, so they start serving as soon as DNS resolves.

## 3. Point the build at the new app host

The marketing pages link to the product by absolute URL, since it is on a
different host:

```bash
python web/build.py --app-url https://app.comgu.site
cd web/dist && vercel deploy --prod --yes
```

## 4. Switch the VM

Only **after** `app.comgu.site` and `context.comgu.site` resolve to
`35.240.72.53` — Caddy will otherwise fail ACME challenges repeatedly, and
Let's Encrypt rate-limits failed authorisations per hostname.

```bash
# on the VM
HASH=$(caddy hash-password --plaintext "$COMGU_JUDGE_PASSWORD")
sudo COMGU_JUDGE_PASSWORD_HASH="$HASH" \
  cp ~/comgu/infra/Caddyfile.comgu.site /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

The staged Caddyfile keeps the sslip.io hostnames alongside the new ones, so any
link already published in the submission keeps working. Do not remove them.

## 5. Update the submission

`README.md` and `SUBMISSION.md` carry the URLs a judge will use. Change them
only once the new hosts actually respond — a dead link in a submission is worse
than an ugly one.

## Verify

```bash
curl -sI https://comgu.site | head -1
curl -s -o /dev/null -w "%{http_code}\n" https://app.comgu.site/health
curl -s -o /dev/null -w "%{http_code}\n" https://context.comgu.site   # 401 expected
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://app.comgu.site/api/runs   # 401 expected
```

The last two matter: DataHub must stay behind auth, and the API must stay closed
to anonymous mutation.
