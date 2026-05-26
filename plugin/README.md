# MaaS 过载溯源插件

单文件 Python 插件，输入告警五元组，调用 MaaS 数据查询接口，输出告警时刻的系统事件与租户级根因 (culprits)。

## 1. 文件

| 文件 | 说明 |
| --- | --- |
| `main.py` | 插件主体，自包含。依赖 `numpy`、`pandas`、`requests`。 |
| `README.md` | 本文档。 |

## 2. 调用方式

```bash
python main.py <domain_id> <service_id> <time> <maasApiurl> \
               <appcode> <applydomainid> <applyprojectid>
```

入参严格按位置传入，共 7 个，全部必填。

| # | 名称 | 含义 | 备注 |
| ---: | --- | --- | --- |
| 1 | `domain_id` | 告警上报租户 ID | 用于跨池历史查询和输出标注 `is_alert_reporter` |
| 2 | `service_id` | `infer_service_id` | Round 1 查询过滤维度 |
| 3 | `time` | ISO 8601 字符串或数字时间戳 | 例 `2026-05-22T10:30:00+08:00` 或 `1779349646000`；数字 > `1e12` 视为毫秒；ISO 无 tz 后缀时按 `PLUGIN_TIMEZONE` 解释 |
| 4 | `maasApiurl` | MaaS 数据查询接口完整端点 URL | 例 `https://.../v1/maas/om/data/query` |
| 5 | `appcode` | API 网关 appcode | 作为 `X-Apig-AppCode` header |
| 6 | `applydomainid` | 调用方租户 ID | 作为 `X-Apply-DomainID` header |
| 7 | `applyprojectid` | 调用方 project ID | 作为 `X-Apply-ProjectID` header |

## 3. 可选环境变量（用于调参）

| 环境变量 | 默认值 | 说明 |
| --- | ---: | --- |
| `PLUGIN_TTFT_SLA` | `15000` | TTFT SLA 阈值，单位 ms |
| `PLUGIN_TPOT_SLA` | `50` | TPOT SLA 阈值，单位 ms |
| `PLUGIN_SEVERE_RATIO` | `7` | 重度异常阈值倍率，触发 `Metric ≥ SLA × ratio` |
| `PLUGIN_MILD_CONSECUTIVE_WINDOWS` | `10` | 轻度异常需要连续超 SLA 的分钟数 |
| `PLUGIN_HISTORY_DAYS` | `14` | Round 2 跨池历史回看天数 |
| `PLUGIN_CANDIDATE_TOP_N` | `6` | Round 1 事件窗口内取多少候选租户 |
| `PLUGIN_CULPRIT_TOP_K` | `3` | 最终输出 culprit 数量上限 |
| `PLUGIN_TIMEZONE` | `Asia/Shanghai` | ISO 时间无时区时的兜底时区 |

## 4. 工作流

```
Round 1  (1 次 API)
├─ 过滤: infer_service_id = service_id
├─ 时间: reported_at ± 30min
├─ 聚合: 用户级 → 系统级 (RPM 加权 TTFT/TPOT)
└─ 检测: TTFT > SLA 或 TPOT > SLA 触发事件
         (重度 ≥ SLA × ratio 单点立判，轻度需连续 N 窗口)

⇣ 仅当 reported_at 落在某个事件窗口内才继续

候选选择
├─ 在事件窗口内按 max(TTFT/SLA) + max(TPOT/SLA) 排序
├─ 取 top N
└─ 强行加入告警上报者 (若不在 top N)

Round 2  (1 次 API)
├─ 过滤: domain_id IN [候选 ∪ 上报者]
├─ 时间: reported_at - 14d - 10min  到  reported_at + 10min
├─ 切片: 当前 ±10min 丢弃，其余按"同时刻偏移"聚合
└─ baseline: 每候选 × 每偏移 = 历史均值

评分
├─ 四类 excess = max(metric_window - baseline_window, 0)
├─ 用户间归一化为 ratio
├─ 按事件 scope 选权重 (ttft_only / tpot_only / both)
└─ Top-K culprit + driver_signal + length_signal
```

详细算法语义见仓库根目录 `ANOMALY_DETECTION_LOGIC.md` 与 `docs/workflow.md`。

## 5. 输出说明

stdout（实际通过 `logging.basicConfig` 输出到 stderr，与示例插件一致）打印一段多行 JSON，进度行以 `[round1] ...` `[round2] ...` 标记，最后是结果 JSON 对象。

顶层字段：

| 字段 | 说明 |
| --- | --- |
| `status` | `anomaly` / `normal` / `no_data` / `error`（见下） |
| `alert` | `{ domain_id, service_id, reported_at }` 回显 |
| `events` | 命中事件（最多 1 个；不命中时为空数组） |
| `culprits` | 根因租户数组（status=anomaly 时填充） |
| `system_stats` | TTFT / TPOT / RPM / TPM / 输入 / 输出 的均值、P95、最大值 |
| `config_echo` | 本次运行的全部 `PluginConfig` 字段 |
| `api_call_count` | 实际 HTTP 调用次数（包括分页） |
| `history_baseline` | Round 2 元信息（候选列表、历史行数等） |
| `warning` | 出现降级时的提示，如 baseline 全空 |

### 5.1 `status` 枚举

| status | exit code | 含义 |
| --- | ---: | --- |
| `anomaly` | 0 | Round 1 在 `reported_at` 命中事件，伴随 culprits |
| `normal` | 0 | Round 1 未在 `reported_at` 处命中事件（包括完全无事件 / 事件不覆盖 `reported_at`） |
| `no_data` | 0 | Round 1 返回空（service 在该时间段无数据） |
| `error` | 1 | 入参非法 / API 错误 / 解析失败，伴随 `error_type` 与 `error_msg` |

### 5.2 `culprit` 字段

| 字段 | 说明 |
| --- | --- |
| `domain_id` | 租户 ID |
| `is_alert_reporter` | 是否为入参 `domain_id`（告警上报者） |
| `score` | 加权综合得分 |
| `score_ratio` | 该 culprit 在所有候选中的得分占比 |
| `driver_signal` | 主导信号：`rpm_rise_dominant / input_shift_dominant / rpm_input_mixed / ...` |
| `length_signal` | 长度信号：`traffic_dominant / input_shift_dominant / output_shift_dominant / io_shift_joint / length_shift_mixed` |
| `rpm_excess_ratio` / `tpm_excess_ratio` | 流量类指标超基线占比 |
| `prompt_delta_ratio` / `completion_delta_ratio` | 长度类指标超基线占比 |
| `peak_time` | 该 culprit 综合得分峰值的分钟，ISO 8601 带 tz |
| `peak_rpm / peak_tpm / peak_ttft / peak_tpot / peak_prompt_tokens / peak_completion_tokens` | 峰值时刻各指标原值 |

## 6. 示例

成功：

```bash
python main.py "d-001" "s-abc" "2026-05-22T10:30:00+08:00" \
    "https://modelarts-test-internal.cn-north-7.myhuaweicloud.com/v1/maas/om/data/query" \
    "Ip2Ahr4...PYxw" \
    "a027d8f4e6cb4bfe88744c72a6d6d620" \
    "52ed5b89fd39497eaff88e3589d32d87"
```

参数错误（exit 1）：

```bash
python main.py    # 输出 { "status": "error", "error_type": "InvalidArgs", ... }
```

## 7. 已知约束

- MaaS API 当前仅支持 `granularity=minute`，故插件全程按分钟粒度对齐
- Round 2 用 `domain_id IN [...]`，候选数过大时建议增大 `PLUGIN_TIMEOUT_SECONDS` 或减少 `PLUGIN_CANDIDATE_TOP_N`
- 候选少于 `min_baseline_points=6` 个同时刻历史样本时，该候选 baseline 默认为 0，可能造成评分偏高
- 严格按 7 个位置参；如需扩展（如 `model_name` 过滤），需要修改 `main.py` 中 `EXPECTED_ARG_COUNT` 与 `run_plugin` 签名
- 无池子过滤：返回行中 `infer_service_id` 字段为空、或 `success_cnt + error_cnt == 0` 的分钟级数据视为无主聚合/无流量，在 `rows_to_dataframe` 入口处丢弃，不参与算法
