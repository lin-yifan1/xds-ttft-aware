# MaaS API：集群时延异常检测 CLI

当前主流程直接调用 MaaS 统一监控数据查询 API，输出 JSON。运行时不需要 CSV、SQLite，也不需要图形化页面。

核心检测逻辑仍复用 `code/latency_detector.py`：

1. 先查询上报时间点的 1 分钟窗口。
2. 如果这一分钟已经达到重度异常阈值，则直接判定异常。
3. 如果这一分钟 TTFT/TPOT 都优于 SLA，则直接判定无异常。
4. 否则拉取近 14 天连续数据，在本地筛出每天同一时刻 `[-10min, +10min)` 的 20 分钟窗口，用历史同期窗口建立基线，再对上报日 20 分钟窗口做异常检测和根因定位。

## 1. 安装依赖

```powershell
uv sync
```

## 2. 配置 API

CLI 默认从环境变量读取 API 地址和 appcode：

```powershell
$env:MAAS_MONITOR_BASE_URL = "https://example.com"
$env:MAAS_APP_CODE = "your-appcode"
```

鉴权默认使用请求头：

```text
appcode: <MAAS_APP_CODE>
```

如网关使用其他 header 名，可以通过 `--appcode-header` 覆盖。

## 3. 运行检测

```powershell
uv run python code/maas_monitor_cli.py `
  --reported-at "2026-05-15 10:00:00" `
  --infer-service-id "svc-001"
```

可选参数：

- `--model-name`：只在提供时加入 API 过滤条件。
- `--timezone`：默认 `Asia/Shanghai`。
- `--ttft-sla`：默认 `15000` ms。
- `--tpot-sla`：默认 `50` ms。
- `--severe-ratio`：默认 `7`。
- `--mild-consecutive-windows`：默认 `10`。
- `--rate-limit-seconds`：分页请求之间的等待时间，默认 `60`，用于遵守 API 限流。

## 4. 输出

CLI 向 stdout 输出 JSON，主要字段包括：

- `status`：`anomaly` / `normal`
- `decision_source`：`first_window_severe` / `first_window_sla_clear` / `second_window_history`
- `query_count`：逻辑查询次数，首查确定时为 `1`，进入同期比对时为 `2`
- `http_request_count`：包含分页在内的实际 HTTP 请求次数
- `first_window`：首查窗口的系统 TTFT/TPOT/RPM/TPM 摘要
- `events` / `event_reports` / `records`：异常事件、根因用户和用户记录
- `system_stats`：系统层统计
- `config`：本次检测配置回显

## 5. API 查询映射

请求维度：

- `domain_id`
- `timestamp`，粒度 `minute`

请求指标：

- `rpm(sum)`
- `tpm(sum)`
- `ttft(avg)`
- `tpot(avg)`
- `prompt_tokens(avg)`
- `completion_tokens(avg)`

默认过滤：

- `infer_service_id = <输入值>`
- `timestamp >= <窗口开始 epoch seconds>`
- `timestamp < <窗口结束 epoch seconds>`
- `model_name = <输入值>`，仅当传入 `--model-name` 时添加

## 6. 在线告警聚合服务

`code/alert_service.py` 提供一个常驻 Flask 服务，用来接收外部告警系统的多条告警，并自动归并以降低 MaaS Monitor API 的查询开销。

启动：

```powershell
$env:MAAS_MONITOR_BASE_URL = "https://example.com"
$env:MAAS_APP_CODE = "your-appcode"
uv run python code/alert_service.py --port 5050
```

主要参数：

- `--ttl-seconds`：告警聚合结果缓存的存活时间，默认 `60`。
- `--max-workers`：内部检测线程池大小，默认 `4`。
- `--rate-limit-seconds`、`--history-days`、`--window-before-minutes`、`--window-after-minutes`、`--ttft-sla`、`--tpot-sla` 等含义与 CLI 一致。

请求接口：

```http
POST /alerts
Content-Type: application/json

{
  "reported_at": "2026-05-19 10:00:00",
  "infer_service_id": "svc-001",
  "model_name": "qwen-2.5"
}
```

响应是 `maas_monitor_cli.analyze_cluster` 的标准 JSON，外加 `aggregation` 字段：

```json
{
  "status": "normal",
  "decision_source": "first_window_sla_clear",
  ...
  "aggregation": {
    "key": ["svc-001", "qwen-2.5", "2026-05-19T10:00:00+08:00"],
    "reused": false
  }
}
```

聚合策略：

- 同一分钟、同 `(infer_service_id, model_name)` 的多条告警共享一次实际检测。后到的请求会等待已在跑的 Future，不会再发起 API 调用，响应里 `aggregation.reused = true`。
- 任务完成后在 `--ttl-seconds` 内继续命中缓存；超时后下一次同 key 告警会重新触发检测。
- 失败结果不缓存，下一条同 key 告警仍会重试。

观测：

```http
GET /health
```

返回 `total_submitted` / `unique_handler_calls` / `deduped_inflight` / `deduped_cached` / `failed_handler_calls` 等聚合统计，便于评估告警去重比例。

测试：

```powershell
uv run python -m unittest code.test_alert_aggregator code.test_alert_service
```

## 7. 旧本地工具

仓库中仍保留原有 CSV/SQLite/Web 脚本，便于回看和本地调试旧数据。但当前推荐入口是：

```powershell
uv run python code/maas_monitor_cli.py --reported-at ... --infer-service-id ...
```
