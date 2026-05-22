"""通用场景运行器：读取 ``tests/scenarios/<name>/`` 下的 CSV + scenario.json，
调用 ``analyze_cluster`` 并打印关键字段。

用法：

    uv run python tests/run_scenario.py tests/scenarios/ttft_only_new_user/

scenario 目录约定与 CSV/JSON schema 见 ``tests/scenarios/README.md``。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "code"))

from config import LatencyDetectorConfig  # noqa: E402
from maas_monitor_cli import analyze_cluster  # noqa: E402


TZ = ZoneInfo("Asia/Shanghai")
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def load_rows_from_csv(path: Path) -> list[dict[str, Any]]:
    """读 CSV 并转成 MaaS Monitor API 的 row 字典格式（``timestamp`` 为 epoch 字符串）。"""
    rows: list[dict[str, Any]] = []
    with Path(path).open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = datetime.strptime(row["collect_time_std"], TIMESTAMP_FORMAT).replace(tzinfo=TZ)
            rows.append(
                {
                    "timestamp": str(int(ts.timestamp())),
                    "domain_id": row["domain_id"],
                    "rpm": row["rpm"],
                    "tpm": row["tpm"],
                    "ttft": row["ttft"],
                    "tpot": row["tpot"],
                    "prompt_tokens": row["prompt_tokens"],
                    "completion_tokens": row["completion_tokens"],
                }
            )
    return rows


class StubMaasClient:
    """duck-typed 替代 ``MaasMonitorClient``，按 ``[start, end)`` 时间窗过滤一批 rows。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: list[tuple[datetime, datetime]] = []

    def query_metrics(
        self,
        start: datetime,
        end: datetime,
        infer_service_id: str,
        model_name: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        self.calls.append((start, end))
        start_ts = int(start.timestamp())
        end_ts = int(end.timestamp())
        filtered = [r for r in self._rows if start_ts <= int(r["timestamp"]) < end_ts]
        return filtered, 1


def _parse_reported_at(value: str) -> datetime:
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=TZ)


def run_scenario(scenario_dir: Path) -> dict[str, Any]:
    """读取场景目录下的 scenario.json + history.csv + current.csv，跑 analyze_cluster 并打印摘要。"""
    scenario_dir = Path(scenario_dir)
    meta_path = scenario_dir / "scenario.json"
    history_path = scenario_dir / "history.csv"
    current_path = scenario_dir / "current.csv"

    with meta_path.open(encoding="utf-8") as f:
        meta = json.load(f)

    reported_at = _parse_reported_at(meta["reported_at"])
    cfg = LatencyDetectorConfig(**(meta.get("config") or {}))

    rows = load_rows_from_csv(history_path) + load_rows_from_csv(current_path)
    client = StubMaasClient(rows)

    result = analyze_cluster(
        client,
        reported_at,
        infer_service_id=meta["infer_service_id"],
        model_name=meta.get("model_name"),
        cfg=cfg,
        history_days=int(meta.get("history_days", 14)),
        window_before_minutes=int(meta.get("window_before_minutes", 10)),
        window_after_minutes=int(meta.get("window_after_minutes", 10)),
    )

    summary: dict[str, Any] = {
        "status": result.get("status"),
        "decision_source": result.get("decision_source"),
        "query_count": result.get("query_count"),
        "events": result.get("events", []),
        "event_count": len(result.get("event_reports", [])),
        "calls": [{"start": str(s), "end": str(e)} for s, e in client.calls],
    }
    for idx, report in enumerate(result.get("event_reports", []) or []):
        summary[f"event_{idx}"] = {
            "rootcause_scope": report.get("rootcause_scope"),
            "duration_hours": report.get("duration_hours"),
            "event_variant": report.get("event_variant"),
            "driver_signal": report.get("driver_signal"),
            "traffic_driver_ratio": round(float(report.get("traffic_driver_ratio") or 0.0), 3),
            "length_driver_ratio": round(float(report.get("length_driver_ratio") or 0.0), 3),
            "new_join_users": [u["user_id"] for u in report.get("new_join_users", []) or []],
            "culprits": [
                {
                    "user_id": c["user_id"],
                    "score_ratio": round(float(c["score_ratio"]), 3),
                    "driver_signal": c.get("driver_signal"),
                    "length_signal": c.get("length_signal"),
                    "is_new_user_join": c.get("is_new_user_join"),
                }
                for c in report.get("culprits", []) or []
            ],
        }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a CSV-driven anomaly scenario.")
    parser.add_argument("scenario_dir", type=Path, help="Path to a scenario directory (containing scenario.json + history.csv + current.csv).")
    args = parser.parse_args(argv)
    run_scenario(args.scenario_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
