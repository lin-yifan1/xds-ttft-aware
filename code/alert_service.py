"""告警接收 HTTP 服务。

把 ``maas_monitor_cli.analyze_cluster`` 包成一个常驻 Flask 应用：

* ``POST /alerts`` 接收告警 JSON，经 :class:`AlertAggregator` 归并后同步返回
  检测结果。
* ``GET /health`` 返回聚合器统计，便于观察去重比例。

启动::

    uv run python code/alert_service.py \
        --base-url https://example.com --appcode your-appcode --port 5050

告警请求 body::

    {
        "reported_at": "2026-05-19 10:00:00",   # 或 epoch seconds 字符串
        "infer_service_id": "svc-001",
        "model_name": "qwen-2.5"                # 可选
    }

聚合策略（同服务 + 同分钟去重）：

* 同一分钟内、同 ``(infer_service_id, model_name)`` 的告警会被合并成一次
  ``analyze_cluster`` 调用；后到的告警直接 wait 同一个 Future。
* Future 完成后在 ``--ttl-seconds`` 内仍可被同 key 命中，复用历史结果。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Any, Optional

from flask import Flask, jsonify, request

from alert_aggregator import AlertAggregator, AlertRequest
from config import DEFAULT_LATENCY_CONFIG, LatencyDetectorConfig
from maas_monitor_cli import (
    APPCODE_ENV,
    BASE_URL_ENV,
    DEFAULT_PAGE_SIZE,
    DEFAULT_RATE_LIMIT_SECONDS,
    DEFAULT_TIMEZONE,
    MaasMonitorClient,
    analyze_cluster,
    parse_reported_at,
    to_jsonable,
)


logger = logging.getLogger(__name__)


def create_app(
    base_url: Optional[str] = None,
    appcode: Optional[str] = None,
    appcode_header: str = "appcode",
    timezone_name: str = DEFAULT_TIMEZONE,
    history_days: int = 14,
    window_before_minutes: int = 10,
    window_after_minutes: int = 10,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout_seconds: float = 30.0,
    rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
    cfg: Optional[LatencyDetectorConfig] = None,
    aggregator_ttl_seconds: float = 60.0,
    aggregator_max_workers: int = 4,
    client: Optional[MaasMonitorClient] = None,
    aggregator: Optional[AlertAggregator] = None,
) -> Flask:
    """构造 Flask app。

    ``client`` 与 ``aggregator`` 可在测试中注入；生产环境通常省略以走默认装配。
    """
    base_url = base_url or os.environ.get(BASE_URL_ENV, "")
    appcode = appcode or os.environ.get(APPCODE_ENV, "")
    cfg = cfg or DEFAULT_LATENCY_CONFIG

    if client is None:
        client = MaasMonitorClient(
            base_url=base_url,
            appcode=appcode,
            appcode_header=appcode_header,
            page_size=page_size,
            timeout_seconds=timeout_seconds,
            rate_limit_seconds=rate_limit_seconds,
        )

    def handle(alert: AlertRequest) -> dict[str, Any]:
        return analyze_cluster(
            client,
            alert.reported_at,
            alert.infer_service_id,
            alert.model_name,
            cfg,
            timezone_name=timezone_name,
            history_days=history_days,
            window_before_minutes=window_before_minutes,
            window_after_minutes=window_after_minutes,
        )

    if aggregator is None:
        aggregator = AlertAggregator(
            handler=handle,
            result_ttl_seconds=aggregator_ttl_seconds,
            max_workers=aggregator_max_workers,
        )

    app = Flask(__name__)
    app.config["AGGREGATOR"] = aggregator
    app.config["TIMEZONE"] = timezone_name

    @app.post("/alerts")
    def receive_alert():  # noqa: WPS430 - Flask 路由
        payload = request.get_json(silent=True) or {}
        reported_at_raw = payload.get("reported_at")
        infer_service_id = payload.get("infer_service_id")
        model_name = payload.get("model_name")

        if not reported_at_raw or not infer_service_id:
            return jsonify({
                "status": "error",
                "error": "reported_at and infer_service_id are required",
            }), 400

        try:
            reported_at = parse_reported_at(reported_at_raw, timezone_name)
        except Exception as exc:  # noqa: BLE001 - 任何解析错误都回 400
            return jsonify({
                "status": "error",
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            }), 400

        alert = AlertRequest(
            reported_at=reported_at,
            infer_service_id=str(infer_service_id),
            model_name=str(model_name) if model_name else None,
            raw=payload,
        )

        result = aggregator.submit(alert)
        status_code = 500 if result.get("status") == "error" else 200
        body = json.dumps(to_jsonable(result), ensure_ascii=False)
        return app.response_class(
            response=body,
            status=status_code,
            mimetype="application/json",
        )

    @app.get("/health")
    def health():  # noqa: WPS430 - Flask 路由
        return jsonify({"status": "ok", "stats": aggregator.snapshot_stats()})

    return app


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MaaS 告警聚合 HTTP 服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--base-url", default=os.environ.get(BASE_URL_ENV, ""))
    parser.add_argument("--appcode", default=os.environ.get(APPCODE_ENV, ""))
    parser.add_argument("--appcode-header", default="appcode")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--history-days", type=int, default=14)
    parser.add_argument("--window-before-minutes", type=int, default=10)
    parser.add_argument("--window-after-minutes", type=int, default=10)
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--rate-limit-seconds", type=float, default=DEFAULT_RATE_LIMIT_SECONDS)
    parser.add_argument(
        "--ttl-seconds",
        type=float,
        default=60.0,
        help="告警聚合结果缓存 TTL（秒）；0 表示只做 in-flight 复用。",
    )
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument(
        "--ttft-sla",
        type=float,
        default=DEFAULT_LATENCY_CONFIG.ttft_sla,
    )
    parser.add_argument(
        "--tpot-sla",
        type=float,
        default=DEFAULT_LATENCY_CONFIG.tpot_sla,
    )
    parser.add_argument(
        "--severe-ratio",
        type=float,
        default=DEFAULT_LATENCY_CONFIG.severe_ratio,
    )
    parser.add_argument(
        "--mild-consecutive-windows",
        type=int,
        default=DEFAULT_LATENCY_CONFIG.mild_consecutive_windows,
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=DEFAULT_LATENCY_CONFIG.max_events,
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = LatencyDetectorConfig(
        ttft_sla=args.ttft_sla,
        tpot_sla=args.tpot_sla,
        severe_ratio=args.severe_ratio,
        mild_consecutive_windows=args.mild_consecutive_windows,
        max_events=args.max_events,
    )
    app = create_app(
        base_url=args.base_url,
        appcode=args.appcode,
        appcode_header=args.appcode_header,
        timezone_name=args.timezone,
        history_days=args.history_days,
        window_before_minutes=args.window_before_minutes,
        window_after_minutes=args.window_after_minutes,
        page_size=args.page_size,
        timeout_seconds=args.timeout_seconds,
        rate_limit_seconds=args.rate_limit_seconds,
        cfg=cfg,
        aggregator_ttl_seconds=args.ttl_seconds,
        aggregator_max_workers=args.max_workers,
    )
    logger.info("alert-service listening on %s:%s", args.host, args.port)
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
