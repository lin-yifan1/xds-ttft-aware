"""alert_service.create_app 的端到端测试。

用 Flask test client + 注入的 AlertAggregator 验证：

* POST /alerts 必填字段校验
* 同分钟同服务的告警在 HTTP 层也只触发一次 handler
* GET /health 返回聚合统计
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any

from alert_aggregator import AlertAggregator, AlertRequest
from alert_service import create_app


class AlertServiceTests(unittest.TestCase):

    def _build_app(self, handler):
        aggregator = AlertAggregator(handler=handler, result_ttl_seconds=60.0)
        app = create_app(
            base_url="http://example.invalid",
            appcode="test-appcode",
            aggregator=aggregator,
        )
        app.config["TESTING"] = True
        return app, aggregator

    def test_missing_fields_returns_400(self) -> None:
        def handler(alert: AlertRequest) -> dict[str, Any]:
            self.fail("handler 不应被调用")
            return {}

        app, _ = self._build_app(handler)
        client = app.test_client()
        resp = client.post("/alerts", json={"reported_at": "2026-05-19 10:00:00"})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual(body["status"], "error")

    def test_dedups_across_http_requests(self) -> None:
        counter = {"n": 0}
        lock = threading.Lock()
        gate = threading.Event()

        def handler(alert: AlertRequest) -> dict[str, Any]:
            with lock:
                counter["n"] += 1
            gate.wait(timeout=2.0)
            return {
                "status": "normal",
                "service": alert.infer_service_id,
            }

        app, aggregator = self._build_app(handler)
        client = app.test_client()

        results: list[Any] = [None, None, None]

        def post(i: int, second_offset: int) -> None:
            results[i] = client.post(
                "/alerts",
                json={
                    "reported_at": f"2026-05-19 10:00:{second_offset:02d}",
                    "infer_service_id": "svc-001",
                    "model_name": "qwen-2.5",
                },
            )

        threads = [
            threading.Thread(target=post, args=(i, i * 5))
            for i in range(3)
        ]
        for t in threads:
            t.start()
        time.sleep(0.1)
        gate.set()
        for t in threads:
            t.join(timeout=3)

        for resp in results:
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertEqual(body["status"], "normal")
            self.assertEqual(body["service"], "svc-001")

        self.assertEqual(counter["n"], 1, "底层 handler 应只被调用一次")

        stats_resp = client.get("/health").get_json()["stats"]
        self.assertEqual(stats_resp["total_submitted"], 3)
        self.assertEqual(stats_resp["unique_handler_calls"], 1)
        self.assertEqual(stats_resp["deduped_inflight"], 2)

    def test_different_services_not_deduped(self) -> None:
        counter = {"n": 0}

        def handler(alert: AlertRequest) -> dict[str, Any]:
            counter["n"] += 1
            return {"status": "normal", "service": alert.infer_service_id}

        app, _ = self._build_app(handler)
        client = app.test_client()
        for svc in ("svc-a", "svc-b", "svc-c"):
            resp = client.post(
                "/alerts",
                json={
                    "reported_at": "2026-05-19 10:00:00",
                    "infer_service_id": svc,
                },
            )
            self.assertEqual(resp.status_code, 200)
        self.assertEqual(counter["n"], 3)


if __name__ == "__main__":
    unittest.main()
