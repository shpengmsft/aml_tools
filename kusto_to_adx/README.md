# Kusto to ADX

Incrementally runs Kusto/KQL aggregation commands and appends only new result rows into a target Azure Data Explorer database.

The tool is designed for Windows Task Scheduler, local delegated runs, or a future Azure Function/Container Job. By default, it discovers the latest timestamp from a target ADX table and uses that as the next start time; the end time defaults to current UTC time.

## Install

```bash
python -m pip install -r requirements.txt
```

## Scheduled incremental run

```bash
python kusto_to_adx.py \
  --auth az-cli \
  --cluster-uri https://shpeng-uksouth-adx.uksouth.kusto.windows.net \
  --database ToolLatencyDashboards \
  --target-table LatencyDashboardMetrics \
  --target-timestamp-column BucketStart \
  --kql examples/latency-dashboard-aggregation.kql
```

If `LatencyDashboardMetrics.BucketStart` has records, the script uses `max(BucketStart)` as `aggregationStart` and current UTC time as `aggregationEnd`. If the target table has no timestamp yet, it falls back to `--lookback-minutes`.

## Config file

Use a config file to save stable command-line parameters:

```bash
cp config.example.json config.local.json
python kusto_to_adx.py --config config.local.json
```

`config.local.json` can contain the same options as the command line, using either snake_case or kebab-case names:

```json
{
  "cluster_uri": "https://shpeng-uksouth-adx.uksouth.kusto.windows.net",
  "database": "ToolLatencyDashboards",
  "target_table": "LatencyDashboardMetrics",
  "target_timestamp_column": "BucketStart",
  "kql": "examples/latency-dashboard-aggregation.kql",
  "auth": "az-cli",
  "lookback_minutes": 60,
  "window_minutes": 60,
  "bucket_minutes": 10
}
```

Command-line arguments override config values, so this is valid for a one-off backfill:

```bash
python kusto_to_adx.py --config config.local.json --start-time 2026-06-12T00:00:00Z --end-time 2026-06-12T04:00:00Z
```

## Backfill

```bash
python kusto_to_adx.py \
  --auth az-cli \
  --kql examples/latency-dashboard-aggregation.kql \
  --start-time 2026-06-12T00:00:00Z \
  --end-time 2026-06-12T04:00:00Z \
  --window-minutes 60
```

Backfill skips watermark writes by default so freshness does not move backward. Use `--write-watermark-for-backfill` only when intentionally repairing watermark rows.

## Windows Task Scheduler

Create a task that runs `python.exe` with arguments like:

```text
C:\path\to\aml_tools\kusto_to_adx\kusto_to_adx.py --config C:\path\to\aml_tools\kusto_to_adx\config.local.json
```

Set the task's working directory to:

```text
C:\path\to\aml_tools\kusto_to_adx
```

For `--auth app-key`, set these environment variables for the scheduled-task account instead of putting secrets in task arguments:

```text
AZURE_TENANT_ID
AZURE_CLIENT_ID
AZURE_CLIENT_SECRET
```

The identity must have source ADX read access and target ADX ingestor access.

## KQL file shape

The KQL file must contain `.set-or-append <TargetTable> <|` management commands. The runner splits the file by target command, injects `aggregationStart` / `aggregationEnd`, materializes candidate rows, and left-anti joins against existing target rows before append.

The included `examples/latency-dashboard-aggregation.kql` is the current latency dashboard aggregation template.
