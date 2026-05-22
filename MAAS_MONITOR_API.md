# MaaS 监控告警与统一数据查询接口

## 1. 告警规则

MaaS 会继承 Fabric 之前的过载溯源告警能力。告警按 `domainId + infer_service_id + mode` 维度统计，在最近 5 分钟内按分钟聚合判断。

### 1.1 非 GLM 模型告警条件

满足以下任一条件，并且在 5 分钟内出现 3 次，即触发告警：

- 每分钟 `TTFT` 平均值 > `10s`
- 每分钟 `TPOT` 平均值 > `150ms`

### 1.2 GLM 模型告警条件

满足以下任一条件，并且在 5 分钟内出现 3 次，即触发告警：

- 每分钟 `TTFT` 平均值 > `30s`
- 每分钟 `TPOT` 平均值 > `500ms`

### 1.3 告警信息

告警信息包含：

| 字段 | 说明 |
| --- | --- |
| `domainId` | 账号信息 |
| `infer_service_id` | 服务实例 ID |
| `model_name` | 模型名称 |

## 2. 统一数据查询接口

MaaS 支持通过统一数据查询接口查询监控指标数据。

### 2.1 接口信息

| 项目 | 说明 |
| --- | --- |
| URL | `POST /v1/maas/om/data/query` |
| 鉴权方式 | 必须同时携带 `X-Apig-AppCode`、`X-Apply-DomainID`、`X-Apply-ProjectID` 三个 header |
| 限流规则 | 同一个 `appcode` 1 分钟最多调用 10 次 |

鉴权 header 详细说明：

| Header | 说明 |
| --- | --- |
| `X-Apig-AppCode` | API 网关下发的 appcode |
| `X-Apply-DomainID` | 调用方所属租户 ID |
| `X-Apply-ProjectID` | 调用方所属 project ID |

## 3. 请求参数

### 3.1 顶层参数

| 字段 | 必填 | 类型 | 说明 |
| --- | --- | --- | --- |
| `dimensions` | 是 | 数组对象 | 分组维度，对应 `GROUP BY`，支持业务维度和时间维度 |
| `metrics` | 是 | 数组对象 | 指标和聚合函数，对应聚合计算 |
| `filters` | 否 | 数组对象 | 筛选条件，对应 `WHERE`，至少包含一个条件，例如 `domain_id = xx` 或 `infer_service_id = xx` |
| `page` | 否 | 对象 | 分页参数，默认 `pageSize = 1000`，最大 `2000` |

### 3.2 dimensions

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 维度名称，支持业务维度和时间维度 |
| `granularity` | 时间维度必填 | 时间粒度，目前仅支持 `minute` |

支持的维度：

| 字段 | 说明 |
| --- | --- |
| `domain_id` | 租户 ID |
| `infer_service_id` | 服务实例 ID |
| `model_name` | 模型名称 |
| `timestamp` | 时间 |

### 3.3 metrics

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 指标名称 |
| `func` | 是 | 聚合函数，支持 `avg`、`sum` |

查询 `TTFT` 和 `TPOT` 时，传原始指标名 `ttft`、`tpot`，再通过 `func = avg` 指定平均聚合。

支持的指标：

| 指标 | 说明 |
| --- | --- |
| `rpm` | 每分钟请求数 |
| `tpm` | 每分钟 token 数 |
| `ttft` | 首字时延 |
| `tpot` | 逐 token 时延 |
| `prompt_tokens` | 一分钟内平均输入长度 |
| `completion_tokens` | 一分钟内平均输出长度 |
| `success_cnt` | 成功次数 |
| `error_cnt` | 失败次数 |

### 3.4 filters

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 筛选字段，支持所有维度和指标 |
| `operator` | 是 | 运算符，支持 `=`、`!=`、`>`、`<`、`>=`、`<=`、`IN` |
| `value` | 是 | 筛选值，`IN` 支持数组结构 |

### 3.5 page

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `pageNum` | 否 | 页码，默认 `1` |
| `pageSize` | 否 | 每页条数，默认 `1000`，最大 `2000` |

## 4. 请求示例

```bash
curl --location --request POST 'https://modelarts-test-internal.cn-north-7.myhuaweicloud.com/v1/maas/om/data/query' \
--header 'X-Apply-ProjectID: 52ed5b89fd39497eaff88e3589d32d87' \
--header 'X-Apply-DomainID: a027d8f4e6cb4bfe88744c72a6d6d620' \
--header 'X-Apig-AppCode: Ip2Ahr4uaJctA6fLga5L5zqtUiL2NTgtMQMBQc45rTMA4XkdU77l5GSrNp97PYxw' \
--header 'Content-Type: application/json' \
--data-raw '{
    "dimensions": [
        {
            "name": "timestamp",
            "granularity": "minute"
        },
        {
            "name": "domain_id"
        },
        {
            "name": "infer_service_id"
        }
        
    ],
    "metrics": [
        {
            "name": "ttft_avg",
            "func": "avg"
        },
        {
             "name": "success_cnt",
            "func": "avg"
        },
        {
             "name": "error_cnt",
            "func": "avg"
        }
    ],
    "filters": [
        {
            "name": "timestamp",
            "operator": ">=",
            "value": "1779321600000"
        },
        {
            "name": "timestamp",
            "operator": "<=",
            "value": "1779349646000"
        },
        {
            "name": "domain_id",
            "operator": "=",
            "value": "04f258c83e00d5a50f38c00df8021700"
        },
        // 也可以同时过滤多个 domain_id：
        {
            "name": "domain_id",
            "operator": "IN",
            "value": ["a", "b", "c"]
        }
    ],
    "page": {
        "pageNum": 1,
        "pageSize": 1000
    }
}'
```

## 5. 返回体

### 5.1 返回字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | int | `200` 成功；`400` 参数错误；`401` 鉴权失败；`403` 权限不足；`429` 限流；`500` 服务异常 |
| `msg` | string | 返回提示信息 |
| `data.total` | long | 符合条件总条数 |
| `data.list` | 数组对象 | 结果数据集 |
| `data.pageNum` | int | 当前页码 |
| `data.pageSize` | int | 当前页条数 |
| `data.pages` | int | 总页数 |

### 5.2 返回示例

```json
{
  "total": 38,
  "msg": "success",
  "list": [
    {
      "domain_id": "C001",
      "error_cnt": 1,
      "infer_service_id": "",
      "success_cnt": 0,
      "timestamp": 1779345420000,
      "ttft_avg": 85.62
    },
    {
      "domain_id": "C001",
      "error_cnt": 1,
      "infer_service_id": "S001",
      "success_cnt": 0,
      "timestamp": 1779345420000,
      "ttft_avg": 85.62
    },
  ],
  "pageNum": 1,
  "pageSize": 1000,
  "pages": 1
}
```

> 注意！如上所示，返回的数据可能不包含 `infer_service_id` 信息，这种数据需要过滤掉。
