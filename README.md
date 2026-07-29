<div align="center">
  <img src="docs/comgu-logo.png" alt="Comgu" width="120">
  <h1>comgu</h1>
  <p><strong>Catch commerce changes before customers do.</strong></p>
</div>

---

A merchant changes one price. Checkout updates. The product feed does not.

For the next few hours, shoppers see one price in Google Shopping and are charged
another at checkout. A bundle keeps selling five units of an item with three in
stock. A promotion keeps discounting from a price the store no longer charges. An
AI shopping agent quotes the old figure to a customer who never visits the site.

Nobody is alerted, because every one of those systems is working correctly. They
are faithfully emitting values that were true yesterday.

**Comgu is continuous integration for commerce operations.** It takes a commerce
change, asks DataHub what depends on it, runs deterministic checks against every
downstream surface, explains the business risk, and — after a human approves —
generates a validated patch, opens a pull request, and writes the resolution back
into the catalog.

Built for [Build with DataHub: The Agent Hackathon](https://datahub.devpost.com/).

## Try it

| | |
| --- | --- |
| **Marketing site** | **https://comgu.vercel.app** |
| **Live demo** | **https://app.comgu.site/app** |
| DataHub catalog | https://context.comgu.site — `judge` / `northstar-2026` |
| Example pull request | [comgu-commerce-lab#1](https://github.com/AmirmLotfy/comgu-commerce-lab/pull/1) |

The root is the marketing site; the product is at **`/app`**, behind the demo
passphrase `northstar-2026` — the instance mutates a real catalog and a real
repository, so it is not left anonymously open.

Press **Trigger commerce change**, watch the run reach *Awaiting approval*,
read the findings and the MCP tool trace, then **Approve**. Comgu patches five
configuration files, runs the real parity suite, and writes the resolution back
into DataHub. **Reset demo** restores the contradictions.

Pull requests are dry-run on the hosted instance — it says so rather than
inventing a URL. The linked PR above was opened by a real run.

---

## The golden path

One commerce change, end to end:

```
Northstar Brew Pro:  $89.00 → $109.00,  inventory 12 → 3
```

| Step | What happens | Where the truth comes from |
| --- | --- | --- |
| 1. Context | Trace what projects from the catalog | **DataHub** `get_lineage`, 3 hops |
| 2. Authority | Determine which value is correct | **DataHub** `comgu.authority` structured property |
| 3. Checks | Five deterministic rules over real transform output | Comgu rule engine |
| 4. Blast radius | 10 assets, criticality and ownership per asset | **DataHub** `get_entities` |
| 5. Plan | Rank corrections, explain impact | AI, schema-validated |
| 6. Approval | A human approves or rejects | — |
| 7. Patch | Registered templates, allowlisted paths, isolated workspace | Comgu patcher |
| 8. Validation | Real `pytest` against the patched workspace | Commerce lab |
| 9. Pull request | Real PR, only if validation passed | GitHub |
| 10. Write-back | Decision document + properties + tag, then read back | **DataHub** mutation tools |

Actual output of a full run:

```
findings=6  patched=5  validation=passed  writeback=verified  pr=open
```

- Findings: [`examples/findings.md`](examples/findings.md)
- Generated diff: [`examples/generated-diff.patch`](examples/generated-diff.patch)
- Pull request: [comgu-commerce-lab#1](https://github.com/AmirmLotfy/comgu-commerce-lab/pull/1)

---

## DataHub is load-bearing, not decorative

The easiest way to fake this category is to render a lineage graph beside an
answer that was computed some other way. Comgu is built so that it cannot:

**The blast radius is lineage.** Downstream assets come from `get_lineage` rooted
at the changed asset. There is no hardcoded topology to fall back on. Remove the
lineage edges and the run produces nothing.

**Authority is a catalog property, not a rule.** Which asset is the source of
truth is read from `comgu.authority` in DataHub. The rule engine refuses to run
without it, rather than assuming the Shopify catalog wins:

```python
def test_engine_refuses_to_guess_without_datahub_authority():
    ctx = golden_context()
    for asset in ctx.assets_by_urn.values():
        asset.authority = "projection"
    report = run_rules(ctx)
    assert report.findings == []
    assert "authoritative" in report.context_error
```

**Severity is governance.** A finding's severity is derived from
`comgu.criticality` and `comgu.customer_facing` on the asset. Re-govern the asset
in DataHub and Comgu grades the same failure differently — also pinned by test.

**Ownership routing is catalog ownership.** The unowned AI manifest produces its
own finding, marked not-auto-fixable, because there is nobody to route it to.

**Quality signals corroborate.** A failing DataHub assertion on the merchant feed
is attached as evidence — the catalog already knew, which tells the operator how
long it has been wrong. It is never the trigger: Comgu's own check decides.

**The MCP tool trace is persisted and shown.** Every call, its arguments,
duration and result summary are stored with the run and rendered in the PR body.
The reasoning is inspectable rather than asserted.

Read paths use `search`, `get_entities`, `get_lineage`, `get_lineage_paths_between`
and `list_schema_fields`. Write-back uses `add_structured_properties`, `add_tags`,
`add_owners` and `save_document`, each verified by reading it back.

Assertions are read over GraphQL rather than MCP, and the trace labels them
`graphql:` rather than pretending otherwise: on DataHub Core v1.5.0.6 the
`get_dataset_assertions` tool never registers even with
`DATA_QUALITY_TOOLS_ENABLED=true`, though the gate logs `ENABLED`. Rather than
drop the signal, Comgu reads it directly and says so.

---

## What is real and what is simulated

Stated plainly, because the category invites overclaiming.

**Real:** the DataHub instance (Core, self-hosted, ~1,270 entities), all MCP
calls, the lineage graph, the transformation code, the contradictions (produced
by executing that code, not fixtures), the generated patches, the `pytest` run,
the GitHub pull request, and the catalog write-back.

**Simulated:** the downstream commerce platforms themselves. There is no live
Google Merchant Center account or promotion engine behind
[comgu-commerce-lab](https://github.com/AmirmLotfy/comgu-commerce-lab); it is a
repository of real transforms and real tests standing in for them. Every asset it
produces is tagged `comgu:simulated-downstream` in DataHub, and labelled as such
wherever Comgu shows it.

**Not yet built:** Shopify OAuth against a live development store. The webhook
*receiving* path — HMAC verification, idempotency, topic allowlisting,
normalization — is implemented and tested; what is missing is a Partner account
and store to point it at. See [Status](#status).

---

## Architecture

```
comgu/
  packages/datahub/   MCP gateway, context builder, write-back
  packages/rules/     five deterministic checks
  packages/patch/     constrained generation + registered-command validation
  packages/planner/   AI planner behind a validated schema
  packages/github/    pull requests
  packages/lab/       bridge to the commerce lab checkout
  seed/               Commerce Lab topology + emitter
  infra/              VM bootstrap, quickstart fixes
  skill/              DataHub Skill contribution
```

DataHub Core runs on a GCE `e2-standard-4`; GMS stays bound to localhost and is
reached over an SSH tunnel. See [`infra/README.md`](infra/README.md) — **it
contains a required fix to the DataHub quickstart** without which the graph never
becomes queryable.

---

## Setup

Requires Python 3.11, `uv`, and a DataHub instance.

```bash
git clone https://github.com/AmirmLotfy/comgu && cd comgu
uv venv --python 3.11 && uv pip install -e ".[dev]"
```

```bash
git clone https://github.com/AmirmLotfy/comgu-commerce-lab ../comgu-commerce-lab
cd ../comgu-commerce-lab && uv venv --python 3.11 && uv pip install -e ".[dev]"
```

Bring up DataHub and seed the graph — full instructions in
[`infra/README.md`](infra/README.md):

```bash
datahub docker quickstart && datahub datapack load showcase-ecommerce
python3 infra/fix_quickstart_consumer.py   # required; see infra/README.md
DATAHUB_GMS_URL=http://localhost:8080 python -m seed.commerce_lab
DATAHUB_GMS_URL=http://localhost:8080 python -m seed.verify   # → COMMERCE_LAB_OK
```

---

## Verification

Offline — no DataHub, no network:

```bash
pytest apps packages tests -q
```

Rule engine, patch safety, planner guards, workflow transitions, webhook
signature verification, OAuth open-redirect and state-replay resistance, and
prompt-injection resistance: **165 tests**.

The commerce lab fails before remediation and passes after:

```bash
cd ../comgu-commerce-lab && pytest -q     # 6 failed, 1 passed
```

Full chain against live DataHub:

```bash
DATAHUB_GMS_URL=http://localhost:18080 \
COMGU_LAB_PATH=../comgu-commerce-lab \
python -m apps.api.scripts.golden_path --remediate --assert-findings 5
```

Add `--pr-live` with `GITHUB_LAB_REPO` set to open a real pull request. Without
it, the PR step is a dry run and says so — a URL is never fabricated.

The security suite on its own, if that is what you want to inspect:

```bash
pytest tests/security -q     # 29 tests
pytest tests/failure -q      # 20 tests — every dependency broken in turn
```

Before any of it, check the host will actually run Comgu:

```bash
python -m apps.api.scripts.doctor
```

24 checks — architecture, memory, disk, Docker, ports, CLIs, environment, the
lab interpreter and DataHub reachability. Every failure names the remedy, not
just the symptom.

Signature forgery, duplicate delivery, path traversal, symlink escape, command
allowlisting, prompt injection, approval-gate bypass, OAuth open redirect and
state replay, secret redaction, tenant scoping: 27 tests in one file.

**The DataHub-dependency proof:** delete the lineage edges from the catalog,
re-run, and the blast radius is empty and no rule can fire. Restore them and the
six findings return.

---

## Safety

- HMAC verified over raw request bytes with `compare_digest`; rejected
  deliveries are recorded but never processed; duplicate deliveries collapse
  onto the existing event; the topic list is closed
- Patch sandbox: path and extension allowlists, no traversal, no symlink escape
  (including symlinked directories), size caps, isolated workspace
- Validation runs only command **ids** from a fixed registry — there is no path
  from a model, a finding, or a DataHub description to a shell string
- The planner cannot widen its own authority: unregistered templates, unknown
  action types, and actions citing findings Comgu did not produce are all
  rejected, falling back to the deterministic plan
- Retrieved catalog text is untrusted input; a prompt-injection test asserts that
  a model obeying injected instructions still cannot get them executed
- The approval gate is a property of the state graph: from `AWAITING_APPROVAL`
  there is no edge to `PATCH_GENERATING`, and from `VALIDATION_FAILED` there is
  no edge to a pull request
- Approvals are bound to the plan and context checksums that were displayed, so
  a decision cannot be replayed against a different plan
- Failed validation blocks pull-request creation
- Output is redacted before storage; no secrets in the repository

---

## Status

| Area | State |
| --- | --- |
| DataHub context, lineage, blast radius | done, live |
| Five deterministic checks | done |
| Patch generation + validation | done |
| GitHub pull requests | done, real PR opened |
| DataHub write-back + verification | done, verified |
| AI planner + guards | done |
| Persistence + 23-state workflow machine | done |
| API, operator UI, judge demo mode | done, deployed |
| Recovery worker | done |
| Shopify OAuth + webhook receive path | done, tested |
| Connected to a live development store | needs a Partner account |
| DataHub Skill contribution | [PR #58 open upstream](https://github.com/datahub-project/datahub-skills/pull/58) |
| Demo video | not recorded |

**165 tests**, all offline.

---

## Open-source contribution

[`skill/datahub-commerce-change-impact`](skill/datahub-commerce-change-impact) is
a vendor-neutral DataHub Skill teaching any agent to assess a commerce change
safely: resolve authority from the catalog, trace the blast radius, refuse to
guess when no source of truth is marked, and stop before mutating. Written to
`datahub-skills` conventions with reference material, a write-back template and
seven acceptance scenarios.

**Submitted upstream: [datahub-skills#58](https://github.com/datahub-project/datahub-skills/pull/58)**

This build also surfaced a reproducible defect in the DataHub quickstart —
`ES_BULK_REFRESH_POLICY=WAIT_UNTIL` stalls the MAE consumer under ingestion load,
silently under-indexing the catalog (428 of 1,267 entities). Diagnosis, fix and
measurements are in [`infra/README.md`](infra/README.md).

**Reported upstream: [datahub#18642](https://github.com/datahub-project/datahub/issues/18642)**

---

## Licence

Apache-2.0. See [LICENSE](LICENSE).
