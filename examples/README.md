# examples

Real output from one Comgu run (`08b4644c4f5e47819c886cc1b5231e08`) against a live DataHub Core
instance. Regenerate with:

```bash
python -m apps.api.scripts.golden_path --remediate --json
```

| File | What it shows |
| --- | --- |
| `findings.md` | The 6 findings, each with expected/observed values, customer impact and evidence |
| `generated-diff.patch` | The 5-file patch, produced from registered templates |
| `validation-output.txt` | Real `pytest` run against the patched workspace |
| `datahub-writeback.json` | Catalog writes and their read-back verification |
| `mcp-tool-trace.json` | Every DataHub MCP call: arguments, duration, result summary |

`mcp-tool-trace.json` is the one worth reading first — it is the evidence that
the blast radius came from DataHub lineage rather than from hardcoded topology.
