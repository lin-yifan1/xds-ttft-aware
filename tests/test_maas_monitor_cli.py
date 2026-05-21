from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "code"))

from config import LatencyDetectorConfig
from maas_monitor_cli import (
    MaasMonitorApiError,
    MaasMonitorClient,
    analyze_cluster,
    rows_to_latency_frame,
    split_same_time_windows,
)


TZ = ZoneInfo("Asia/Shanghai")


def api_row(ts: datetime, domain_id: str = "u1", **metrics):
    values = {
        "domain_id": domain_id,
        "timestamp": str(int(ts.timestamp())),
        "rpm": 10,
        "tpm": 100,
        "ttft": 100.0,
        "tpot": 10.0,
        "prompt_tokens": 20.0,
        "completion_tokens": 30.0,
    }
    values.update(metrics)
    return values


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeUrlopen:
    def __init__(self, payloads: list[dict]):
        self.payloads = list(payloads)
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        return FakeResponse(self.payloads.pop(0))


class FakeClient:
    def __init__(self, responses: list[list[dict]]):
        self.responses = list(responses)
        self.calls = []

    def query_metrics(self, start, end, infer_service_id, model_name=None):
        self.calls.append((start, end, infer_service_id, model_name))
        return self.responses.pop(0), 1


class MaasMonitorCliTests(unittest.TestCase):
    def test_client_sends_appcode_header_and_paginates(self):
        reported = datetime(2026, 5, 15, 10, 0, tzinfo=TZ)
        fake = FakeUrlopen(
            [
                {
                    "code": 200,
                    "msg": "success",
                    "data": {"list": [api_row(reported)], "pageNum": 1, "pageSize": 1, "pages": 2},
                },
                {
                    "code": 200,
                    "msg": "success",
                    "data": {"list": [api_row(reported, domain_id="u2")], "pageNum": 2, "pageSize": 1, "pages": 2},
                },
            ]
        )
        client = MaasMonitorClient(
            "https://maas.example",
            "secret",
            rate_limit_seconds=0,
            page_size=1,
            urlopen_func=fake,
        )

        rows, request_count = client.query_metrics(reported, reported + timedelta(minutes=1), "svc-1", "glm")

        self.assertEqual(request_count, 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(fake.requests), 2)
        first_req = fake.requests[0]
        self.assertEqual(first_req.headers.get("Appcode"), "secret")
        payload = json.loads(first_req.data.decode("utf-8"))
        self.assertEqual(payload["page"]["pageNum"], 1)
        self.assertIn({"name": "infer_service_id", "operator": "=", "value": "svc-1"}, payload["filters"])
        self.assertIn({"name": "model_name", "operator": "=", "value": "glm"}, payload["filters"])
        self.assertIn({"name": "timestamp", "operator": "<", "value": str(int((reported + timedelta(minutes=1)).timestamp()))}, payload["filters"])

    def test_client_raises_on_api_error_code(self):
        fake = FakeUrlopen([{"code": 429, "msg": "limited", "data": {}}])
        client = MaasMonitorClient("https://maas.example", "secret", rate_limit_seconds=0, urlopen_func=fake)

        with self.assertRaises(MaasMonitorApiError) as ctx:
            client.query_metrics(
                datetime(2026, 5, 15, 10, 0, tzinfo=TZ),
                datetime(2026, 5, 15, 10, 1, tzinfo=TZ),
                "svc-1",
            )
        self.assertEqual(ctx.exception.status, 429)

    def test_client_rate_limits_across_logical_queries(self):
        reported = datetime(2026, 5, 15, 10, 0, tzinfo=TZ)
        sleeps = []
        fake = FakeUrlopen(
            [
                {"code": 200, "msg": "success", "data": {"list": [api_row(reported)], "pageNum": 1, "pages": 1}},
                {"code": 200, "msg": "success", "data": {"list": [api_row(reported)], "pageNum": 1, "pages": 1}},
            ]
        )
        client = MaasMonitorClient(
            "https://maas.example",
            "secret",
            rate_limit_seconds=60,
            urlopen_func=fake,
            sleep_func=sleeps.append,
        )

        client.query_metrics(reported, reported + timedelta(minutes=1), "svc-1")
        client.query_metrics(reported, reported + timedelta(minutes=1), "svc-1")

        self.assertEqual(len(sleeps), 1)
        self.assertGreater(sleeps[0], 0)

    def test_rows_to_latency_frame_maps_epoch_and_metric_names(self):
        ts = datetime(2026, 5, 15, 10, 0, tzinfo=TZ)
        df = rows_to_latency_frame([api_row(ts, ttft=123.4, tpot=56.7)])

        self.assertEqual(df.loc[0, "collect_time_std"], "2026-05-15 10:00:00")
        self.assertEqual(df.loc[0, "ttft_avg"], 123.4)
        self.assertEqual(df.loc[0, "tpot_avg"], 56.7)

    def test_split_same_time_windows_keeps_current_20_minutes_and_history_offsets(self):
        reported = datetime(2026, 5, 15, 10, 0, tzinfo=TZ)
        rows = [
            api_row(reported - timedelta(minutes=11)),
            api_row(reported - timedelta(minutes=10)),
            api_row(reported + timedelta(minutes=9)),
            api_row(reported + timedelta(minutes=10)),
            api_row(reported - timedelta(days=1, minutes=10), domain_id="hist-a"),
            api_row(reported - timedelta(days=1) + timedelta(minutes=9), domain_id="hist-b"),
            api_row(reported - timedelta(days=1) + timedelta(minutes=10), domain_id="hist-out"),
        ]
        df = rows_to_latency_frame(rows)

        current, history = split_same_time_windows(df, reported)

        self.assertEqual(len(current), 2)
        self.assertEqual(set(history["domain_id"]), {"hist-a", "hist-b"})

    def test_first_window_severe_skips_second_query(self):
        reported = datetime(2026, 5, 15, 10, 0, tzinfo=TZ)
        cfg = LatencyDetectorConfig()
        client = FakeClient([[api_row(reported, ttft=cfg.ttft_sla * cfg.severe_ratio + 1)]])

        result = analyze_cluster(client, reported, "svc-1", None, cfg)

        self.assertEqual(result["status"], "anomaly")
        self.assertEqual(result["decision_source"], "first_window_severe")
        self.assertEqual(result["query_count"], 1)
        self.assertEqual(len(client.calls), 1)

    def test_first_window_sla_clear_skips_second_query(self):
        reported = datetime(2026, 5, 15, 10, 0, tzinfo=TZ)
        cfg = LatencyDetectorConfig()
        client = FakeClient([[api_row(reported, ttft=cfg.ttft_sla - 1, tpot=cfg.tpot_sla - 1)]])

        result = analyze_cluster(client, reported, "svc-1", None, cfg)

        self.assertEqual(result["status"], "normal")
        self.assertEqual(result["decision_source"], "first_window_sla_clear")
        self.assertEqual(result["query_count"], 1)
        self.assertEqual(len(client.calls), 1)

    def test_uncertain_first_window_runs_second_query_with_history_detector(self):
        reported = datetime(2026, 5, 15, 10, 0, tzinfo=TZ)
        cfg = LatencyDetectorConfig(mild_consecutive_windows=2, min_baseline_points=1)
        first_rows = [api_row(reported, ttft=cfg.ttft_sla + 1, tpot=cfg.tpot_sla + 1)]

        second_rows = []
        for minute in range(-10, 10):
            second_rows.append(api_row(reported + timedelta(minutes=minute), ttft=cfg.ttft_sla + 1, rpm=20, tpm=200))
        for day in range(1, 3):
            anchor = reported - timedelta(days=day)
            for minute in range(-10, 10):
                second_rows.append(api_row(anchor + timedelta(minutes=minute), ttft=100, rpm=5, tpm=50))

        client = FakeClient([first_rows, second_rows])

        result = analyze_cluster(client, reported, "svc-1", None, cfg)

        self.assertEqual(result["decision_source"], "second_window_history")
        self.assertEqual(result["query_count"], 2)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(result["status"], "anomaly")
        self.assertEqual(result["events"], [[0, 19]] if isinstance(result["events"][0], list) else [(0, 19)])
        self.assertEqual(result["second_window"]["current_rows"], 20)
        self.assertEqual(result["second_window"]["history_rows"], 40)


if __name__ == "__main__":
    unittest.main()
