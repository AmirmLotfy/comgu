# Devpost submission pack

Everything the Devpost form asks for, in one place.
**Deadline: 10 August 2026, 5:00pm EDT** (00:00 EEST on the 11th).

---

## Required fields

| Field | Value |
| --- | --- |
| Project name | Comgu |
| Tagline | Catch commerce changes before customers do. |
| Live demo URL | https://app.35-240-72-53.sslip.io |
| Public repository | https://github.com/AmirmLotfy/comgu |
| Licence | Apache-2.0 |
| Challenge category | **Metadata-Aware Code Generation & Development** (secondary: Agents That Do Real Work) |
| Video | *to record — under 3 minutes, public on YouTube or Vimeo* |

### Judge test access

No account or credit card required.

- **Comgu:** https://app.35-240-72-53.sslip.io — open it and press *Trigger commerce change*
- **DataHub catalog:** https://datahub.35-240-72-53.sslip.io — `judge` / `northstar-2026`
- **Example pull request:** https://github.com/AmirmLotfy/comgu-commerce-lab/pull/1

---

## Description

Comgu is continuous integration for commerce operations.

When a merchant changes a price or a stock level, that value is copied into
many customer-visible surfaces — checkout, product feeds, promotions, bundle
availability, AI shopping manifests, policy copy. Those copies drift
independently, and nobody is alerted, because each system is working correctly:
they are faithfully emitting values that were true yesterday. The merchant finds
out when a customer is charged a different price than they were shown.

Comgu takes a commerce change, asks DataHub what projects from it, runs five
deterministic checks against what those downstream surfaces actually produce,
explains the business risk in plain language, and — only after a human approves —
generates a patch from registered templates, validates it with a real test suite,
opens a pull request, and writes the resolution back into the catalog.

The DataHub integration is load-bearing rather than decorative. The blast radius
comes from `get_lineage`; which asset is authoritative is read from a structured
property rather than assumed; severity is derived from catalog criticality; the
ownership gap on an unowned customer-facing asset becomes its own finding. Two
tests pin this: strip `comgu.authority` and the engine halts instead of guessing,
and empty the lineage and every rule skips.

## Built with

Python 3.11 · FastAPI · SQLAlchemy · Pydantic · DataHub Core v1.5.0.6 ·
DataHub MCP Server · Model Context Protocol · pytest · ruamel.yaml ·
GCE · Docker · Caddy · GitHub CLI · Vertex AI (Gemini, pluggable)

## How DataHub is used

**DataHub technologies:** MCP Server, structured properties, lineage,
ownership, glossary terms, domains, tags, assertions, documents, the
`showcase-ecommerce` datapack.

**Read** — `get_lineage` (blast radius, 3 hops), `get_entities` (governance),
`search`, `list_schema_fields`, `get_lineage_paths_between`.

**Write** — `add_structured_properties`, `add_tags`, `add_owners`,
`save_document` (a Decision document per resolution). Every write is read back;
an unverified write is reported as unverified.

Every MCP call — arguments, duration, result summary — is persisted with the run
and rendered in the UI and the pull-request body.

---

## Open-source contributions

1. **`datahub-commerce-change-impact`** — a vendor-neutral DataHub Skill
   ([`skill/`](skill/datahub-commerce-change-impact)) teaching any agent to
   assess a commerce change: resolve authority from the catalog, trace the blast
   radius with an explicit hop count, compare output rather than configuration,
   surface ownership gaps, refuse to guess when no source of truth is marked, and
   stop before mutating. Written to `datahub-skills` conventions with reference
   material, a write-back template and seven acceptance scenarios.
   *PR to `datahub-project/datahub-skills` — to open.*

2. **A reproducible quickstart defect.** DataHub's quickstart ships
   `ES_BULK_REFRESH_POLICY=WAIT_UNTIL`, which blocks every OpenSearch bulk write
   until the next index refresh (~3s regardless of batch size). Under ingestion
   load the MAE consumer exceeds `max.poll.interval.ms`, is evicted mid-batch,
   replays against already-written documents, hits version conflicts, and stalls
   permanently without committing an offset — silently under-indexing the catalog
   (we saw 428 of 1,267 entities). Diagnosis, fix and measurements in
   [`infra/README.md`](infra/README.md). *Issue to file.*

---

## Sample outputs

[`examples/`](examples/) is generated from a real run, not written by hand:

| File | Contents |
| --- | --- |
| `findings.md` | 6 findings with expected/observed values, customer impact, evidence |
| `generated-diff.patch` | The 5-file patch from registered templates |
| `validation-output.txt` | Real `pytest` output against the patched workspace |
| `datahub-writeback.json` | Catalog writes and their read-back verification |
| `mcp-tool-trace.json` | Every DataHub MCP call |

---

## Pre-existing code disclosure

None. Every line in both repositories was written during the hackathon period
(July 2026). No prior codebase, template or starter was used. Third-party
dependencies are the open-source packages listed under *Built with*.

---

## Demo video script (under 3 minutes)

| Time | Beat |
| --- | --- |
| 0:00–0:15 | One price change. Checkout updates, the feed does not. Nobody is alerted. |
| 0:15–0:30 | Shopify: $89 → $109, inventory 12 → 3. |
| 0:30–0:55 | DataHub: the catalog, its lineage, the five downstream surfaces, owners — and one asset with no owner. |
| 0:55–1:20 | Comgu run: 6 findings. Show the MCP tool trace — the blast radius came from lineage. |
| 1:20–1:40 | Blast radius and business risk. The unowned manifest cannot be auto-assigned. |
| 1:40–1:55 | Approve. Nothing was touched until this moment. |
| 1:55–2:20 | Generated diff, then validation: 6 failed → 7 passed. |
| 2:20–2:35 | The real pull request. |
| 2:35–2:50 | DataHub write-back, read back and verified. |
| 2:50–3:00 | "Comgu catches commerce changes before customers do." |

Record with the browser at 1440×900. Reset the demo first so the contradictions
are present.

---

## Before submitting

- [ ] Record and publish the video (under 3 minutes, public)
- [ ] Open the Skill PR to `datahub-project/datahub-skills`
- [ ] File the quickstart issue on `datahub-project/datahub`
- [ ] Tag a release (`v1.0.0`) on both repositories
- [ ] Confirm the live demo and DataHub URLs respond
- [ ] Complete the Devpost feedback survey (10 × $50 prizes)
- [ ] Optional: register `comgu.site` and repoint DNS from the sslip.io hosts
