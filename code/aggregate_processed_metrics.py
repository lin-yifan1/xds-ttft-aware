from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

from config import AGGREGATED_SQLITE_PATH, DEFAULT_AGGREGATION_GRANULARITY, PROCESSED_SQLITE_PATH
from process_data2_to_excel import SQL_COLUMNS, SQL_TABLE, cleanup_sqlite_files

AGGREGATED_TABLE = "aggregated_metrics"
AGGREGATED_COLUMNS = [
    "infer_service_id",
    "service_name",
    "domain_id",
    "rpm",
    "tpm",
    "ttft_avg",
    "tpot_avg",
    "prompt_tokens",
    "completion_tokens",
    "collect_time_std",
]


def parse_bucket_freq(granularity: str) -> str:
    value = str(granularity).strip().lower().replace("_", "").replace(" ", "")
    mapping = {
        "hour": "1h",
        "hours": "1h",
        "1hour": "1h",
        "1hours": "1h",
        "1h": "1h",
        "h": "1h",
        "halfhour": "30min",
        "minute": "1min",
        "minutes": "1min",
        "min": "1min",
    }
    if value in mapping:
        return mapping[value]

    minute_match = re.fullmatch(r"([1-9]\d*)(?:m|min|minute|minutes)", value)
    if minute_match:
        return f"{int(minute_match.group(1))}min"

    raise ValueError(
        f"Unsupported granularity: {granularity}. "
        "Use 1h or a positive minute bucket such as 1min, 5min, 10min, 30min."
    )


def bucket_freq_to_seconds(bucket_freq: str) -> int:
    if bucket_freq == "1h":
        return 3600

    minute_match = re.fullmatch(r"([1-9]\d*)min", bucket_freq)
    if minute_match:
        return int(minute_match.group(1)) * 60

    raise ValueError(f"Unsupported parsed bucket frequency: {bucket_freq}")


def validate_input_database(conn: sqlite3.Connection, input_path: Path) -> None:
    table_exists = conn.execute(
        "SELECT 1 FROM src.sqlite_master WHERE type = 'table' AND name = ?",
        (SQL_TABLE,),
    ).fetchone()
    if not table_exists:
        raise ValueError(f"SQLite input {input_path} does not contain table {SQL_TABLE!r}.")

    columns = {str(row[1]) for row in conn.execute(f"PRAGMA src.table_info({SQL_TABLE})")}
    missing = [col for col in SQL_COLUMNS if col not in columns]
    if missing:
        raise ValueError(f"SQLite input {input_path} is missing required columns: {missing}")


def create_output_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE {AGGREGATED_TABLE} (
            infer_service_id TEXT NOT NULL,
            service_name TEXT NOT NULL,
            domain_id TEXT NOT NULL,
            rpm REAL NOT NULL,
            tpm REAL NOT NULL,
            ttft_avg REAL NOT NULL,
            tpot_avg REAL NOT NULL,
            prompt_tokens REAL NOT NULL,
            completion_tokens REAL NOT NULL,
            collect_time_std TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE aggregation_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def insert_aggregated_rows(conn: sqlite3.Connection, bucket_seconds: int) -> None:
    conn.execute(
        f"""
        INSERT INTO {AGGREGATED_TABLE} ({", ".join(AGGREGATED_COLUMNS)})
        WITH bucketed AS (
            SELECT
                infer_service_id,
                service_name,
                domain_id,
                rpm,
                tpm,
                ttft_avg,
                tpot_avg,
                prompt_tokens,
                completion_tokens,
                datetime(
                    CAST(CAST(strftime('%s', collect_time_std) AS INTEGER) / ? AS INTEGER) * ?,
                    'unixepoch'
                ) AS bucket_time
            FROM src.{SQL_TABLE}
        ),
        grouped AS (
            SELECT
                infer_service_id,
                service_name,
                domain_id,
                bucket_time,
                SUM(rpm) AS rpm_sum,
                SUM(tpm) AS tpm_sum,
                SUM(CASE WHEN rpm > 0 AND ttft_avg != 0 THEN ttft_avg * rpm ELSE 0 END) AS ttft_weighted_sum,
                SUM(CASE WHEN rpm > 0 AND ttft_avg != 0 THEN rpm ELSE 0 END) AS ttft_weight_sum,
                SUM(CASE WHEN rpm > 0 AND tpot_avg != 0 THEN tpot_avg * rpm ELSE 0 END) AS tpot_weighted_sum,
                SUM(CASE WHEN rpm > 0 AND tpot_avg != 0 THEN rpm ELSE 0 END) AS tpot_weight_sum,
                SUM(CASE WHEN rpm > 0 THEN prompt_tokens * rpm ELSE 0 END) AS prompt_weighted_sum,
                SUM(CASE WHEN rpm > 0 THEN completion_tokens * rpm ELSE 0 END) AS completion_weighted_sum,
                SUM(CASE WHEN rpm > 0 THEN rpm ELSE 0 END) AS token_weight_sum
            FROM bucketed
            WHERE
                infer_service_id != ''
                AND service_name != ''
                AND domain_id != ''
                AND bucket_time IS NOT NULL
            GROUP BY infer_service_id, service_name, domain_id, bucket_time
        )
        SELECT
            infer_service_id,
            service_name,
            domain_id,
            rpm_sum,
            tpm_sum,
            CASE WHEN ttft_weight_sum > 0 THEN ttft_weighted_sum / ttft_weight_sum ELSE 0.0 END AS ttft_avg,
            CASE WHEN tpot_weight_sum > 0 THEN tpot_weighted_sum / tpot_weight_sum ELSE 0.0 END AS tpot_avg,
            CASE WHEN token_weight_sum > 0 THEN prompt_weighted_sum / token_weight_sum ELSE 0.0 END AS prompt_tokens,
            CASE WHEN token_weight_sum > 0 THEN completion_weighted_sum / token_weight_sum ELSE 0.0 END AS completion_tokens,
            bucket_time AS collect_time_std
        FROM grouped
        ORDER BY infer_service_id, service_name, domain_id, bucket_time
        """,
        (bucket_seconds, bucket_seconds),
    )


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE INDEX idx_aggregated_group_order
        ON {AGGREGATED_TABLE} (infer_service_id, service_name, domain_id, collect_time_std)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX idx_aggregated_group_lookup
        ON {AGGREGATED_TABLE} (infer_service_id, service_name)
        """
    )


def insert_metadata(
    conn: sqlite3.Connection,
    input_path: Path,
    bucket_freq: str,
    bucket_seconds: int,
    source_rows: int,
    output_rows: int,
    service_groups: int,
) -> None:
    rows = {
        "source_path": str(input_path),
        "granularity": bucket_freq,
        "bucket_seconds": str(bucket_seconds),
        "source_rows": str(source_rows),
        "output_rows": str(output_rows),
        "service_groups": str(service_groups),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    conn.executemany(
        "INSERT INTO aggregation_metadata (key, value) VALUES (?, ?)",
        rows.items(),
    )


def aggregate_sqlite_database(input_path: Path, output_path: Path, granularity: str) -> Path:
    bucket_freq = parse_bucket_freq(granularity)
    bucket_seconds = bucket_freq_to_seconds(bucket_freq)
    input_path = input_path.resolve()
    output_path = output_path.resolve()

    if not input_path.exists():
        raise FileNotFoundError(f"SQLite input not found: {input_path}")
    if input_path == output_path:
        raise ValueError("Input and output SQLite paths must be different.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f"{output_path.stem}.tmp{output_path.suffix}")
    cleanup_sqlite_files(temp_path)

    print(f"[progress] start sqlite aggregation input={input_path} output={output_path} bucket={bucket_freq}")
    conn = sqlite3.connect(temp_path)
    success = False
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute("PRAGMA cache_size=-65536")
        conn.execute("ATTACH DATABASE ? AS src", (str(input_path),))
        validate_input_database(conn, input_path)

        source_rows = int(conn.execute(f"SELECT COUNT(*) FROM src.{SQL_TABLE}").fetchone()[0])
        service_groups = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM (
                    SELECT 1
                    FROM src.{SQL_TABLE}
                    GROUP BY infer_service_id, service_name
                )
                """
            ).fetchone()[0]
        )
        print(f"[progress] source rows={source_rows} service_groups={service_groups}")

        create_output_schema(conn)
        insert_aggregated_rows(conn, bucket_seconds)
        create_indexes(conn)

        output_rows = int(conn.execute(f"SELECT COUNT(*) FROM {AGGREGATED_TABLE}").fetchone()[0])
        if output_rows == 0:
            raise ValueError("No rows were produced by aggregation.")

        insert_metadata(conn, input_path, bucket_freq, bucket_seconds, source_rows, output_rows, service_groups)
        conn.commit()
        conn.execute("DETACH DATABASE src")
        success = True
    finally:
        conn.close()
        if not success:
            cleanup_sqlite_files(temp_path)

    cleanup_sqlite_files(output_path)
    temp_path.replace(output_path)
    print(f"[ok] wrote {output_path} rows={output_rows} service_groups={service_groups}")
    return output_path


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Aggregate processed metrics SQLite by configurable time buckets.")
    ap.add_argument("--input", type=Path, default=PROCESSED_SQLITE_PATH, help="Input processed SQLite path.")
    ap.add_argument("--output", type=Path, default=AGGREGATED_SQLITE_PATH, help="Output aggregated SQLite path.")
    ap.add_argument(
        "--granularity",
        default=DEFAULT_AGGREGATION_GRANULARITY,
        help="Bucket size: 1h or any positive minute bucket, for example 1min, 5min, 10min, 30min.",
    )
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    aggregate_sqlite_database(args.input, args.output, args.granularity)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
