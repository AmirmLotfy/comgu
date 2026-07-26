# Comgu infrastructure

DataHub Core runs on a GCE VM rather than a laptop: the quickstart needs ~8 GB
RAM and ~13 GB disk, and running it alongside the app on a smaller machine
starves it.

| Piece | Where |
|---|---|
| DataHub Core (quickstart) | GCE `e2-standard-4`, `europe-west1-b` |
| GMS | `localhost:8080` on the VM — **never exposed publicly** |
| DataHub UI | `:9002`, to be fronted by TLS for judges |

## Bringing up a host

`vm-startup.sh` runs as the VM's startup script. It installs Docker, adds 4 GB
of swap, and raises `vm.max_map_count` for OpenSearch.

Then, on the VM:

```bash
uv tool install --python 3.11 acryl-datahub
datahub docker quickstart
datahub datapack load showcase-ecommerce
```

## Required: fix the quickstart's indexing stall

**Run `fix_quickstart_consumer.py` before seeding.** Without it the graph never
becomes queryable, and the failure is silent and very easy to misread.

The quickstart ships `ES_BULK_REFRESH_POLICY=WAIT_UNTIL`, which makes every
OpenSearch bulk write block until the next index refresh — about 3 seconds
regardless of batch size (we measured 1 event at 1893 ms and 14 events at
2803 ms). Under real ingestion volume the MAE consumer's batch processing then
exceeds `max.poll.interval.ms`, Kafka evicts it mid-batch, it rebalances,
replays the same batch against already-written documents, hits
`version_conflict_engine_exception`, fails the bulk, and never commits an
offset. It processes a few thousand messages and then stalls permanently.

Two things make this hard to spot:

- **Consumer lag looks frozen even while writes land**, because lag is measured
  against *committed* offsets. Ours sat at exactly 6,793 for ten minutes.
- **Most metadata still reads correctly.** Entity properties, owners and
  structured properties come from the aspect store, not the index — so the
  catalog looks healthy while only search and lineage are broken. Only
  `relationships` / `searchAcrossLineage` expose it.

The fix sets `ES_BULK_REFRESH_POLICY=NONE` and caps `max.poll.records` at 50 so
a batch always finishes well inside the poll interval:

```bash
python3 infra/fix_quickstart_consumer.py
cd ~/.datahub/quickstart
docker compose --profile quickstart -f docker-compose.yml -p datahub \
  up -d --force-recreate datahub-gms-quickstart
```

`docker compose` needs `DATAHUB_VERSION`, `UI_INGESTION_DEFAULT_CLI_VERSION`,
`DATAHUB_TOKEN_SERVICE_SALT` and `DATAHUB_TOKEN_SERVICE_SIGNING_KEY` in a
`.env` beside the compose file; the CLI supplies them but a bare
`docker compose` does not.

Measured effect: a 1,748-message backlog drained to zero in 140 s, and the
showcase-ecommerce datapack finished indexing at **1,267** entities rather than
stalling at 428.

## Seeding

From the repo root, with a tunnel to the VM
(`gcloud compute ssh comgu-datahub --ssh-flag="-L" --ssh-flag="18080:localhost:8080"`):

```bash
DATAHUB_GMS_URL=http://localhost:18080 python -m seed.commerce_lab
DATAHUB_GMS_URL=http://localhost:18080 python -m seed.verify
```

`verify` exits `COMMERCE_LAB_OK` once all five projections and five dataJobs
are reachable from the catalog through MCP.

Seeding emits in ordered phases. Structured property *definitions* must be
committed before any value references them, or DataHub rejects the value with
`Unexpected null value found for ... Structured Property Definition`; batching
does not preserve ordering on its own.

## Security

`METADATA_SERVICE_AUTH_ENABLED=false` in the quickstart, so GMS on `:8080`
accepts unauthenticated reads **and writes**. It must stay bound to localhost.
Before anything is exposed publicly, enable metadata service auth, issue a real
PAT for `DATAHUB_GMS_TOKEN`, and change the default `datahub`/`datahub` login.
