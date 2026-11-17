# Measurements

How the 0.7.0 budgets were measured, on what, and what they do not prove.
Regenerate with `uv run python scripts/measure_state.py [--scale N ...]`.
All fixtures are disposable local bare remotes on this machine
(macOS, local disk); hosted latency was not measured.

## Method

- Tokenizer: `o200k_base` via tiktoken (`TrackerService.count_tokens`;
  `chars/4` fallback only when tiktoken is absent — it was present here).
- Counted surfaces are actual serialized payloads: the committed
  `scripts/mcp_snapshots/tools.json` plus `INSTRUCTIONS` for startup, and
  `json.dumps` of real tool results for list/show/take/finish.
- Seeding writes one commit per scale (`scripts/measure_state.py seed`);
  publication latency is measured separately on sampled single-operation
  publishes (create, take, finish). Seeding is not the publication path.
- Scales: 100 and 10,000 items.

## Token budgets (measured 2026-09-08)

| Surface | Measured | Target |
| --- | --- | --- |
| Startup: 10 tool schemas + instructions | 3,150 tokens | — (was 3,344 for 26 tool defs alone, 4,688 for 38) |
| Five list rows | 288–316 tokens | ~300 |
| One take with work context | 128–137 tokens (small fixture) | ~1,500 for a full task context |
| One finish acknowledgement | ≤ 100 tokens (asserted in `test_mcp_slim.py`) | ~100 |

Targets are benchmark acceptance targets, not universal guarantees: token
counts depend on task content, and multibyte text is counted as encoded.
Every response additionally honors a hard 16 KiB byte ceiling on its final
serialization. A 60 KB description stays bounded: `show_item` spills it to
`field`/`offset`/`budget` section reads with guaranteed progress
(`tests/test_service.py`, `tests/test_mcp_slim.py`).

## Latency and Git growth (local disk)

Measured 2026-10-17 with the object-store cache
([ADR 0011](adr/0011-git-object-store-cache.md)), alongside the same script
run on the previous release (0.9.1) on the same machine minutes apart.

| Metric | 100 items | 10,000 items | 0.9.1, 100 items | 0.9.1, 10,000 items |
| --- | --- | --- | --- | --- |
| Seed (one commit) | 0.21 s | 71.6 s | 0.23 s | 67.2 s |
| 5-row list page (first read) | 0.13 s | 0.21 s | 0.14–0.17 s | 2.54 s |
| One show | 0.04 s | 0.10 s | 0.02 s | 0.41 s |
| Single publish (create/take/finish) | ~0.20 s | ~0.41 s | ~0.27 s | ~3.3 s |
| Bare-remote growth per item | ~45 B | ~47 B | ~45 B | ~47 B |

Each read and write fetches only the objects its namespace's store
lacks, so the per-operation cost no longer grows with the branch's
history. At 100 items a show costs about 20 ms more than 0.9.1: three
local git commands replace reading a few files, which is within the
0.11 s recorded for 0.7.0. Seeding is one large commit and is dominated
by validation and hashing, not the cache.

## Write latency under host load

Measured 2026-11-17 on a shared macOS host with no load added for the test
(load average 86 to 369 from other sessions). The fixture was a bare remote
cloned from the BenchBox state branch (4,605 items, 12,900+ commits) with an
empty cache, so the remote is local disk and GitHub round trips are not
included. Each of 8 cycles ran `create_item`, `take`, `update_item` with
`context_append` and `finish`. With 8 samples the p95 equals the maximum.
Stage times come from the `tractable.git` log line each write now emits.

| Run (load average 1/5/15 min) | `take` p50 / p95 | `finish` p50 / p95 |
| --- | --- | --- |
| 1.1.1 (171/202/154) | 8.5 s / 13.5 s | 7.8 s / 26.6 s |
| 1.1.1 (110/128/136) | 12.8 s / 18.2 s | 13.4 s / 52.8 s |
| This change (369/294/228) | 8.2 s / 14.1 s | 9.0 s / 20.0 s, then 17.5 s on retry |

The runs were back to back but the host load differed between them, so the
rows show the size of the effect, not a controlled ratio.

Where the time goes, from the stage log of 32 writes:

- Network-shaped stages are small against a local remote: fetch about 0.15 s,
  push about 0.2 s, the confirming `ls-remote` about 0.06 s. GitHub round
  trips were not measured here and add to these.
- Most of a write is local CPU over the whole tracker: loading the snapshot
  (2 to 3 s), `apply` (1 to 5 s, mostly repeated `validate_snapshot`) and
  `write_tree` (3 to 5 s, serializing every detail file). The Python part is
  about 1.3 s of CPU on this fixture (validation 0.7 s, serialization 0.45 s,
  parsing 0.15 s) before the git subprocesses, so under a load average above
  100 each write sits at 8 to 19 s and a single descheduling pushes it past
  30 s.
- The tail is lock contention. Background cache maintenance (`repack -A`) holds
  the store lock exclusively, and every read and write waits behind it (the
  cache's `maintenance.stamp` was written during each run): one
  `finish` waited 36 s for the lock (49 s in total). The 1.1.1 runs show the
  same tail (a 52.8 s `finish`), but 1.1.1 logs no stage times to attribute it.
  The write had not started when the client gave up, so nothing was lost, but
  the client could not tell that from a lost reply.
- A cold cache's first fetch of the whole history took 100 to 139 s.

This change makes writes safe to repeat (`operation_id`), makes the lock wait
end at 20 s with `E_STATE` and nothing changed instead of running past the
client timeout, drops the history search for generated ids (about 0.3 s on
this fixture) and cuts the control-character checks that dominated
`validate_snapshot` (0.18 s to 0.05 s of CPU per validation, run four times
per write). Serializing every detail file in `write_tree` (about 0.45 s of
CPU) is unchanged.

## Local cache size

Measured 2026-10-18 against BenchBox state (3,437 items, 5,100+
commits): read-only against the production remote, and with writes
against a scratch copy of that state. Each pair was run back to back
with the published 0.9.1 wheel in an isolated environment.

| Measure | 0.9.1 snapshot cache | Object store |
| --- | --- | --- |
| Cache after reading the current tip | 31 MiB (one snapshot folder) | 15 MiB (the whole history) |
| Cache after three further tips | 122.7 MiB, growing ~30 MiB per tip forever | 15.0 MiB |
| First `validate` for a namespace | 2.8 s (depth-1 fetch of the tip) | 3.7 s (full history, once) |
| Repeat `validate` at the same tip | 0.61 s | 0.60 s |
| `validate` after a new tip (scratch copy) | 1.7 s | 0.37 s |
| Growth per one-item edit, after shipped maintenance | ~30 MiB | 0.72× the bytes touched (20 edits) |
| Unpacked slack before maintenance | — | ~180 KiB per commit; maintenance at 8 MiB, inline above 16 MiB |

"Bytes touched" is the edited item's detail file plus its index entry.
With full delta recomputation (`repack -adf`, not what ships) the same
20 edits grow the store by 0.14×.

Every commit also carries a fixed record: the commit object with its
trailers, two tree deltas, an `index.json` delta and pack index entries.
Title-only edits (about 140 B touched) measured 510 B per commit at 300
items and 900 B at 3,000 items, so for tiny edits growth is 3.6 to 6.4
times the bytes touched. The supported bound is therefore about 2 times
the bytes touched plus about 1 KiB per commit
([ADR 0011](adr/0011-git-object-store-cache.md)). The test suite asserts
it both ways (`test_size_per_interaction_is_within_twice_the_items_touched`
at 0.21× on a fixture with realistic items, and
`test_small_edits_cost_a_bounded_fixed_overhead_per_commit` for small
edits).

Before maintenance, a fetched tip costs 150 to 300 KB, because every
commit rewrites `index.json` and the `items` tree; maintenance folds
those into deltas.

## Footprint

| Tree | 0.6.x baseline | 0.7.0 |
| --- | --- | --- |
| `src/todo_db` Python | 8,563 lines + 237 migration lines | 3,101 lines, no migrations |
| `tests/` | 7,870 lines | 1,134 lines |
| MCP tools | 26 default / 38 full | 9 + `get_instructions` |
| Runtime dependencies | libsql, cryptography, pyyaml (extras) | none beyond the `mcp` extra |
| Dev dependencies | + libsql, cryptography, pyyaml | + tiktoken (budget measurement) |

Test lines fell because the removed suite tested the removed product
(hosted backends, audit chain, findings, workflow gates, SQL budgets).
Retained behaviors — readiness, priority ordering, cycle rejection,
one-winner claims, bounded output, migration mapping — are ported into
the new suite, not deleted. 75 tests pass: `uv run pytest -q`.
