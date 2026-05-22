"""构造 TTFT-only + 新用户加入场景的 CSV + scenario.json，末尾自动调 run_scenario 自验证。

设计意图与预期算法输出见同目录 README.md。

运行：

    uv run python tests/scenarios/ttft_only_new_user/build.py
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SCENARIO_DIR = Path(__file__).resolve().parent
TESTS_DIR = SCENARIO_DIR.parents[1]
PROJECT_ROOT = TESTS_DIR.parent

sys.path.insert(0, str(PROJECT_ROOT / "code"))
sys.path.insert(0, str(TESTS_DIR))

from config import LatencyDetectorConfig  # noqa: E402
from run_scenario import TIMESTAMP_FORMAT, run_scenario  # noqa: E402


TZ = ZoneInfo("Asia/Shanghai")
REPORTED_AT = datetime(2026, 5, 15, 10, 0, 0, tzinfo=TZ)
INFER_SERVICE_ID = "svc-test-001"
MODEL_NAME: str | None = None
HISTORY_DAYS = 14
WINDOW_BEFORE_MIN = 10
WINDOW_AFTER_MIN = 10
CFG = LatencyDetectorConfig()


CSV_COLUMNS = [
    "collect_time_std",
    "domain_id",
    "rpm",
    "tpm",
    "ttft",
    "tpot",
    "prompt_tokens",
    "completion_tokens",
]


def _row(
    ts: datetime,
    domain_id: str,
    *,
    rpm: float,
    tpm: float,
    ttft: float,
    tpot: float,
    prompt: float,
    completion: float,
) -> dict[str, Any]:
    return {
        "collect_time_std": ts.strftime(TIMESTAMP_FORMAT),
        "domain_id": domain_id,
        "rpm": rpm,
        "tpm": tpm,
        "ttft": ttft,
        "tpot": tpot,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
    }


def build_history_rows() -> list[dict[str, Any]]:
    """过去 14 天 × 每天 [09:50, 10:10) 共 20 分钟 × A、B 两个老用户，稳态流量。"""
    rows: list[dict[str, Any]] = []
    for day in range(1, HISTORY_DAYS + 1):
        for offset in range(-WINDOW_BEFORE_MIN, WINDOW_AFTER_MIN):
            ts = REPORTED_AT - timedelta(days=day) + timedelta(minutes=offset)
            rows.append(_row(ts, "user-A", rpm=100, tpm=60_000, ttft=8_000, tpot=25, prompt=500, completion=100))
            rows.append(_row(ts, "user-B", rpm=80, tpm=48_000, ttft=6_000, tpot=25, prompt=500, completion=100))
    rows.sort(key=lambda r: (r["collect_time_std"], r["domain_id"]))
    return rows


def build_current_rows() -> list[dict[str, Any]]:
    """上报日 [09:50, 10:10) 共 20 分钟。

    09:50–09:59 维持稳态（与历史一致）。
    10:00–10:09 事件期：A 几乎不变；B 流量与输入突增；C 新接入。
    """
    rows: list[dict[str, Any]] = []
    for offset in range(-WINDOW_BEFORE_MIN, WINDOW_AFTER_MIN):
        ts = REPORTED_AT + timedelta(minutes=offset)
        in_event = 0 <= offset < 10
        if in_event:
            rows.append(_row(ts, "user-A", rpm=100, tpm=60_000, ttft=9_000, tpot=28, prompt=500, completion=100))
            rows.append(_row(ts, "user-B", rpm=200, tpm=340_000, ttft=25_000, tpot=30, prompt=1_500, completion=200))
            rows.append(_row(ts, "user-C", rpm=50, tpm=105_000, ttft=30_000, tpot=32, prompt=2_000, completion=100))
        else:
            rows.append(_row(ts, "user-A", rpm=100, tpm=60_000, ttft=8_000, tpot=25, prompt=500, completion=100))
            rows.append(_row(ts, "user-B", rpm=80, tpm=48_000, ttft=6_000, tpot=25, prompt=500, completion=100))
    rows.sort(key=lambda r: (r["collect_time_std"], r["domain_id"]))
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_scenario_json(path: Path) -> None:
    meta = {
        "reported_at": REPORTED_AT.strftime(TIMESTAMP_FORMAT),
        "infer_service_id": INFER_SERVICE_ID,
        "model_name": MODEL_NAME,
        "history_days": HISTORY_DAYS,
        "window_before_minutes": WINDOW_BEFORE_MIN,
        "window_after_minutes": WINDOW_AFTER_MIN,
        "config": {
            "ttft_sla": CFG.ttft_sla,
            "tpot_sla": CFG.tpot_sla,
            "severe_ratio": CFG.severe_ratio,
            "mild_consecutive_windows": CFG.mild_consecutive_windows,
        },
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main() -> None:
    history_path = SCENARIO_DIR / "history.csv"
    current_path = SCENARIO_DIR / "current.csv"
    meta_path = SCENARIO_DIR / "scenario.json"

    write_csv(history_path, build_history_rows())
    write_csv(current_path, build_current_rows())
    write_scenario_json(meta_path)
    print(f"[build] wrote {history_path.relative_to(PROJECT_ROOT)}")
    print(f"[build] wrote {current_path.relative_to(PROJECT_ROOT)}")
    print(f"[build] wrote {meta_path.relative_to(PROJECT_ROOT)}")
    print("[build] self-verify by running run_scenario...")
    run_scenario(SCENARIO_DIR)


if __name__ == "__main__":
    main()
