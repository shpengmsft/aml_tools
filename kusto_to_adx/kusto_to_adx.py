#!/usr/bin/env python3
"""Incrementally ingest Kusto query results into Azure Data Explorer.

The script is safe to run from Windows Task Scheduler: each execution processes
one or more closed time windows, skips rows already present in the target ADX
tables, and exits non-zero if any Kusto command fails.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

try:
    from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
except ModuleNotFoundError as exc:
    KustoClient = None
    KustoConnectionStringBuilder = None
    KUSTO_IMPORT_ERROR = exc
else:
    KUSTO_IMPORT_ERROR = None


DEFAULT_CLUSTER_URI = "https://shpeng-uksouth-adx.uksouth.kusto.windows.net"
DEFAULT_DATABASE = "ToolLatencyDashboards"
DEFAULT_TEMPLATE = Path(__file__).with_name("examples").joinpath("latency-dashboard-aggregation.kql")
DEFAULT_BUCKET_MINUTES = 10
DEFAULT_LOOKBACK_MINUTES = 60
DEFAULT_WINDOW_MINUTES = 60
DEFAULT_TARGET_TABLE = "LatencyDashboardMetrics"
DEFAULT_TARGET_TIMESTAMP_COLUMN = "BucketStart"

COMMAND_MARKERS = (
    ".set-or-append LatencyDashboardMetrics <|",
    ".set-or-append LatencyDashboardCacheMetrics <|",
    ".set-or-append LatencyDashboardAggregationWatermark <|",
)

TABLE_DEDUP_CONFIG = {
    "LatencyDashboardMetrics": {
        "time_column": "BucketStart",
        "keys": (
            "BucketStart",
            "Flow",
            "Segment",
            "Environment",
            "Region",
            "Status",
            "SourceQueryHash",
        ),
    },
    "LatencyDashboardCacheMetrics": {
        "time_column": "BucketStart",
        "keys": (
            "BucketStart",
            "Flow",
            "CacheName",
            "MetricName",
            "Environment",
            "Region",
            "Outcome",
            "SourceTelemetry",
        ),
    },
    "LatencyDashboardAggregationWatermark": {
        "time_column": "LastSuccessfulBucketStart",
        "inclusive_end": True,
        "keys": ("Flow", "LastSuccessfulBucketStart", "SourceQueryHash"),
    },
}


@dataclass(frozen=True)
class KustoCommand:
    table: str
    body: str


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime

    @property
    def label(self) -> str:
        return f"{format_kusto_datetime(self.start)}..{format_kusto_datetime(self.end)}"


def split_management_commands(template: str) -> list[KustoCommand]:
    try:
        starts = [template.index(marker) for marker in COMMAND_MARKERS]
    except ValueError as exc:
        markers = ", ".join(COMMAND_MARKERS)
        raise SystemExit(f"The KQL file must contain these management command markers: {markers}") from exc
    starts.append(len(template))

    commands: list[KustoCommand] = []
    for index, marker in enumerate(COMMAND_MARKERS):
        table = marker.split()[1]
        body = template[starts[index] : starts[index + 1]].strip()
        commands.append(KustoCommand(table=table, body=body + "\n"))
    return commands


def build_connection_string(args: argparse.Namespace) -> KustoConnectionStringBuilder:
    if KustoConnectionStringBuilder is None:
        raise SystemExit(
            "Missing dependency: azure-kusto-data. Install it with "
            "``."
        ) from KUSTO_IMPORT_ERROR

    if args.auth == "az-cli":
        return KustoConnectionStringBuilder.with_az_cli_authentication(args.cluster_uri)

    if args.auth == "app-key":
        tenant_id = args.tenant_id or os.getenv("AZURE_TENANT_ID")
        client_id = args.client_id or os.getenv("AZURE_CLIENT_ID")
        client_secret = args.client_secret or os.getenv("AZURE_CLIENT_SECRET")
        missing = [
            name
            for name, value in (
                ("--tenant-id or AZURE_TENANT_ID", tenant_id),
                ("--client-id or AZURE_CLIENT_ID", client_id),
                ("--client-secret or AZURE_CLIENT_SECRET", client_secret),
            )
            if not value
        ]
        if missing:
            raise SystemExit(f"{args.auth} auth requires {', '.join(missing)}")
        return KustoConnectionStringBuilder.with_aad_application_key_authentication(
            args.cluster_uri,
            client_id,
            client_secret,
            tenant_id,
        )

    # Managed identity is the production scheduler shape; the identity still needs
    # source ADX viewer and target dashboard DB ingestor permissions.
    if args.managed_identity_client_id:
        try:
            return KustoConnectionStringBuilder.with_aad_managed_service_identity_authentication(
                args.cluster_uri,
                client_id=args.managed_identity_client_id,
            )
        except TypeError:
            return KustoConnectionStringBuilder.with_aad_managed_service_identity_authentication(
                args.cluster_uri,
                args.managed_identity_client_id,
            )

    return KustoConnectionStringBuilder.with_aad_managed_service_identity_authentication(args.cluster_uri)


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def floor_datetime(value: datetime, bucket_minutes: int) -> datetime:
    value = value.astimezone(UTC)
    bucket_seconds = bucket_minutes * 60
    floored = int(value.timestamp()) // bucket_seconds * bucket_seconds
    return datetime.fromtimestamp(floored, UTC)


def format_kusto_datetime(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_identifier(value: str, argument_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise SystemExit(f"{argument_name} must be a simple Kusto identifier, got: {value!r}")
    return value


def first_value(response: object) -> object | None:
    primary_results = getattr(response, "primary_results", [])
    if not primary_results:
        return None
    table = primary_results[0]
    rows = getattr(table, "rows", None)
    if rows is not None:
        if not rows:
            return None
        row = rows[0]
    else:
        row = next(iter(table), None)
        if row is None:
            return None

    try:
        return row[0]
    except (KeyError, TypeError):
        return row["LatestTimestamp"]


def get_latest_target_timestamp(client: KustoClient, args: argparse.Namespace) -> datetime | None:
    table = validate_identifier(args.target_table, "--target-table")
    timestamp_column = validate_identifier(args.target_timestamp_column, "--target-timestamp-column")
    query = f'table("{table}") | summarize LatestTimestamp = max({timestamp_column})'
    value = first_value(client.execute(args.database, query))
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)

    value_text = str(value)
    if not value_text or value_text.lower() == "none":
        return None
    return parse_datetime(value_text)


def iter_windows(start: datetime, end: datetime, window_minutes: int) -> Iterable[TimeWindow]:
    current = start
    step = timedelta(minutes=window_minutes)
    while current < end:
        window_end = min(current + step, end)
        yield TimeWindow(start=current, end=window_end)
        current = window_end


def resolve_windows(args: argparse.Namespace, client: KustoClient) -> list[TimeWindow]:
    end = (
        floor_datetime(parse_datetime(args.end_time), args.bucket_minutes)
        if args.end_time
        else floor_datetime(datetime.now(UTC), args.bucket_minutes)
    )

    if args.start_time:
        start = floor_datetime(parse_datetime(args.start_time), args.bucket_minutes)
    else:
        latest_target_timestamp = get_latest_target_timestamp(client, args)
        if latest_target_timestamp is None:
            start = end - timedelta(minutes=args.lookback_minutes)
            print(
                f"{args.target_table}.{args.target_timestamp_column} has no latest timestamp; "
                f"falling back to --lookback-minutes {args.lookback_minutes}"
            )
        else:
            start = floor_datetime(latest_target_timestamp, args.bucket_minutes)
            print(
                f"using latest {args.target_table}.{args.target_timestamp_column} "
                f"{format_kusto_datetime(start)} as start time"
            )

    if start >= end:
        print(f"nothing to process: start {format_kusto_datetime(start)} >= end {format_kusto_datetime(end)}")
        return []
    return list(iter_windows(start, end, args.window_minutes))


def inject_time_window(command: str, window: TimeWindow) -> str:
    replacements = {
        r"let\s+aggregationStart\s*=\s*[^;]+;": f"let aggregationStart = datetime({format_kusto_datetime(window.start)});",
        r"let\s+aggregationEnd\s*=\s*[^;]+;": f"let aggregationEnd = datetime({format_kusto_datetime(window.end)});",
    }

    updated = command
    for pattern, replacement in replacements.items():
        updated = re.sub(pattern, replacement, updated)
    return updated


def find_statement_end(query: str, start: int) -> int:
    depth = 0
    quote: str | None = None
    index = start
    while index < len(query):
        char = query[index]
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue

        if char in ("'", '"'):
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(depth - 1, 0)
        elif char == ";" and depth == 0:
            return index + 1
        index += 1

    raise ValueError("Could not find the end of a top-level KQL statement")


def split_let_prefix(query: str) -> tuple[str, str]:
    position = 0
    while position < len(query):
        while position < len(query) and query[position].isspace():
            position += 1
        if not query[position:].startswith("let "):
            break
        position = find_statement_end(query, position)

    return query[:position].strip(), query[position:].strip()


def add_dedup_filter(command: KustoCommand, window: TimeWindow) -> str:
    config = TABLE_DEDUP_CONFIG.get(command.table)
    if not config:
        return inject_time_window(command.body, window)

    marker = f".set-or-append {command.table} <|"
    query = inject_time_window(command.body, window).removeprefix(marker).strip()
    let_prefix, final_expression = split_let_prefix(query)
    keys = ", ".join(config["keys"])
    time_column = config["time_column"]
    end_operator = "<=" if config.get("inclusive_end") else "<"

    return f"""{marker}
{let_prefix}
let CandidateRowsForDedup = materialize(
{final_expression}
);
CandidateRowsForDedup
| join kind=leftanti (
    {command.table}
    | where {time_column} >= datetime({format_kusto_datetime(window.start)})
        and {time_column} {end_operator} datetime({format_kusto_datetime(window.end)})
    | summarize by {keys}
) on {keys}
"""


def response_row_count(primary_results: Iterable[object]) -> int:
    total = 0
    for table in primary_results:
        rows = getattr(table, "rows", None)
        if rows is not None:
            total += len(rows)
        else:
            total += sum(1 for _ in table)
    return total


def normalize_config_key(key: str) -> str:
    return key.replace("-", "_")


def load_config(config_path: Path | None) -> dict[str, object]:
    if config_path is None:
        return {}

    with config_path.open(encoding="utf-8") as config_file:
        config = json.load(config_file)

    if not isinstance(config, dict):
        raise SystemExit(f"Config file must contain a JSON object: {config_path}")

    normalized = {normalize_config_key(str(key)): value for key, value in config.items()}
    if "template" not in normalized and "kql" in normalized:
        normalized["template"] = normalized["kql"]

    if "template" in normalized:
        template = Path(str(normalized["template"]))
        if not template.is_absolute():
            template = config_path.parent / template
        normalized["template"] = template

    return normalized


def config_value(config: dict[str, object], key: str, default: object) -> object:
    return config.get(key, default)


def parse_args() -> argparse.Namespace:
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", type=Path, help="JSON config file with saved command-line parameters.")
    config_args, remaining_args = config_parser.parse_known_args()
    config = load_config(config_args.config)

    parser = argparse.ArgumentParser(
        description="Incrementally ingest KQL query results into a target ADX database.",
        parents=[config_parser],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.set_defaults(config=config_args.config)
    parser.add_argument("--cluster-uri", default=config_value(config, "cluster_uri", DEFAULT_CLUSTER_URI))
    parser.add_argument("--database", default=config_value(config, "database", DEFAULT_DATABASE))
    parser.add_argument(
        "--target-table",
        default=config_value(config, "target_table", DEFAULT_TARGET_TABLE),
        help="Target ADX table used to discover the default start time.",
    )
    parser.add_argument(
        "--target-timestamp-column",
        default=config_value(config, "target_timestamp_column", DEFAULT_TARGET_TIMESTAMP_COLUMN),
        help="Timestamp column in --target-table used to discover the default start time.",
    )
    parser.add_argument(
        "--kql",
        "--template",
        dest="template",
        type=Path,
        default=config_value(config, "template", DEFAULT_TEMPLATE),
    )
    parser.add_argument(
        "--start-time",
        default=config_value(config, "start_time", None),
        help="Inclusive UTC backfill start, for example 2026-06-11T00:00:00Z.",
    )
    parser.add_argument(
        "--end-time",
        default=config_value(config, "end_time", None),
        help="Exclusive UTC backfill end, for example 2026-06-12T00:00:00Z.",
    )
    parser.add_argument("--lookback-minutes", type=int, default=config_value(config, "lookback_minutes", DEFAULT_LOOKBACK_MINUTES))
    parser.add_argument("--window-minutes", type=int, default=config_value(config, "window_minutes", DEFAULT_WINDOW_MINUTES))
    parser.add_argument("--bucket-minutes", type=int, default=config_value(config, "bucket_minutes", DEFAULT_BUCKET_MINUTES))
    parser.add_argument(
        "--write-watermark-for-backfill",
        action="store_true",
        default=bool(config_value(config, "write_watermark_for_backfill", False)),
        help="Also write watermark rows for explicit --start-time/--end-time backfills.",
    )
    parser.add_argument(
        "--no-write-watermark-for-backfill",
        action="store_false",
        dest="write_watermark_for_backfill",
        help="Disable watermark writes for explicit --start-time/--end-time backfills.",
    )
    parser.add_argument(
        "--auth",
        choices=("az-cli", "managed-identity", "app-key"),
        default=config_value(config, "auth", "az-cli"),
        help="Authentication mode. Use az-cli for delegated manual refreshes.",
    )
    parser.add_argument(
        "--managed-identity-client-id",
        default=config_value(config, "managed_identity_client_id", None),
        help="Optional user-assigned managed identity client ID for --auth managed-identity.",
    )
    parser.add_argument(
        "--tenant-id",
        default=config_value(config, "tenant_id", None),
        help="Tenant ID for --auth app-key; defaults to AZURE_TENANT_ID.",
    )
    parser.add_argument(
        "--client-id",
        default=config_value(config, "client_id", None),
        help="Application client ID for --auth app-key; defaults to AZURE_CLIENT_ID.",
    )
    parser.add_argument(
        "--client-secret",
        default=config_value(config, "client_secret", None),
        help="Application client secret for --auth app-key; defaults to AZURE_CLIENT_SECRET.",
    )
    return parser.parse_args(remaining_args)


def main() -> None:
    args = parse_args()
    template = args.template.read_text()
    commands = split_management_commands(template)

    client = KustoClient(build_connection_string(args))
    windows = resolve_windows(args, client)
    is_backfill = bool(args.start_time or args.end_time)
    for window in windows:
        print(f"processing window {window.label}")
        for command in commands:
            if (
                is_backfill
                and command.table == "LatencyDashboardAggregationWatermark"
                and not args.write_watermark_for_backfill
            ):
                print(f"{command.table} skipped for backfill window")
                continue
            deduped_command = add_dedup_filter(command, window)
            response = client.execute_mgmt(args.database, deduped_command)
            rows = response_row_count(response.primary_results)
            print(f"{command.table} succeeded; response rows={rows}")


if __name__ == "__main__":
    main()
