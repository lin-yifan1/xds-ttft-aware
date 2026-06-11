# MaaS 过载溯源插件

单文件 Python 插件，调用 MaaS 数据查询接口完成过载检测与根因定位。两个入口：

- **反应式（Mode A）** `main.py`：输入告警五元组，输出告警时刻的系统事件与租户级根因 (culprits)。见 §2–§7。
- **主动巡检（Mode B）** `proactive_main.py`：由定时巡检任务逐服务调用，判断「此刻是否正在过载」，并产出按 `(domain_id, resident_model_id, region)` 维度的过载处理策略 (strategies)。见 §8。

## 1. 文件

| 文件 | 说明 |
| --- | --- |
| `main.py` | 反应式插件主体，自包含。依赖 `numpy`、`pandas`、`requests`。 |
| `proactive_main.py` | 主动巡检插件主体，自包含（不 import `main.py`），同依赖。 |
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
| `PLUGIN_SCENARIO_TRIGGER_FACTOR` | `1.3` | 场景点亮阈值：窗口均值 `current ≥ baseline × factor` 才点亮 |
| `PLUGIN_TPM_CAP_FACTOR` | `1.5` | `input_too_long` 限流建议：`tpm_limit = baseline_tpm × factor` |
| `PLUGIN_OUTPUT_CAP_FACTOR` | `1.5` | `output_too_long` 限流建议：`max_output = baseline_completion × factor` |
| `PLUGIN_RPM_SHRINK_FACTOR` | `0.8` | `rpm_increase` 限流建议：`rpm_limit = baseline_rpm × factor`（缩小系数 < 1） |
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
├─ 三类 excess = max(metric_window - baseline_window, 0)  (rpm / 输入 / 输出)
├─ 用户间归一化为 ratio
├─ 按事件 scope 选三维权重 (ttft_only / tpot_only / both)
├─ 按 bScore 排序取 Top-K culprit
└─ 每 culprit 按 scope 门控点亮场景 + 给出 remediation
       (rpm_increase / input_too_long / output_too_long；
        窗口均值 current ≥ baseline × trigger_factor 才点亮)
```

事件检测与候选选择的算法语义见仓库根目录 `ANOMALY_DETECTION_LOGIC.md` 与 `docs/workflow.md`。
注意：三维评分权重与「三场景 + remediation」为本插件特有（root 文档描述的是 `code/` 旧版的四维 `driver_signal / length_signal` 模型），场景字段定义以本文 §5.3 为准。

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
| `score` | 加权综合得分（三维 bScore） |
| `score_ratio` | 该 culprit 在所有候选中的得分占比 |
| `scenarios` | 该租户点亮的场景数组（按 `trigger.ratio` 降序）；结构见 §5.3。可能为空数组 |
| `warning` | 仅当存在因 baseline 缺失被抑制的场景时出现：`baseline_unavailable: <类型列表>` |
| `note` | 仅当 `scenarios` 为空时出现：`no_scenario_triggered` |
| `peak_time` | 该 culprit 综合得分峰值的分钟，ISO 8601 带 tz |
| `peak_rpm / peak_tpm / peak_ttft / peak_tpot / peak_prompt_tokens / peak_completion_tokens` | 峰值时刻各指标原值 |

### 5.3 `scenario` 字段

每个 culprit 的 `scenarios` 是一个数组，元素为已点亮的场景对象。场景共三类，按 event `scope` 物理门控：`ttft_only` 仅允许 `rpm_increase / input_too_long`，`tpot_only` 仅允许 `output_too_long`，`both` 三者皆可。点亮条件为窗口均值 `current ≥ baseline × PLUGIN_SCENARIO_TRIGGER_FACTOR`。

| 场景 `type` | 触发指标 | 解决方案（remediation） |
| --- | --- | --- |
| `rpm_increase` | `rpm` | `action=throttle_rpm`，`target_metric=rpm`，`recommended_value = baseline_rpm × PLUGIN_RPM_SHRINK_FACTOR` |
| `input_too_long` | `prompt_tokens` | `action=cap_tpm`，`target_metric=tpm`，`recommended_value = baseline_tpm × PLUGIN_TPM_CAP_FACTOR`（触发看输入长度，限流杠杆落在 TPM） |
| `output_too_long` | `completion_tokens` | `action=cap_output_length`，`target_metric=completion_tokens`，`recommended_value = baseline_completion × PLUGIN_OUTPUT_CAP_FACTOR` |

每个场景对象的嵌套结构：

| 字段 | 说明 |
| --- | --- |
| `type` | 场景类型，见上表 |
| `trigger.metric` | 触发判定所用指标 |
| `trigger.current` | 事件窗口内该指标的窗口均值（非 0 均值） |
| `trigger.baseline` | 该指标的历史同时刻偏移 baseline |
| `trigger.ratio` | `current / baseline`，也是场景排序键 |
| `remediation.action` | 建议动作：`throttle_rpm / cap_tpm / cap_output_length` |
| `remediation.target_metric` | 限流杠杆落在哪个指标 |
| `remediation.baseline` | 目标指标的 baseline |
| `remediation.factor` | 放大/缩小系数（对应 `PLUGIN_*_FACTOR`） |
| `remediation.recommended_value` | 建议阈值 = `baseline × factor` |

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

## 8. 主动巡检插件 `proactive_main.py`

与 `main.py` 平行的第二入口：maas-monitor 的定时巡检任务（每 5 分钟）逐服务调用，
检测信号、根因评分与策略产出与反应式版有以下差异。算法语义详见仓库根目录
`PROACTIVE_INSPECTION_ALGORITHM.md`。

### 8.1 调用方式

```bash
python proactive_main.py <service_id> <model_name> <time> <maasApiurl> \
                         <appcode> <applydomainid> <applyprojectid>
```

入参严格按位置传入，共 7 个，全部必填。与 `main.py` 的差异：第 1 槽位不再是告警上报
租户（巡检没有上报者），新增 `model_name`（用于 `(P,M)` 过滤、SLA 选表与策略回填）。

| # | 名称 | 含义 |
| ---: | --- | --- |
| 1 | `service_id` | `infer_service_id`（被巡检的池子/服务实例） |
| 2 | `model_name` | 服务承载的模型名 |
| 3 | `time` | 巡检时刻，ISO 8601 或数字时间戳（同 `main.py` 规则） |
| 4–7 | `maasApiurl` / `appcode` / `applydomainid` / `applyprojectid` | 同 `main.py` |

### 8.2 专有环境变量（通用项同 `main.py` §3）

| 环境变量 | 默认值 | 说明 |
| --- | ---: | --- |
| `PLUGIN_TTFT_SLA` / `PLUGIN_TPOT_SLA` | 按模型表 | 不设时走 SLA 表：模型名含 `glm`（忽略大小写）→ 30000/500，其余 → 10000/150 |
| `PLUGIN_ENABLE_TPOT` | `0` | 置 1 时 TPOT 也参与事件检测（本轮公司范围仅 TTFT） |
| `PLUGIN_DOMINANCE_MARGIN` | `1.25` | 多指标触发时最大 ratio ≥ 次大 × margin 才算 dominant，否则 default(mixed) |
| `PLUGIN_LOOKBACK_MINUTES` | `60` | Round 1 回看窗口 |
| `PLUGIN_ACTIVE_RECENT_MINUTES` | `5` | 事件末端落在窗口最后 N 分钟内才算「正在过载」 |
| `PLUGIN_DETECT_ONLY` | `0` | 置 1 时 Step 1 判完即返回（恢复巡检 `allServiceCheckOverLoadResume` 用，省配额） |
| `PLUGIN_RETRY_MAX` / `PLUGIN_RETRY_BASE_SECONDS` | `3` / `2` | HTTP 429 有界递增退避（appcode 配额 10 次/分钟） |

### 8.3 工作流

```
Round 1  (1 次 API)
├─ 过滤: infer_service_id = service_id AND model_name = model_name
├─ 时间: [time - 60min, time)
├─ 检测: 仅 TTFT 对 SLA（heavy 单点立判 / mild 连续 N 窗；TPOT 走开关）
└─ 活跃性: 事件末端须落在窗口最后 5 分钟内，否则 normal（附 inactive_event_count）

⇣ PLUGIN_DETECT_ONLY=1 时到此返回（status 即「是否仍过载」）

候选选择: 事件窗口内 max(TTFT/SLA)+max(TPOT/SLA) 排序取 top N（无强制上报者）

Round 2  (1 次 API)
├─ 过滤: domain_id IN 候选 AND model_name = model_name
├─ 时间: [time - 60min - 14d, time - 60min)（止于当前窗口前，天然不混入当前数据）
└─ baseline: 同时刻偏移均值（同 main.py）

评分: 固定 both 权重 (0.28125, 0.34375, 0.375)，三维全参与 → Top-K culprits

场景分类（单一 dominant + margin）
├─ 触发指标: rpm / tpm / completion_tokens 各对自身 baseline，ratio ≥ 1.3 算触发
├─ 恰一个触发 → 该场景；多个 → 最大 ratio ≥ 次大 × 1.25 才 dominant，否则 default
├─ rpm_rise_dominant → rpm_limit；tpm_rise_dominant → tpm_limit；
│  output_shift_dominant → compeletion_token_limit；default(mixed) → rpm_limit
└─ 零触发 → 无策略 + note=no_scenario_triggered

Round 3  (1 次 API，仅对有杠杆的 culprits)
├─ 过滤: domain_id IN culprits AND model_name = model_name，事件窗口
├─ 维度: timestamp, domain_id, project_id, resident_model_id, region, infer_service_id
├─ fan-out: 把流量路由到过载池 P 的 (resident_model_id, region) 集合
├─ region_total: 该租户经该常驻服务在「所有池子」上的逐分钟总量取非零均值
└─ value = floor(region_total × s)（s = min(baseline×factor / current, 1)，须 s<1）
   compeletion_token_limit 例外: value = floor(baseline_completion × 1.5)，不放大
```

### 8.4 输出说明

顶层在 `main.py` §5 基础上变化：新增 `mode="proactive"`、`sweep`（巡检回显）、`sla`
（生效 SLA 与来源）、`strategies`；`alert` 改为 `sweep`；normal 时附
`inactive_event_count`（窗口内已结束的历史事件数）。

`strategies` 数组每行（可直接供 maas-monitor 调 maas-manager
`POST /v1/maas/om/add/overload/strategy`）：

| 字段 | 说明 |
| --- | --- |
| `domain_id` | 根因租户 |
| `resident_model_id` | 常驻服务 ID（限流执行点） |
| `region` | 常驻服务所在区域（按站点路由 maas-manager 用） |
| `process_type` | `rpm_limit` / `tpm_limit` / `compeletion_token_limit`（协议原文拼写，含既定笔误） |
| `value` | 建议限流值（整数，≥1）；rpm/tpm 为区域放大后的速率，输出长度为 max_token 上限 |
| `model_name` / `project_id` / `scenario` | 补齐字段：模型名（入参回填）、主导项目（Round 3 按 rpm 份额）、场景溯源 |

`culprit` 在 `main.py` 字段基础上：`scenarios` 数组替换为单个 `scenario` 对象
（`type / process_type / decision(single|margin|mixed) / trigger_ratios / triggered`），
新增 `pool_lever`（池级杠杆明细）与 `region_breakdown`（区域放大明细）。

### 8.5 status 与恢复巡检

| status | 含义 |
| --- | --- |
| `anomaly` | 存在活跃过载事件（detect-only 模式下不含 culprits/strategies） |
| `normal` | 无活跃事件；恢复巡检以此判定「过载已恢复」 |
| `no_data` / `error` | 同 `main.py` |
