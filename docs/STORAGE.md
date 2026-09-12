# SQLite retention and maintenance

ArbSync keeps history until the operator explicitly prunes it. SQLite contains
canonical opportunities and approximate minute rollups, not order books. Back up
valuable history before maintenance. Use the database path from the selected config;
relative runtime paths are resolved beside that config.

## Capacity planning

The [September 8 modeled burst runs](../artifacts/benchmarks/performance/dedupe-perf-20260908T102410Z.json)
stored 615 rows in 0.098 MiB at 110 input events/s, 5,015 rows in 0.668–0.672 MiB
at 1,100 events/s, and 24,571 rows in 3.254 MiB at 5,500 events/s. Each included
five warmup seconds, twenty measured seconds, and drain time. Including those periods,
observed storage rates were approximately 23, 189, and 862 rows/s respectively.
These short, deliberately opportunity-heavy synthetic runs are planning examples;
their old harness predates the quote-currency correction and is not current live evidence.

The files used roughly 139–167 bytes per canonical row including indexes and the
short-run rollups. At a sustained 189 rows/s, 150 bytes/row suggests about 2.28 GiB/day;
at 862 rows/s, about 10.40 GiB/day. Decimal string lengths, pair distribution, free
pages, WAL size, and longer-lived rollups change this estimate. Live smoke runs with
zero opportunities cannot establish a useful nonzero storage-growth rate. Measure
your own row-count and database-plus-WAL growth over a representative interval and
budget extra space for backups, WAL/checkpoints, and vacuum before selecting retention.

## Explicit retention

Run from the repository root (or use the installed `arbsync-prune` command):

```text
uv run arbsync-prune --database var/arb.sqlite3 --before 2026-09-01T00:00:00Z --batch-size 1000 --max-batches 10
```

This permanently removes rows strictly older than the supplied timezone-qualified
timestamp. Rows exactly on the boundary remain. The command prints per-batch and
cumulative committed deletions. With no batch options, it attempts just one batch
of at most 1,000 rows. A full final batch may leave older rows; rerun to continue.
Choose the cutoff explicitly; this example is not a recommended retention duration.

Each transaction deletes at most 10,000 canonical rows and rebuilds only affected
minute/pair rollups from survivors. This keeps counts, sums, and maxima correct even
when the cutoff or batch splits a minute. Both tables commit together or roll back.
All-time statistics now describe retained history; deleted peaks are intentionally lost.

The command runs separately from ingestion. Its default lock wait and SQLite query
work budget are 0.5 seconds (`--timeout-seconds`, maximum 2 seconds), with 0.1 seconds
between batches. Lock contention, query-budget expiry, and SQL failures stop the command
with a nonzero exit code; the failing batch rolls back while earlier batches remain
committed. Dense-minute rebuilding may exceed the budget: stop the backend and retry
with a smaller batch or a budget up to two seconds. Filesystem stalls and rollback I/O
cannot be bounded by SQLite's query progress callback. Monitor persistence queue drops
when maintaining a live database; schedule large cleanups during downtime if needed.

Late writes with older timestamps can reintroduce expired history after a batch, so
repeat maintenance periodically. Configure an OS scheduler with an explicit database,
cutoff, and bounded batch count if unattended pruning is needed. Never run this command
inside the ingestion event loop. A misspelled path fails without creating a new database.

## Backup, checkpoints, and vacuum

Use SQLite's online backup API or the SQLite CLI `.backup` command to create a consistent
copy, for example `sqlite3 var/arb.sqlite3 ".backup 'var/arb-backup.sqlite3'"` with a
new destination filename. The SQLite CLI is a separate prerequisite. Do not copy only
the main file while the backend is live: committed data may still be in the `-wal` file.
Verify the backup with `sqlite3 var/arb-backup.sqlite3 "PRAGMA integrity_check;"` and
occasionally test a restore in an isolated directory. Protect backups like the originals.

Pruning makes pages reusable but does not normally shrink the file. WAL checkpoints
transfer committed changes into the main file; they are not backups. For a planned
disk-reclamation window, stop the backend and other database users, back up, then run
`sqlite3 var/arb.sqlite3 "PRAGMA wal_checkpoint(TRUNCATE); VACUUM; PRAGMA integrity_check;"`.
Allow roughly twice the database size in free space for VACUUM. It can take substantial
time and acquire locks, so do not run it automatically on the market-data path.

## Migration and restore

The pruner requires schema version 2 and never migrates a database. Application startup
owns migrations. The quote-currency migration discards legacy conflated USD/USDT history;
back up before upgrading if that historical data must be retained for investigation.
Restore only while all database users are stopped, into a fresh directory with no old
WAL/SHM sidecars, and point an explicit config at the restored file. Verify integrity
and application statistics before resuming service. Do not combine a restored main file
with WAL files from a different database generation.

SQLite references: [online backup](https://www.sqlite.org/backup.html),
[VACUUM](https://www.sqlite.org/lang_vacuum.html), and
[query progress deadlines](https://www.sqlite.org/c3ref/progress_handler.html).
