## Inspiration

A merchant raises the price of one product from $89 to $109 and drops the stock
from 12 units to 3. Checkout picks it up instantly. The Google Merchant feed
does not. Neither does the promotion engine, the returns policy page, or the
JSON manifest that AI shopping agents read.

For the next few hours, shoppers are quoted one price and charged another. A
bundle keeps selling units that no longer exist. A "15% off" badge is anchored
to a price nobody pays.

Nothing alerts, because nothing is broken. Every one of those systems is working
exactly as designed — faithfully emitting values that were true yesterday. There
is no exception to catch, no error rate to spike, no dashboard that turns red.
The failure is *semantic*, and the only system that knows those assets are
related to each other is the data catalog.

That was the whole idea: if the catalog already knows what depends on what, it
should be able to tell you what a commerce change just broke — before a customer
finds out.

## What it does

Comgu is continuous integration for commerce operations. A price or inventory
change arrives as a Shopify webhook, and Comgu:

1. **Asks DataHub what depends on it.** `get_lineage` from the authoritative
   Shopify catalog asset gives the blast radius — five downstream surfaces, some
   three hops away.
2. **Decides what "correct" means from the catalog, not from itself.** The
   `comgu.authority` structured property marks which asset is the source of
   truth. If no asset in the graph claims authority, the rule engine halts
   instead of guessing.
3. **Runs five deterministic checks** — price parity, inventory safety,
   promotion integrity, AI-commerce freshness, policy consistency — grading
   severity from `comgu.criticality` and `comgu.customer_facing` on each asset,
   and routing each finding to the owner recorded in DataHub. One surface has no
   owner; that absence becomes its own finding rather than being silently
   assigned.
4. **Asks Gemini to write the remediation plan**, constrained to a JSON schema
   and rejected outright if it references a finding Comgu didn't produce or a
   template that isn't registered.
5. **Stops, and waits for a human.**
6. **On approval**, generates a patch from registered templates into allowlisted
   paths, runs the real test suite in the target repo, opens a genuine GitHub
   pull request, and writes the resolution back into DataHub — a Decision
   document, structured properties, and a `comgu:remediated` tag, each read back
   and verified.

On the live instance: **39 seconds** from trigger to the approval gate, **10
seconds** after approving, **49 seconds** end to end. Six findings, five files
patched, and a commerce parity suite that goes from `6 failed, 1 passed` to
`7 passed`.

**What's real and what isn't.** The DataHub instance is real and self-hosted
(1,267 entities). The lineage, ownership, structured properties and assertions
are real. The transformation code is real — the contradictions are produced by
executing it, not hardcoded. The patches, the test run, the pull request and the
catalog write-back are all real.

The downstream commerce platforms are simulated. There is no live Google
Merchant Center account or promotion engine behind this. Every simulated asset
is tagged `comgu:simulated-downstream` in the catalog, and the product labels it
wherever that output is shown. The Shopify OAuth and signed-webhook path is
implemented and tested against recorded payloads; a live Partner store is not
connected yet.

## How we built it

**DataHub Core**, self-hosted on a GCE VM — six containers behind Caddy with
TLS. Not DataHub Cloud, not a mock: the same stack anyone can run with
`datahub docker quickstart`.

**The DataHub MCP server** is the only way Comgu talks to the catalog. Every
tool call is recorded into a `ToolTrace` that the UI renders, so you can see
exactly which catalog calls produced a finding. There's no second path to the
data — pull the MCP session and Comgu has nothing to reason about.

**FastAPI + SQLAlchemy 2.0 + Alembic** for the platform. A 23-state workflow
machine with explicitly declared transitions, where the approval gate is a
property of the graph rather than an `if` statement someone can forget. Every
transition writes an audit row.

**Vertex AI (Gemini 2.5 Pro)** for the remediation plan, with a deterministic
fallback. If the model is unavailable, returns invalid JSON, or hallucinates a
finding, Comgu builds the same plan from the findings' own templates. A model
outage degrades the prose, never the correctness.

**Patch generation** uses `ruamel.yaml` round-trip parsing so the comments that
explain *why* a pinned value exists survive the edit. A patch that touches a
path outside the allowlist is rejected before it's written.

## Challenges we ran into

**The catalog was lying, quietly.** Partway through seeding, DataHub reported
428 entities when we'd ingested far more. Consumer lag read as zero. Every API
that reads from the aspect store returned correct data. Only search and lineage
were short — which is exactly what Comgu depends on.

The cause was `ES_BULK_REFRESH_POLICY=WAIT_UNTIL` in the quickstart compose
profile. It makes every OpenSearch bulk write block until the next index
refresh, roughly 3 seconds regardless of batch size. The tell was flat latency:
one event took 1893ms, fourteen events took 2803ms. Under load the MAE consumer
blew past `max.poll.interval.ms`, got evicted mid-batch, rejoined, replayed the
same records, hit version conflicts, and never committed an offset — so lag
looked frozen at zero because it's measured against *committed* offsets.

Setting the policy to `NONE` with `MAX_POLL_RECORDS=50` drained a 1,748-message
backlog in 140 seconds and took the catalog from 428 to 1,267 entities. We filed
it upstream as [datahub#18642](https://github.com/datahub-project/datahub/issues/18642),
because a catalog that silently under-indexes is worse than one that fails
loudly.

**Our own validation lied too.** In production, runs came back reporting the
test suite had *failed*. It hadn't — the workspace was being copied without its
virtualenv, so `pytest` was invoked with the wrong interpreter and the resulting
`No module named pytest` was being classified as a test failure. A broken
environment was being reported as broken code. We now resolve the target repo's
own interpreter, and a missing module is an `error`, not a `failed`. Being wrong
about *why* something failed turned out to be worse than failing.

**`get_lineage` defaults to one hop.** Accepting that default silently
understates a blast radius — a feed three hops downstream is just as
customer-visible. We set the hop count explicitly, and wrote a test that fails
if the blast radius comes back empty.

**Structured properties have to exist before they hold values.** Batching the
writes lost the ordering and produced `Unexpected null value found for
Structured Property Definition`. Splitting it into three ordered phases fixed
it.

**`ASYNC_WAIT` wasn't trustworthy** — it reported "Consumer has processed past
the offset" for writes that had actually landed. We switched to batched `ASYNC`
and verify every write by querying the graph back.

**A public demo that mutates a real catalog is a liability.** The instance
initially let anyone with the URL trigger runs, approve remediations and reset
state. It now uses capability-based auth: the judge role can run the full flow
and approve, but `live:mutate` is withheld from it, so it cannot open a pull
request against the real repository no matter what the UI offers.

**Alembic bit us.** `create_all` adds missing tables but not missing columns, so
a schema that looked fine locally produced `no such column:
findings.rule_execution_id` in production. Stamping it at head made it worse.
The migration now refuses to stamp an unversioned database that has drifted, and
a test compares the models against the migrations.

## Accomplishments that we're proud of

**DataHub is load-bearing, and we can prove it.** Two tests exist for exactly
this: strip the lineage edges and every rule skips; strip the `comgu.authority`
markers and the engine halts with an error instead of picking a value. You can
run the same experiment against the live instance — delete the lineage in
DataHub, trigger a run, watch the blast radius come back empty, restore it and
the six findings return.

**The system refuses to guess.** When the catalog can't say which value is
authoritative, Comgu stops. That was a deliberate choice and it survived every
temptation to add a fallback heuristic.

**Two contributions back to DataHub** — a
[Skill for driving DataHub over MCP](https://github.com/datahub-project/datahub-skills/pull/58)
and the [quickstart indexing defect](https://github.com/datahub-project/datahub/issues/18642)
we found while building this.

**165 tests**, including a security suite and a failure-path suite that covers
DataHub timeouts, model timeouts, validation failure, GitHub failure and
write-back failure. The demo also survives a host reboot — we tested that by
actually rebooting it.

## What we learned

Metadata is only infrastructure if something breaks when you remove it. It's
easy to call a catalog "the source of truth" while quietly hardcoding the same
facts in your own config. Writing the two tests that delete DataHub's
contribution and assert failure was the most clarifying thing we did — it turned
an architectural claim into something falsifiable.

We also learned how much of agent safety is just refusing to be clever. The
model never sees a shell, a file handle, or a catalog write. It gets a rendered
summary of findings that were already established deterministically, and returns
JSON that's validated, schema-checked, and cross-referenced against the findings
we actually produced. Every genuinely dangerous capability sits behind a human.

And that a system reporting the *wrong reason* for a failure is more dangerous
than one that fails loudly. Both of the worst bugs in this build — the indexing
stall and the validation misreport — were failures that presented as something
else.

## What's next for Comgu

**Connect a live Shopify Partner store.** The OAuth and webhook code is written
and tested; it needs a store and a public callback to become a real integration
rather than a tested one.

**More rule families.** Tax and shipping-rate parity, multi-currency price
consistency, and subscription-plan drift are the same shape as the five that
exist.

**Per-tenant rule configuration.** Thresholds, enable/disable, and severity
overrides — the schema is there, the UI isn't.

**Push more back upstream.** The MCP integration surfaced several rough edges
worth turning into issues or patches beyond the one we filed.
