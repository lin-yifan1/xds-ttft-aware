"""通过构造 API 返回数据检测异常检测算法 —— TTFT-only + 新用户加入场景。

本脚本不是单元测试，而是一个**对抗性构造示范**：演示如何只通过控制 MaaS
Monitor API 返回的 rows 来探测 ``analyze_cluster`` 的行为，并给出预期输出。

构造目标：
1. 让首查窗口 ``[reported_at, reported_at + 1min)`` 的 system_ttft 落在
   ``(SLA, SLA * severe_ratio)`` 的中间区，强制走 ``second_window_history`` 分支。
2. 在事件期 ``[10:00, 10:09]`` 连续 10 分钟轻度超 TTFT SLA，命中
   ``mild_consecutive_windows`` 触发持续异常。
3. TPOT 始终 ≤ SLA，使事件 scope 落到 ``ttft_only`` 而不是 ``both``。
4. 用户 B 是老用户，事件期 RPM 80→200、prompt 500→1500 突增。
5. 用户 C 没有任何历史记录，事件期首次出现（rpm=50, prompt=2000），
   触发 ``event_variant = new_user_join_event``。

预期算法输出（与代码 100% 对得上即说明算法实现与设计一致）：

  status                       == "anomaly"
  decision_source              == "second_window_history"
  query_count                  == 2
  event_reports[0]:
    rootcause_scope            == "ttft_only"
    duration_hours             == 10
    event_variant              == "new_user_join_event"
    new_join_users             == ["user-C"]
    driver_signal              ∈ {"input_shift_dominant", "rpm_input_mixed"}
    culprits                   == [user-B, user-C]   # B 在前，C 在后
    culprits[0].driver_signal  == "rpm_rise_dominant"      # B 流量主导
    culprits[1].driver_signal  == "input_shift_dominant"   # C 输入主导
    culprits[1].is_new_user_join == True

运行：

    uv run python tests/scenario_ttft_only_new_user.py
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "code"))

from config import LatencyDetectorConfig  # noqa: E402
from maas_monitor_cli import analyze_cluster  # noqa: E402


TZ = ZoneInfo("Asia/Shanghai")
REPORTED_AT = datetime(2026, 5, 15, 10, 0, 0, tzinfo=TZ)
HISTORY_DAYS = 14
WINDOW_BEFORE_MIN = 10
WINDOW_AFTER_MIN = 10


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
    """构造一行符合 MaaS Monitor API 响应格式的指标。"""
    return {
        "timestamp": str(int(ts.timestamp())),
        "domain_id": domain_id,
        "rpm": rpm,
        "tpm": tpm,
        "ttft": ttft,
        "tpot": tpot,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
    }


# ----- 数据构造 --------------------------------------------------------------

def build_history_rows() -> list[dict[str, Any]]:
    """过去 14 天，每天 [09:50, 10:10) 共 20 分钟，每分钟每用户 1 行。
    A、B 都是老用户，稳态流量。C 不出现，所以 C 对算法而言没有历史基线。"""
    rows: list[dict[str, Any]] = []
    for day in range(1, HISTORY_DAYS + 1):
        for offset in range(-WINDOW_BEFORE_MIN, WINDOW_AFTER_MIN):
            ts = REPORTED_AT - timedelta(days=day) + timedelta(minutes=offset)
            rows.append(_row(ts, "user-A", rpm=100, tpm=60_000, ttft=8_000, tpot=25, prompt=500, completion=100))
            rows.append(_row(ts, "user-B", rpm=80,  tpm=48_000, ttft=6_000, tpot=25, prompt=500, completion=100))
    return rows


def build_current_rows() -> list[dict[str, Any]]:
    """上报日 [09:50, 10:10) 共 20 分钟。
    09:50-09:59 维持稳态（与历史一致）。
    10:00-10:09 这 10 分钟：A 几乎不变；B 流量与输入突增；C 新接入。"""
    rows: list[dict[str, Any]] = []
    for offset in range(-WINDOW_BEFORE_MIN, WINDOW_AFTER_MIN):
        ts = REPORTED_AT + timedelta(minutes=offset)
        in_event = 0 <= offset < 10
        if in_event:
            rows.append(_row(ts, "user-A", rpm=100, tpm=60_000,  ttft=9_000,  tpot=28, prompt=500,  completion=100))
            rows.append(_row(ts, "user-B", rpm=200, tpm=340_000, ttft=25_000, tpot=30, prompt=1_500, completion=200))
            rows.append(_row(ts, "user-C", rpm=50,  tpm=105_000, ttft=30_000, tpot=32, prompt=2_000, completion=100))
        else:
            rows.append(_row(ts, "user-A", rpm=100, tpm=60_000, ttft=8_000, tpot=25, prompt=500, completion=100))
            rows.append(_row(ts, "user-B", rpm=80,  tpm=48_000, ttft=6_000, tpot=25, prompt=500, completion=100))
    return rows


# ----- Stub client -----------------------------------------------------------

class StubMaasClient:
    """duck-typed 替代 MaasMonitorClient，仅实现 analyze_cluster 用到的 query_metrics。

    按 [start, end) 时间窗过滤一份预制 rows，模拟 MaaS API 的真实响应。
    """

    def __init__(self) -> None:
        self._rows = build_history_rows() + build_current_rows()
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
        rows = [r for r in self._rows if start_ts <= int(r["timestamp"]) < end_ts]
        return rows, 1


# ----- 跑一次并打印关键字段 --------------------------------------------------

def main() -> None:
    cfg = LatencyDetectorConfig()  # 默认 ttft_sla=15000, tpot_sla=50, severe_ratio=7, mild_consecutive_windows=10
    client = StubMaasClient()
    result = analyze_cluster(
        client,
        REPORTED_AT,
        infer_service_id="svc-test-001",
        model_name=None,
        cfg=cfg,
        history_days=HISTORY_DAYS,
        window_before_minutes=WINDOW_BEFORE_MIN,
        window_after_minutes=WINDOW_AFTER_MIN,
    )

    summary: dict[str, Any] = {
        "status": result.get("status"),
        "decision_source": result.get("decision_source"),
        "query_count": result.get("query_count"),
        "events": result.get("events", []),
        "event_count": len(result.get("event_reports", [])),
        "calls": [
            {"start": str(s), "end": str(e)} for s, e in client.calls
        ],
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


if __name__ == "__main__":
    main()
