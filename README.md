# 多层配额原子试算 API

基于 **Python + FastAPI + Pydantic v2** 的令牌桶配额试算服务。给定若干令牌桶与一条带时间戳的请求序列，服务按时间顺序模拟连续补充，并对每次请求执行 **桶链原子扣减**：

- 每个桶以 `refill_rate`（令牌/秒）按时间差**连续补充**，余额上限为 `capacity`；
- 请求携带一条桶链 `chain`，链上**每个桶**都需满足 `余额 >= cost`；
- 全部充足 → 链上每个桶原子扣减 `cost`；任一不足 → **全部桶保持不变**，并返回链上第一个不足的桶（`rejected_by`）；
- 逐项返回放行结果、限制桶以及链上各桶扣减前后余额。

另提供 **等待时间预测** 接口 `POST /forecast`：给定当前时刻各桶快照（容量/速率/当前余额）、请求成本与桶链，在**不扣减任何状态**的前提下，计算链上全部桶首次同时满足成本所需的等待秒数，返回预计可通过时刻、等待最长的限制桶与各桶预计余额；零速率或容量不足导致永远不可达时返回明确的不可达标记。

以及 **初始余额规划** 接口 `POST /plan-initial-balances`：请求体与 `/simulate` 相同，以**所有请求均获放行**为目标，在 `[0, capacity]` 内为每个桶反推满足其全部链上消耗的最低 `initial`（误差 ≤ 1e-6），返回各桶 `required_initial`、相对请求 `initial` 的 `delta` 以及用规划值完整回放得到的 `final_balances`；满桶起步仍不可行时返回 `reachable = false` 与按处理顺序首个无法满足的请求和桶。

以及 **配额使用分析** 接口 `POST /usage-report`：请求体在 `/simulate` 基础上增加 `window_seconds`，按与 `/simulate` 完全一致的连续补充与链式原子判定回放请求序列，再按时间窗口统计每个桶的**放行消耗**、**拒绝次数**与**峰值占用**（= `capacity` − 窗口内最低余额），并给出每桶达到峰值的窗口起止时间与全局最忙窗口。

以及 **主体公平性分析** 接口 `POST /fairness-report`：请求在 `/simulate` 的请求字段上增加 `principal`（归属主体）与 `weight`（主体权重，缺省 1），按与 `/simulate` 完全一致的时间排序、连续补充与链式原子判定回放后，按主体统计**放行消耗（实际获得量）**、**拒绝次数**，并以理论权重份额（= 窗口/全局全部放行消耗 × `weight / Σweight`）对齐实际获得量，计算**加权 Jain 公平指数**，找出**最受限主体**及按处理顺序**首个造成其短缺的桶**；权重总和、理论份额、短缺比例与加权 Jain 指数一律基于**未舍入的真实权重与实际获得量**计算（`1e-10` 等微小正权重不会被抹成 0），响应中的 `weight` 也按请求原值返回；响应同时给出**跨全部窗口的全局报告**与**逐窗口报告**（窗口划分语义与 `/usage-report` 一致）。

以及 **区间预留试算** 接口 `POST /reservation-simulate`：给定只有容量上限（无补充概念）的桶与一批携带 `start` / `end` / `cost` / `chain` 的区间预留，按 `start` 升序处理；仅当预留的整个半开区间 `[start, end)` 内链上每个桶的并发占用加 `cost` 处处不超过 `capacity` 时才**原子接受**，否则拒绝且不改变任何占用。返回按原始输入索引排列的逐项结果，以及每个桶**合并后的占用区间**与**峰值并发**。

以及 **候选链路由试算** 接口 `POST /route-simulate`：桶定义与 `/simulate` 相同（共享的连续补充令牌桶），但每个请求把单一 `chain` 换成多条候选链 `chains`。按时间顺序回放，逐请求在**全部桶都有余额**的候选链中，按**扣减后链上最大占用率最小**（占用率 = `(capacity − 扣减后余额) / capacity`，取链上各桶最大值；并列按候选顺序）挑选一条放行，**仅扣选中链**；无候选可行时**所有桶原子不变**，并逐候选给出首个不足桶。响应按原始索引返回选中链、余额快照、拒绝原因与终态余额。

以及 **严格 FIFO 排队试算** 接口 `POST /queue-simulate`：桶与请求字段复用 `/simulate`，并为每个请求增加不早于 `timestamp` 的 `deadline`。请求按（到达时间, 输入顺序）排入**严格 FIFO 队列**，**仅队首可执行**；事件按队首各桶连续补充后的最早可执行时刻、下一次到达与 `deadline` 推进，等待期间到达的请求**不得越过队首**。队首在 `deadline` 前（含 `deadline` 时刻）余额充足即**原子扣减**放行；否则等待至 `deadline` 标记 `expired` 出队——成本超过容量或零补充不可达的队首同样等到 `deadline`——随后继续处理后继。响应按原始输入顺序给出 `executed` / `expired`、实际执行或过期时刻、等待秒数、限制桶与余额快照，并返回终态余额。

以及 **半开滑动窗口试算** 接口 `POST /window-simulate`：给定多个带 `window_seconds` 与 `limit` 的滑动窗口策略（额度按 `principal` 归属主体相互隔离），以及按时间排序、携带 `principal` / `cost` / `policy_refs` 的请求序列。判定前先移除每个引用策略在**半开窗口** `(timestamp - window_seconds, timestamp]` **左端已过期**的记账事件，再统计窗口内该主体的累计消耗；仅当请求引用的**全部**策略都满足 `used + cost <= limit` 时才**原子记账**（向每个策略追加同一事件），否则**不改变任何策略**并返回 `policy_refs` 顺序上**首个超限策略**。响应按原始输入顺序给出 `allowed`、各引用策略的窗口边界、记账前后占用与剩余额度、拒绝原因明细，并返回各策略在最后请求时刻的**终态占用**（逐主体的事件数/占用/剩余）。

以及 **周期结转额度试算** 接口 `POST /rollover-simulate`：给定多个由 `anchor`（周期锚点）、`period_seconds`（周期长度）、`allowance`（每期基础额度）与 `carry_cap`（结转上限）定义的周期结转策略，以及按时间排序、携带 `cost` / `policy_refs` 的请求序列。跨期时**旧结转额一律失效**，上一周期未用的基础额度结转到新周期（成为该周期的结转余额，不超过 `carry_cap`），新周期基础额度重置为满额；消费时**先扣即将失效的结转余额**、再扣本期基础额度。仅当请求引用的**全部**策略两类余额合计都足够时才**原子扣减**，否则**所有策略保持不变**并返回 `policy_refs` 顺序上**首个余额不足策略**。响应按原始输入顺序给出各引用策略的周期编号与周期区间、两类余额扣减前后快照、拒绝原因明细，并返回各策略在最后请求时刻的**终态**（周期/基础额度/结转额）。

以及 **可借贷令牌桶试算** 接口 `POST /debt-simulate`：桶在 `/simulate` 的 `capacity` / `refill_rate` / `initial` 字段上增加累计债务上限 `debt_limit`。每次判定先对链上各桶**连续补充**，但**补充量先偿还债务**、偿还后剩余的部分才进入可用余额（余额仍封顶 `capacity`）；余额不足以支付 `cost` 时允许该桶**借入缺口**，只要累计债务不超过 `debt_limit`（即可支付总额 = 可用余额 + `debt_limit` − 当前债务）；`cost > capacity + debt_limit` 的请求对该桶**永远不可达**（`cost_exceeds_limit`），其余当前借债额度不足的情形以 `debt_limit_exceeded` 拒绝。链上**任一桶**无法在借贷上限内满足成本则整笔**原子拒绝**（链上所有桶的余额/债务保持不变，但补充还债作为时间推进照常发生）。响应按原始索引返回 `allowed`、每桶 `before` / `after` 的 `balance` 与 `debt`、`rejected_by` / 拒绝原因明细，并给出各桶在最后请求时刻的**终态快照**（余额/债务/可立即支付总额）。

以及 **两情景差异对比** 接口 `POST /debt-diff`：请求提交 `baseline` 与 `candidate` 两组与 `/debt-simulate` 完全同格式的债务桶与请求序列，服务对两组情景**各自独立**按补充先偿债、链上原子借贷规则回放，再按**原始请求索引**逐一对齐，返回首个 `allowed` / 拒绝原因分歧（含双方结论）、每个请求各链上桶的余额/债务差值，以及各桶在最后请求时刻的**终态快照差值**。两组情景的请求数量、逐索引请求 ID、逐索引时间戳或桶 ID 集合不一致时，返回带精确定位（情景名 + 索引 + 字段）的 **422** 且不进行任何回放；服务本身无状态、不写入任何数据。

## 一条命令启动

需要已安装 Docker（含 Compose 插件）。在本目录执行：

```bash
docker compose up --build
```

启动后访问地址与端口：

| 用途 | 地址 |
| --- | --- |
| 健康检查 | http://localhost:8000/health |
| 试算接口 | http://localhost:8000/simulate （POST） |
| 预测接口 | http://localhost:8000/forecast （POST） |
| 规划接口 | http://localhost:8000/plan-initial-balances （POST） |
| 使用分析接口 | http://localhost:8000/usage-report （POST） |
| 公平性分析接口 | http://localhost:8000/fairness-report （POST） |
| 预留试算接口 | http://localhost:8000/reservation-simulate （POST） |
| 路由试算接口 | http://localhost:8000/route-simulate （POST） |
| 排队试算接口 | http://localhost:8000/queue-simulate （POST） |
| 滑动窗口试算接口 | http://localhost:8000/window-simulate （POST） |
| 周期结转试算接口 | http://localhost:8000/rollover-simulate （POST） |
| 可借贷桶试算接口 | http://localhost:8000/debt-simulate （POST） |
| 两情景差异对比接口 | http://localhost:8000/debt-diff （POST） |
| Swagger 交互文档 | http://localhost:8000/docs |
| ReDoc 文档 | http://localhost:8000/redoc |

依赖安装与启动初始化均已封装进镜像（见 `Dockerfile` 与 `entrypoint.sh`），宿主机无需 Python 环境。
后台运行可用 `docker compose up --build -d`，停止用 `docker compose down`。

## 请求格式 `POST /simulate`

```json
{
  "buckets": [
    {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "initial": 100},
    {"id": "api_qps", "capacity": 5, "refill_rate": 5}
  ],
  "requests": [
    {"id": "req-1", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-2", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-3", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"]}
  ]
}
```

### 字段说明

**桶 `buckets[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 桶唯一标识，非空字符串 |
| `capacity` | 是 | 桶容量（令牌上限），必须 > 0 |
| `refill_rate` | 是 | 连续补充速率（令牌/秒），必须 ≥ 0 |
| `initial` | 否 | 初始余额，默认等于 `capacity`；要求 0 ≤ initial ≤ capacity |

**请求 `requests[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 请求唯一标识 |
| `timestamp` | 是 | 请求时间（秒，任意一致的时间原点，如相对秒或 Unix 时间） |
| `cost` | 是 | 消耗量，必须 > 0；从链上**每个**桶各扣此数量 |
| `chain` | 是 | 桶 ID 列表（至少 1 个），同一链内不可重复 |

### 模拟语义

1. **时间顺序**：所有请求按 `timestamp` 升序处理；同一时间戳按输入数组顺序处理。`results` 仍按原始数组顺序返回（含 `index`）。
2. **连续补充**：桶在模拟起始时刻（最早请求时间戳）持有 `initial` 余额，之后每次被请求触及时按 `(当前时间 - 上次更新时间) × refill_rate` 惰性补充，封顶 `capacity`。因此时间戳可以是任意大小（Unix 秒等），只取决于时间差。
3. **原子扣减**：判定时先对链上所有桶补充到当前时刻，再按链顺序找第一个余额不足的桶。不足则全部不扣，`balances` 中各桶 `after == before`；充足则每个桶都扣 `cost`。
4. **终态**：`final_balances` 给出所有桶推进到最后一个时间戳后的余额。

## 响应示例

```json
{
  "allowed_count": 2,
  "rejected_count": 1,
  "results": [
    {
      "request_id": "req-1",
      "index": 0,
      "timestamp": 0.0,
      "allowed": true,
      "rejected_by": null,
      "balances": {
        "tenant_quota": {"before": 100.0, "after": 96.0},
        "api_qps": {"before": 5.0, "after": 1.0}
      }
    },
    {
      "request_id": "req-2",
      "index": 1,
      "timestamp": 0.0,
      "allowed": false,
      "rejected_by": "api_qps",
      "balances": {
        "tenant_quota": {"before": 96.0, "after": 96.0},
        "api_qps": {"before": 1.0, "after": 1.0}
      }
    }
  ],
  "final_balances": {"tenant_quota": 96.0, "api_qps": 1.0}
}
```

- `before`：该请求时刻补充之后、扣减之前的余额；
- `after`：扣减之后的余额；被拒绝时与 `before` 相同（原子性）。

## 请求格式 `POST /forecast`

```json
{
  "now": 1000,
  "cost": 4,
  "chain": ["tenant_quota", "api_qps", "user_quota"],
  "buckets": [
    {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "balance": 96},
    {"id": "api_qps", "capacity": 5, "refill_rate": 5, "balance": 1},
    {"id": "user_quota", "capacity": 20, "refill_rate": 1, "balance": 2}
  ]
}
```

### 字段说明

**顶层**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `now` | 是 | 当前时刻（秒，任意一致的时间原点，如相对秒或 Unix 时间） |
| `cost` | 是 | 请求成本，必须 > 0；链上**每个**桶都需持有此数量 |
| `chain` | 是 | 桶 ID 列表（至少 1 个），同一链内不可重复 |
| `buckets` | 是 | 桶状态快照集合（至少 1 个） |

**桶快照 `buckets[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 桶唯一标识，非空字符串 |
| `capacity` | 是 | 桶容量（令牌上限），必须 > 0 |
| `refill_rate` | 是 | 连续补充速率（令牌/秒），必须 ≥ 0 |
| `balance` | 是 | 当前余额（`now` 时刻持有），要求 0 ≤ balance ≤ capacity |

### 预测语义

1. **纯计算**：不扣减、不修改任何状态；`now` 可以是任意一致的时间原点。
2. **单桶等待**：余额已满足 `cost` → 等待 0；否则若 `refill_rate > 0`，等待 `(cost - balance) / refill_rate` 秒。
3. **链等待**：链上各桶等待时间的**最大值**，即全部桶首次同时满足成本的时刻；`pass_at = now + wait_seconds`。
4. **限制桶**：等待时间最长的桶（`limiting_bucket`），并列时取链上首个；立即可通过（等待为 0）时为 `null`。
5. **预计余额**：`projected_balance` 为预计可通过时刻连续补充后的余额（封顶 `capacity`），**不扣减** `cost`。
6. **不可达标记**：`cost > capacity`（`cost_exceeds_capacity`）或余额不足且 `refill_rate == 0`（`zero_refill_rate`）的桶永远不可达；链上任一桶不可达则整体 `reachable = false`，`wait_seconds` / `pass_at` / `limiting_bucket` / `projected_balance` 均为 `null`，各桶 `reason` 标明原因。

### 响应示例（可达）

```json
{
  "reachable": true,
  "wait_seconds": 2.0,
  "pass_at": 1002.0,
  "limiting_bucket": "user_quota",
  "buckets": [
    {"id": "tenant_quota", "balance_now": 96.0, "reachable": true, "reason": null, "wait_seconds": 0.0, "projected_balance": 100.0},
    {"id": "api_qps", "balance_now": 1.0, "reachable": true, "reason": null, "wait_seconds": 0.6, "projected_balance": 5.0},
    {"id": "user_quota", "balance_now": 2.0, "reachable": true, "reason": null, "wait_seconds": 2.0, "projected_balance": 4.0}
  ]
}
```

### 响应示例（不可达，`user_quota` 零速率且余额不足）

```json
{
  "reachable": false,
  "wait_seconds": null,
  "pass_at": null,
  "limiting_bucket": null,
  "buckets": [
    {"id": "tenant_quota", "balance_now": 96.0, "reachable": true, "reason": null, "wait_seconds": 0.0, "projected_balance": null},
    {"id": "api_qps", "balance_now": 1.0, "reachable": true, "reason": null, "wait_seconds": 0.6, "projected_balance": null},
    {"id": "user_quota", "balance_now": 2.0, "reachable": false, "reason": "zero_refill_rate", "wait_seconds": null, "projected_balance": null}
  ]
}
```

## 请求格式 `POST /plan-initial-balances`

请求体与 `/simulate` **完全相同**（`buckets` + `requests`，字段与校验规则一致，`initial` 缺省仍按 `capacity` 计）。接口以**所有请求均获放行**为目标，按与 `/simulate` 相同的时间排序与连续补充语义，为每个桶在 `[0, capacity]` 内反推满足其全部链上消耗的最低初始余额。

```json
{
  "buckets": [
    {"id": "tenant_quota", "capacity": 100, "refill_rate": 10},
    {"id": "api_qps", "capacity": 8, "refill_rate": 5},
    {"id": "user_quota", "capacity": 20, "refill_rate": 1, "initial": 3}
  ],
  "requests": [
    {"id": "req-1", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps", "user_quota"]},
    {"id": "req-2", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-3", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-4", "timestamp": 2, "cost": 4, "chain": ["tenant_quota", "api_qps", "user_quota"]}
  ]
}
```

### 规划语义

1. **目标全量计入**：规划假设每个请求都会放行，链上每个桶都按 `cost` 全量扣减——即使某请求在真实模拟中会因其他桶暂时不足而被原子拒绝，它对链上各桶的消耗也必须计入，不得跳过。
2. **时间顺序与连续补充**：与 `/simulate` 一致——按 `timestamp` 升序（同刻按输入顺序）处理；`initial` 在最早请求时刻持有，之后按 `(时间差) × refill_rate` 连续补充、封顶 `capacity`。
3. **最低 initial**：对每个桶独立反推（自最后一个消耗事件向前递推，跨过补充区间时减去 `refill_rate × 时间差`、不下穿 0），给出恰好满足其全部链上消耗的最低初始余额，误差不超过 1e-6；从未被任何链引用的桶 `required_initial` 为 0。
4. **不可行**：若满桶起步（`initial = capacity`）仍无法让全部请求放行，返回 `reachable = false`，`first_failure` 指出**按处理顺序**首个无法满足的请求与桶（同一请求多个桶不足时取链上首个），此时 `buckets` 与 `final_balances` 为 `null`。
5. **可行**：返回各桶 `required_initial`、相对请求 `initial` 的 `delta`（= `required_initial` − 请求 `initial`，缺省按 `capacity` 计；为正表示请求初始余额不足），以及**用规划值完整回放**得到的 `final_balances`（回放中所有请求均放行）。

### 响应示例（可行）

```json
{
  "reachable": true,
  "buckets": [
    {"id": "tenant_quota", "required_initial": 8.0, "requested_initial": 100.0, "delta": -92.0},
    {"id": "api_qps", "required_initial": 8.0, "requested_initial": 8.0, "delta": 0.0},
    {"id": "user_quota", "required_initial": 6.0, "requested_initial": 3.0, "delta": 3.0}
  ],
  "final_balances": {"tenant_quota": 12.0, "api_qps": 2.0, "user_quota": 0.0},
  "first_failure": null
}
```

- `tenant_quota`：`t=0` 连扣两笔 4，之后每秒补 10 足够覆盖后续请求 → 最低 8；
- `api_qps`：`t=0` 两笔 4 恰好占满容量 → 最低 8（请求的缺省 initial 正好够用，`delta = 0`）；
- `user_quota`：`t=0` 扣 4，`t=2` 前只能补 2，还需 2 → 最低 6，比请求的 `initial: 3` 多 3。

### 响应示例（不可行）

把上例中 `api_qps` 的 `capacity` 改为 5：`t=0` 的 req-1 + req-2 共需 8，满桶 5 也无法满足。

```json
{
  "reachable": false,
  "buckets": null,
  "final_balances": null,
  "first_failure": {"request_id": "req-2", "index": 1, "timestamp": 0.0, "bucket_id": "api_qps", "cost": 4.0, "available": 1.0}
}
```

- `available`：满桶起步回放中，该桶在判定时刻（补充之后、扣减之前）的余额。

## 请求格式 `POST /usage-report`

请求体 = `/simulate` 请求体（`buckets` + `requests`，字段与校验规则完全一致）+ `window_seconds`。接口按与 `/simulate` 相同的时间排序、连续补充与链式原子判定回放请求序列，再按时间窗口统计每个桶的使用情况。

```json
{
  "buckets": [
    {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "initial": 100},
    {"id": "api_qps", "capacity": 5, "refill_rate": 5}
  ],
  "requests": [
    {"id": "req-1", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-2", "timestamp": 0.5, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-3", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
    {"id": "req-4", "timestamp": 3, "cost": 4, "chain": ["tenant_quota"]}
  ],
  "window_seconds": 2
}
```

### 字段说明

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `buckets` | 是 | 同 `/simulate` |
| `requests` | 是 | 同 `/simulate` |
| `window_seconds` | 是 | 统计窗口宽度（秒），必须为正的有限数值；否则返回 422 |

### 统计语义

1. **窗口划分**：以最早请求时刻 `t_start` 为原点，窗口 `k` 为**半开区间** `[t_start + k·window_seconds, t_start + (k+1)·window_seconds)`，覆盖到最后一个请求所在窗口为止。恰好落在窗口边界上的请求属于**右侧**窗口。
2. **归属唯一**：每个请求只计入其 `timestamp` 所在的那个窗口，绝不跨窗重复计数。
3. **回放一致**：放行/拒绝判定与 `/simulate` 完全一致（时间戳升序、同刻按输入顺序；连续补充封顶 `capacity`；链上任一桶不足则全部不扣）。
4. **放行消耗** `allowed_consumption`：窗口内所有放行请求对该桶的 `cost` 合计（请求被放行时，链上每个桶各计一份 `cost`）。
5. **拒绝次数** `rejected_count`：窗口内引用了该桶且被原子拒绝的请求数（无论限制桶是否为该桶本身）。
6. **峰值占用** `peak_occupancy`：`capacity − 窗口内最低余额`，即该窗口内配额被占用（消耗待补充）的峰值。最低余额在窗口起点与每次放行扣减之后采样——余额在事件之间只会因连续补充而上升，因此这些采样点足以刻画窗口最低值。满桶且无人引用的桶占用恒为 0。
7. **峰值窗口** `peak_window`：该桶 `peak_occupancy` 最大的窗口起止时间；并列时取最早窗口。
8. **全局最忙窗口** `busiest_window`：跨桶放行消耗合计最大的窗口（并列取最早），附该窗口的放行/拒绝请求数。

### 响应示例

```json
{
  "window_seconds": 2.0,
  "window_count": 2,
  "windows": [
    {"index": 0, "start": 0.0, "end": 2.0},
    {"index": 1, "start": 2.0, "end": 4.0}
  ],
  "buckets": [
    {
      "id": "tenant_quota",
      "allowed_consumption": 12.0,
      "rejected_count": 1,
      "peak_occupancy": 4.0,
      "peak_window": {"index": 0, "start": 0.0, "end": 2.0},
      "windows": [
        {"index": 0, "allowed_consumption": 8.0, "rejected_count": 1, "peak_occupancy": 4.0},
        {"index": 1, "allowed_consumption": 4.0, "rejected_count": 0, "peak_occupancy": 4.0}
      ]
    },
    {
      "id": "api_qps",
      "allowed_consumption": 8.0,
      "rejected_count": 1,
      "peak_occupancy": 4.0,
      "peak_window": {"index": 0, "start": 0.0, "end": 2.0},
      "windows": [
        {"index": 0, "allowed_consumption": 8.0, "rejected_count": 1, "peak_occupancy": 4.0},
        {"index": 1, "allowed_consumption": 0.0, "rejected_count": 0, "peak_occupancy": 0.0}
      ]
    }
  ],
  "busiest_window": {"index": 0, "start": 0.0, "end": 2.0, "allowed_consumption": 16.0, "allowed_count": 2, "rejected_count": 1}
}
```

- 窗口 0 `[0, 2)`：req-1 放行（`api_qps` 5→1）、req-2 因 `api_qps` 不足被原子拒绝、req-3 放行；`api_qps` 最低余额 1 → 峰值占用 4；
- 窗口 1 `[2, 4)`：req-4 只走 `tenant_quota`；`api_qps` 无事件，余额补满 → 占用回落到 0；
- 最忙窗口为窗口 0：跨桶放行消耗 8 + 8 = 16。

## 请求格式 `POST /fairness-report`

请求体 = `/simulate` 请求体（`buckets` + `requests`，桶字段与跨字段校验完全一致）+ `window_seconds`；其中每个 `requests[]` 在 `/simulate` 字段之上增加 `principal` 与 `weight`。接口按与 `/simulate` 相同的时间排序、连续补充与链式原子判定回放请求序列，再按主体（principal）与时间窗口做公平性统计。

```json
{
  "buckets": [
    {"id": "shared_qps", "capacity": 4, "refill_rate": 1, "initial": 4}
  ],
  "requests": [
    {"id": "req-1", "timestamp": 0, "cost": 3, "chain": ["shared_qps"], "principal": "alpha", "weight": 1},
    {"id": "req-2", "timestamp": 0, "cost": 3, "chain": ["shared_qps"], "principal": "beta", "weight": 1},
    {"id": "req-3", "timestamp": 1, "cost": 3, "chain": ["shared_qps"], "principal": "beta", "weight": 1},
    {"id": "req-4", "timestamp": 5, "cost": 2, "chain": ["shared_qps"], "principal": "alpha", "weight": 1},
    {"id": "req-5", "timestamp": 5, "cost": 2, "chain": ["shared_qps"], "principal": "beta", "weight": 1}
  ],
  "window_seconds": 5
}
```

### 字段说明

**顶层**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `buckets` | 是 | 同 `/simulate` |
| `requests` | 是 | 请求序列，字段在 `/simulate` 基础上增加 `principal` / `weight`（见下表） |
| `window_seconds` | 是 | 统计窗口宽度（秒），必须为正的有限数值；否则返回 422 |

**请求 `requests[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` / `timestamp` / `cost` / `chain` | 是 | 同 `/simulate` |
| `principal` | 是 | 请求归属主体（租户/用户/调用方 ID），非空字符串 |
| `weight` | 否 | 主体权重，必须为正的有限数值（允许 `1e-10` 等微小正权重，按真实数值参与计算并在响应中原样返回），缺省为 `1.0`；同一 `principal` 的权重在整个请求序列中必须按真实数值严格一致，否则返回 422（`inconsistent_principal_weight`） |

### 公平性语义

1. **窗口划分**：与 `/usage-report` 完全一致——以最早请求时刻 `t_start` 为原点，窗口 `k` 为半开区间 `[t_start + k·window_seconds, t_start + (k+1)·window_seconds)`，边界上的请求属于右侧窗口；逐窗口报告只统计**该窗口内出现过**的主体，全局报告覆盖全部主体（按主体首次请求的处理顺序排列）。
2. **回放一致**：放行/拒绝判定与 `/simulate` 完全一致（时间戳升序、同刻按输入顺序；连续补充封顶 `capacity`；链上任一桶不足则全部不扣）。
3. **实际获得量** `allowed_consumption`：主体在该范围（窗口/全局）内所有**放行**请求的 `cost` 合计——注意每个放行请求给主体计一次 `cost`，而不是链上每桶各计一次。
4. **理论权重份额** `theoretical_share`：`该范围全部放行消耗 × weight / Σweight`，其中 `Σweight` 只对该范围内出现过的主体求和。`weight` 全部为 1 时即等份额（总消耗的 1/n）。权重总和与份额始终基于**未舍入的真实权重**计算——`1e-10`、`2e-10` 这类合法微小正权重不会因展示精度被抹成 0，份额为正即按正值报告。
5. **短缺**：`deficit = max(0, theoretical_share − allowed_consumption)`，`deficit_ratio = deficit / theoretical_share`（理论份额为 0 时为 0）；短缺与比例同样基于未舍入份额与实际获得量计算。
6. **加权 Jain 公平指数** `weighted_jain_index`：

   $$J = \frac{(\sum_i x_i)^2}{(\sum_i w_i)\left(\sum_i \frac{x_i^2}{w_i}\right)}$$

   其中 $x_i$ 为主体 $i$ 的实际获得量、$w_i$ 为其**未舍入真实权重**；取值 `(0, 1]`，各主体获得量严格按权重比例分配时为 1，越接近 0 越不公平。等权重（$w_i = 1$）时退化为经典 Jain 指数 $(\sum x_i)^2 / (n \sum x_i^2)$。**仅当**该范围实际获得量合计恰好为 0 时指数定义为 0；只要存在放行消耗，公式就按真实权重与获得量求值（权重经校验严格为正，不会除零）。
7. **最受限主体** `most_constrained_principal`：在该范围**有被拒请求**的主体中，取短缺比例最高者（全部主体获得量均为 0 时按彻底短缺、比例 1 处理）；短缺比例并列时取**首次拒绝时刻最早**（同刻按输入顺序）的主体。没有任何被拒且短缺的主体时为 `null`——已拿到不少于份额的主体即使后续请求被拒，也不视为受限。
8. **首个短缺桶** `first_shortfall`：最受限主体按处理顺序的**第一个被拒请求**，给出请求 ID/下标/时间戳、链上第一个余额不足的桶 `bucket_id`、`cost` 与判定时刻该桶余额 `available`；逐窗口报告中取的是**该窗口内**的首个短缺拒绝。

### 响应示例

```json
{
  "window_seconds": 5.0,
  "window_count": 2,
  "windows": [
    {"index": 0, "start": 0.0, "end": 5.0},
    {"index": 1, "start": 5.0, "end": 10.0}
  ],
  "global_report": {
    "principal_count": 2,
    "request_count": 5,
    "allowed_count": 3,
    "rejected_count": 2,
    "allowed_consumption": 7.0,
    "weighted_jain_index": 0.844827586,
    "most_constrained_principal": "beta",
    "first_shortfall": {
      "request_id": "req-2", "index": 1, "timestamp": 0.0,
      "bucket_id": "shared_qps", "cost": 3.0, "available": 1.0
    },
    "principals": [
      {"principal": "alpha", "weight": 1.0, "request_count": 2, "allowed_count": 2, "rejected_count": 0,
       "allowed_consumption": 5.0, "theoretical_share": 3.5, "deficit": 0.0, "deficit_ratio": 0.0},
      {"principal": "beta", "weight": 1.0, "request_count": 3, "allowed_count": 1, "rejected_count": 2,
       "allowed_consumption": 2.0, "theoretical_share": 3.5, "deficit": 1.5, "deficit_ratio": 0.428571429}
    ]
  },
  "window_reports": [
    {
      "index": 0, "start": 0.0, "end": 5.0,
      "principal_count": 2, "request_count": 3, "allowed_count": 1, "rejected_count": 2,
      "allowed_consumption": 3.0, "weighted_jain_index": 0.5,
      "most_constrained_principal": "beta",
      "first_shortfall": {
        "request_id": "req-2", "index": 1, "timestamp": 0.0,
        "bucket_id": "shared_qps", "cost": 3.0, "available": 1.0
      },
      "principals": [
        {"principal": "alpha", "weight": 1.0, "request_count": 1, "allowed_count": 1, "rejected_count": 0,
         "allowed_consumption": 3.0, "theoretical_share": 1.5, "deficit": 0.0, "deficit_ratio": 0.0},
        {"principal": "beta", "weight": 1.0, "request_count": 2, "allowed_count": 0, "rejected_count": 2,
         "allowed_consumption": 0.0, "theoretical_share": 1.5, "deficit": 1.5, "deficit_ratio": 1.0}
      ]
    },
    {
      "index": 1, "start": 5.0, "end": 10.0,
      "principal_count": 2, "request_count": 2, "allowed_count": 2, "rejected_count": 0,
      "allowed_consumption": 4.0, "weighted_jain_index": 1.0,
      "most_constrained_principal": null, "first_shortfall": null,
      "principals": [
        {"principal": "alpha", "weight": 1.0, "request_count": 1, "allowed_count": 1, "rejected_count": 0,
         "allowed_consumption": 2.0, "theoretical_share": 2.0, "deficit": 0.0, "deficit_ratio": 0.0},
        {"principal": "beta", "weight": 1.0, "request_count": 1, "allowed_count": 1, "rejected_count": 0,
         "allowed_consumption": 2.0, "theoretical_share": 2.0, "deficit": 0.0, "deficit_ratio": 0.0}
      ]
    }
  ]
}
```

- 窗口 0 `[0, 5)`：req-1（alpha，cost 3）放行后余额 1；req-2、req-3（beta，各 cost 3）连续补充后仍不足，均被原子拒绝 → alpha 拿到 3、beta 拿到 0，等权 Jain 指数 0.5，beta 短缺比例 1.0 为最受限主体，首个短缺桶为 t=0 时余额仅 1 的 `shared_qps`；
- 窗口 1 `[5, 10)`：t=5 桶已补满，alpha、beta 各放行一笔 cost 2 → 严格等份额，Jain 指数 1，无最受限主体；
- 全局：alpha 获得 5、beta 获得 2（理论份额各 3.5），加权 Jain 指数 ≈ 0.845，beta 仍为最受限主体。

## 请求格式 `POST /reservation-simulate`

```json
{
  "buckets": [
    {"id": "room_a", "capacity": 2},
    {"id": "room_b", "capacity": 1}
  ],
  "reservations": [
    {"start": 0, "end": 3, "cost": 1, "chain": ["room_a", "room_b"]},
    {"start": 2, "end": 5, "cost": 1, "chain": ["room_a", "room_b"]},
    {"start": 5, "end": 8, "cost": 1, "chain": ["room_a"]},
    {"start": 6, "end": 7, "cost": 1, "chain": ["room_a"]},
    {"start": 7, "end": 9, "cost": 1, "chain": ["room_a", "room_b"]}
  ]
}
```

### 字段说明

**桶 `buckets[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 桶唯一标识，非空字符串 |
| `capacity` | 是 | 桶容量，即任意时刻允许的**并发占用**上限，必须 > 0；此模型没有补充（refill）概念 |

**预留 `reservations[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `start` | 是 | 预留起始时刻（含），秒，任意一致的时间原点 |
| `end` | 是 | 预留结束时刻（不含），必须为有限数值且严格大于 `start` |
| `cost` | 是 | 预留期间对链上**每个**桶的并发占用量，必须 > 0 |
| `chain` | 是 | 桶 ID 列表（至少 1 个），同一链内不可重复 |

### 预留语义

1. **半开区间**：占用区间为 `[start, end)`——在 `start` 时刻生效，在 `end` 时刻释放。两个恰好在端点相接的区间（如 `[0,2)` 与 `[2,4)`）**不算并发**，可各自占满容量。
2. **处理顺序**：所有预留按 `start` 升序处理；同一 `start` 按输入数组顺序处理。`results` 仍按原始数组顺序返回（含 `index`）。
3. **整段原子判定**：接受前对链上每个桶检查，在 `[start, end)` 的**每一处**（含被其他已接受区间部分覆盖的重叠段）并发占用加 `cost` 都不超过 `capacity`；链上第一个放不下的桶记入 `rejected_by`，链上所有桶都不登记该预留（原子拒绝）。即使 `cost <= capacity`，只要区间内存在任何重叠时刻超限也会被拒。
4. **合并占用区间**：`buckets[].intervals` 是该桶所有**已接受**预留叠加后的占用量恒定分段，按起点升序；占用为 0 的空档不输出，端点相接且占用相同的相邻段自动合并。
5. **峰值并发**：`buckets[].peak_occupancy` 为整个时间轴上该桶的最高并发占用量；从未被任何已接受预留引用的桶为 0、`intervals` 为空。

### 响应示例

```json
{
  "allowed_count": 4,
  "rejected_count": 1,
  "results": [
    {"index": 0, "start": 0.0, "end": 3.0, "cost": 1.0, "allowed": true, "rejected_by": null},
    {"index": 1, "start": 2.0, "end": 5.0, "cost": 1.0, "allowed": false, "rejected_by": "room_b"},
    {"index": 2, "start": 5.0, "end": 8.0, "cost": 1.0, "allowed": true, "rejected_by": null},
    {"index": 3, "start": 6.0, "end": 7.0, "cost": 1.0, "allowed": true, "rejected_by": null},
    {"index": 4, "start": 7.0, "end": 9.0, "cost": 1.0, "allowed": true, "rejected_by": null}
  ],
  "buckets": [
    {
      "id": "room_a",
      "capacity": 2.0,
      "peak_occupancy": 2.0,
      "intervals": [
        {"start": 0.0, "end": 3.0, "occupancy": 1.0},
        {"start": 5.0, "end": 6.0, "occupancy": 1.0},
        {"start": 6.0, "end": 8.0, "occupancy": 2.0},
        {"start": 8.0, "end": 9.0, "occupancy": 1.0}
      ]
    },
    {
      "id": "room_b",
      "capacity": 1.0,
      "peak_occupancy": 1.0,
      "intervals": [
        {"start": 0.0, "end": 3.0, "occupancy": 1.0},
        {"start": 7.0, "end": 9.0, "occupancy": 1.0}
      ]
    }
  ]
}
```

- index 0 接受；index 1 与 index 0 在 `[2,3)` 重叠，`room_b` 容量 1 放不下第二笔，被原子拒绝（`room_a` 也不留占用）；
- index 2、3 接受：`room_a` 在 `[6,7)` 峰值并发 2；index 4 在 t=7 与 index 3 端点相接（半开，不并发），链上均有空位，接受。

## 请求格式 `POST /route-simulate`

桶定义与 `/simulate` 相同（共享的连续补充令牌桶）；每个请求把单一 `chain` 换成多条候选链 `chains`，由服务逐请求挑选一条放行。

```json
{
  "buckets": [
    {"id": "chain_a_api", "capacity": 10, "refill_rate": 1, "initial": 10},
    {"id": "chain_b_api", "capacity": 10, "refill_rate": 1, "initial": 6},
    {"id": "shared_db", "capacity": 4, "refill_rate": 0.5, "initial": 4}
  ],
  "requests": [
    {"id": "r1", "timestamp": 0, "cost": 4, "chains": [["chain_a_api", "shared_db"], ["chain_b_api"]]},
    {"id": "r2", "timestamp": 0, "cost": 3, "chains": [["chain_b_api"], ["chain_a_api"]]},
    {"id": "r3", "timestamp": 2, "cost": 4, "chains": [["chain_a_api", "shared_db"], ["chain_b_api"]]},
    {"id": "r4", "timestamp": 3, "cost": 2, "chains": [["chain_b_api"], ["chain_a_api", "shared_db"]]},
    {"id": "r5", "timestamp": 4, "cost": 5, "chains": [["chain_a_api"], ["chain_b_api"]]},
    {"id": "r6", "timestamp": 5, "cost": 6, "chains": [["chain_a_api"], ["chain_b_api"], ["chain_a_api", "shared_db"]]}
  ]
}
```

### 字段说明

**桶 `buckets[]`**：同 `/simulate`（`id` / `capacity` / `refill_rate` / 可选 `initial`）。

**请求 `requests[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 请求唯一标识 |
| `timestamp` | 是 | 请求时间（秒，任意一致的时间原点） |
| `cost` | 是 | 消耗量，必须 > 0；从**选中链**上每个桶各扣此数量 |
| `chains` | 是 | 候选桶链列表（至少 1 条）；每条链至少引用 1 个桶、链内不可重复 |

### 路由语义

1. **时间顺序**：与 `/simulate` 一致——按 `timestamp` 升序处理，同一时刻按输入数组顺序处理；`results` 仍按原始数组顺序返回（含 `index`）。
2. **连续补充**：与 `/simulate` 完全一致的共享令牌桶。请求触及的桶 = 其全部候选链引用桶的并集；判定时这些桶都补充到当前时刻（无论最终放行与否，时间都会推进）。
3. **候选可行**：一条候选链仅当链上**每个桶**余额都 ≥ `cost` 时才可被选（与 `/simulate` 同一 EPS 容差）。
4. **选链规则**：在可行候选中，按**扣减后链上最大占用率最小**选择——占用率 = `(capacity − 扣减后余额) / capacity`，取链上各桶的最大值；并列时按候选顺序取在前者。该规则倾向把请求路由到扣减后负载最轻的链。
5. **原子扣减**：放行时**仅扣选中链**上的桶（每个桶扣 `cost`），其余桶保持不变；无候选可行时拒绝，**所有桶保持不变**，`rejections` 逐候选给出该链上第一个余额不足的桶及其判定时刻余额。
6. **余额快照**：`balances` 覆盖本请求触及的所有桶（全部候选链的并集，按首次出现顺序），`before` 为补充之后、扣减之前的余额，`after` 为扣减之后（未选中链与被拒绝时与 `before` 相同）。
7. **终态**：`final_balances` 给出所有桶推进到最后一个时间戳后的余额。

### 响应示例

```json
{
  "allowed_count": 5,
  "rejected_count": 1,
  "results": [
    {
      "request_id": "r1", "index": 0, "timestamp": 0.0, "allowed": true,
      "selected_chain_index": 1, "selected_chain": ["chain_b_api"],
      "balances": {
        "chain_a_api": {"before": 10.0, "after": 10.0},
        "shared_db": {"before": 4.0, "after": 4.0},
        "chain_b_api": {"before": 6.0, "after": 2.0}
      },
      "rejections": null
    },
    {
      "request_id": "r2", "index": 1, "timestamp": 0.0, "allowed": true,
      "selected_chain_index": 1, "selected_chain": ["chain_a_api"],
      "balances": {
        "chain_b_api": {"before": 2.0, "after": 2.0},
        "chain_a_api": {"before": 10.0, "after": 7.0}
      },
      "rejections": null
    },
    {
      "request_id": "r3", "index": 2, "timestamp": 2.0, "allowed": true,
      "selected_chain_index": 0, "selected_chain": ["chain_a_api", "shared_db"],
      "balances": {
        "chain_a_api": {"before": 9.0, "after": 5.0},
        "shared_db": {"before": 4.0, "after": 0.0},
        "chain_b_api": {"before": 4.0, "after": 4.0}
      },
      "rejections": null
    },
    {
      "request_id": "r4", "index": 3, "timestamp": 3.0, "allowed": true,
      "selected_chain_index": 0, "selected_chain": ["chain_b_api"],
      "balances": {
        "chain_b_api": {"before": 5.0, "after": 3.0},
        "chain_a_api": {"before": 6.0, "after": 6.0},
        "shared_db": {"before": 0.5, "after": 0.5}
      },
      "rejections": null
    },
    {
      "request_id": "r5", "index": 4, "timestamp": 4.0, "allowed": true,
      "selected_chain_index": 0, "selected_chain": ["chain_a_api"],
      "balances": {
        "chain_a_api": {"before": 7.0, "after": 2.0},
        "chain_b_api": {"before": 4.0, "after": 4.0}
      },
      "rejections": null
    },
    {
      "request_id": "r6", "index": 5, "timestamp": 5.0, "allowed": false,
      "selected_chain_index": null, "selected_chain": null,
      "balances": {
        "chain_a_api": {"before": 3.0, "after": 3.0},
        "chain_b_api": {"before": 5.0, "after": 5.0},
        "shared_db": {"before": 1.5, "after": 1.5}
      },
      "rejections": [
        {"chain_index": 0, "rejected_by": "chain_a_api", "available": 3.0},
        {"chain_index": 1, "rejected_by": "chain_b_api", "available": 5.0},
        {"chain_index": 2, "rejected_by": "chain_a_api", "available": 3.0}
      ]
    }
  ],
  "final_balances": {"chain_a_api": 3.0, "chain_b_api": 5.0, "shared_db": 1.5}
}
```

- r1：两条候选都可行；候选 0 扣后 `shared_db` 占用率 (4−0)/4 = 1.0，候选 1 扣后 `chain_b_api` 占用率 (10−2)/10 = 0.8 → 选候选 1，仅 `chain_b_api` 扣 4；
- r2：候选 0 的 `chain_b_api` 余额 2 不足，落到候选 1，`chain_a_api` 10→7；
- r3：t=2 补充后两条候选都可行，且扣减后最大占用率同为 1.0（候选 0 受 `shared_db` 限制、候选 1 受 `chain_b_api` 限制）→ 并列按候选顺序选候选 0，`chain_a_api` 9→5、`shared_db` 4→0；
- r4：候选 1 因 `shared_db`（0.5）不足被跳过，选候选 0，`chain_b_api` 5→3；
- r5：候选 1 的 `chain_b_api`（4）不足，选候选 0，`chain_a_api` 7→2；
- r6：三条候选的首个不足桶分别为 `chain_a_api`（3）、`chain_b_api`（5）、`chain_a_api`（3），全部不可行 → 原子拒绝，所有桶保持，`rejections` 逐候选列出。

## 请求格式 `POST /queue-simulate`

桶定义与请求字段复用 `/simulate`，每个请求额外携带不早于 `timestamp` 的 `deadline`；请求按（到达时间, 输入顺序）排入严格 FIFO 队列，仅队首可执行。

```json
{
  "buckets": [
    {"id": "api_qps", "capacity": 4, "refill_rate": 1, "initial": 4},
    {"id": "db_quota", "capacity": 10, "refill_rate": 2, "initial": 10}
  ],
  "requests": [
    {"id": "r1", "timestamp": 0, "cost": 3, "chain": ["api_qps", "db_quota"], "deadline": 5},
    {"id": "r2", "timestamp": 0.5, "cost": 3, "chain": ["api_qps"], "deadline": 6},
    {"id": "r3", "timestamp": 1, "cost": 2, "chain": ["db_quota"], "deadline": 2.5},
    {"id": "r4", "timestamp": 2, "cost": 5, "chain": ["api_qps"], "deadline": 4},
    {"id": "r5", "timestamp": 3, "cost": 4, "chain": ["api_qps"], "deadline": 10}
  ]
}
```

### 字段说明

**桶 `buckets[]`**：同 `/simulate`（`id` / `capacity` / `refill_rate` / 可选 `initial`）。

**请求 `requests[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` / `timestamp` / `cost` / `chain` | 是 | 同 `/simulate` |
| `deadline` | 是 | 最晚可执行时刻（秒），必须为有限数值且**不早于 `timestamp`**，否则返回 422 |

### 排队语义

1. **严格 FIFO**：请求按（`timestamp`, 输入顺序）排队，仅队首可执行；`results` 仍按原始数组顺序返回（含 `index`）。
2. **队首等待**：队首从成为队首的时刻 `t0`（自身到达与前序队首出队的较晚者）起等待。链上各桶连续补充后全部满足 `cost` 的最早时刻即最早可执行时刻 `t_exec`；`cost > capacity` 或零补充且余额不足的桶永远不可达（`t_exec` 为无穷大）。事件按 `t_exec`、下一次到达与 `deadline` 推进；等待期间到达的请求排入队尾，**不得越过队首**。
3. **执行**：`t_exec` 不晚于 `deadline` 时在 `t_exec` 对链上每个桶**原子扣减** `cost`——即 `deadline` 时刻余额仍充足也一定会执行。
4. **过期**：否则等待至 `deadline` 标记 `expired` 出队（不可达的队首同样等到 `deadline`；若成为队首时 `deadline` 已过，则立即过期），链上桶时间推进到过期时刻但不扣减（原子性），随后继续处理后继。
5. **等待秒数** `wait_seconds`：从到达（`timestamp`）到执行/过期时刻的等待时长。
6. **限制桶** `limiting_bucket`：链上就绪最晚的桶（并列取链上首个）；成为队首即可执行（无需等待任何桶）时为 `null`。
7. **余额快照**：`balances` 覆盖链上各桶，`before` 为判定时刻（执行/过期时刻）补充之后、扣减之前的余额，`after` 为扣减之后（过期时与 `before` 相同）。
8. **终态**：`final_balances` 给出所有桶推进到最后一个请求出队时刻后的余额。

### 响应示例

```json
{
  "executed_count": 4,
  "expired_count": 1,
  "results": [
    {
      "request_id": "r1", "index": 0, "timestamp": 0.0, "deadline": 5.0,
      "status": "executed", "executed_at": 0.0, "expired_at": null,
      "wait_seconds": 0.0, "limiting_bucket": null,
      "balances": {
        "api_qps": {"before": 4.0, "after": 1.0},
        "db_quota": {"before": 10.0, "after": 7.0}
      }
    },
    {
      "request_id": "r2", "index": 1, "timestamp": 0.5, "deadline": 6.0,
      "status": "executed", "executed_at": 2.0, "expired_at": null,
      "wait_seconds": 1.5, "limiting_bucket": "api_qps",
      "balances": {"api_qps": {"before": 3.0, "after": 0.0}}
    },
    {
      "request_id": "r3", "index": 2, "timestamp": 1.0, "deadline": 2.5,
      "status": "executed", "executed_at": 2.0, "expired_at": null,
      "wait_seconds": 1.0, "limiting_bucket": null,
      "balances": {"db_quota": {"before": 10.0, "after": 8.0}}
    },
    {
      "request_id": "r4", "index": 3, "timestamp": 2.0, "deadline": 4.0,
      "status": "expired", "executed_at": null, "expired_at": 4.0,
      "wait_seconds": 2.0, "limiting_bucket": "api_qps",
      "balances": {"api_qps": {"before": 2.0, "after": 2.0}}
    },
    {
      "request_id": "r5", "index": 4, "timestamp": 3.0, "deadline": 10.0,
      "status": "executed", "executed_at": 6.0, "expired_at": null,
      "wait_seconds": 3.0, "limiting_bucket": "api_qps",
      "balances": {"api_qps": {"before": 4.0, "after": 0.0}}
    }
  ],
  "final_balances": {"api_qps": 0.0, "db_quota": 10.0}
}
```

- r1：到达即队首，余额充足，t=0 立即执行（`api_qps` 4→1、`db_quota` 10→7）；
- r2：t=0.5 成为队首时 `api_qps` 仅 1.5，以 1/s 补到 3 需等到 t=2.0，早于 deadline 6 → 在 t=2.0 执行；
- r3：t=1 到达时 `db_quota` 余额已充足，但 r2 仍是队首，**不得越过**；r2 在 t=2.0 出队后 r3 立即成为队首并执行；
- r4：cost 5 超过 `api_qps` 容量 4，永远不可达，仍等待至 deadline t=4 才过期出队，不扣减；
- r5：t=3 到达，等 r4 在 t=4 过期后成为队首，`api_qps` 补到 4 需等到 t=6 → 在 t=6 执行。

## 请求格式 `POST /window-simulate`

给定多个**半开滑动窗口**配额策略与一条带 `principal`（归属主体）的请求序列，按时间顺序做链上原子记账。与令牌桶不同，滑动窗口**没有补充概念**：额度随窗口左端滑动而自动释放。

```json
{
  "policies": [
    {"id": "per_second", "window_seconds": 1, "limit": 10},
    {"id": "per_minute", "window_seconds": 60, "limit": 15}
  ],
  "requests": [
    {"id": "r1", "timestamp": 0, "principal": "alpha", "cost": 6, "policy_refs": ["per_second", "per_minute"]},
    {"id": "r2", "timestamp": 0, "principal": "alpha", "cost": 6, "policy_refs": ["per_second", "per_minute"]},
    {"id": "r3", "timestamp": 1, "principal": "alpha", "cost": 6, "policy_refs": ["per_second", "per_minute"]},
    {"id": "r4", "timestamp": 1, "principal": "alpha", "cost": 4, "policy_refs": ["per_minute"]},
    {"id": "r5", "timestamp": 2, "principal": "beta", "cost": 9, "policy_refs": ["per_second"]}
  ]
}
```

### 字段说明

**策略 `policies[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 窗口策略唯一标识，非空字符串 |
| `window_seconds` | 是 | 滑动窗口宽度（秒），必须为正的有限数值 |
| `limit` | 是 | 窗口内按主体统计的累计消耗上限，必须为正的有限数值 |

**请求 `requests[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 请求唯一标识 |
| `timestamp` | 是 | 请求时间（秒，任意一致的时间原点），可乱序输入，服务内部按时间排序 |
| `principal` | 是 | 请求归属主体（租户/用户/调用方 ID），非空字符串；不同主体的窗口额度**相互独立** |
| `cost` | 是 | 消耗量，必须 > 0；对每个引用策略各计此数量 |
| `policy_refs` | 是 | 引用的策略 ID 列表（至少 1 个），同一请求内不可重复；按此顺序查找首个超限策略 |

### 滑动窗口语义

1. **半开窗口**：请求在时刻 `t` 判定时，窗口为 `(t - window_seconds, t]`——**右端含、左端不含**。恰好在左边界到期（事件时间 == `t - window_seconds`）的事件视为已过期被移除；恰好在右端（`t` 本身）的事件在窗口内。
2. **判定前过期清理**：每次判定前，先从该策略该主体的事件列表左端弹出所有已过期事件，再对窗口内剩余事件的 `cost` 求和得到 `used_before`。
3. **主体隔离**：每个 `(策略, principal)` 组合拥有独立的事件账本与独立的 `limit` 额度；一个主体用满不影响其他主体。
4. **时间顺序**：请求按 `timestamp` 升序处理，同一时刻按输入数组顺序处理；`results` 仍按原始数组顺序返回（含 `index`）。
5. **原子记账**：仅当请求引用的**全部**策略都满足 `used_before + cost <= limit` 时才放行，并一次性向每个引用策略的该主体账本追加 `(timestamp, cost)`；任一策略超限即拒绝，**所有策略保持不变**（连本次过期清理之外的状态都不新增），`rejected_by` 取 `policy_refs` 顺序上首个超限的策略，`rejection` 给出 `used` / `required` / `available` 等明细。
6. **拒绝原因**：固定为 `limit_exceeded`；单笔 `cost > limit`（即使窗口为空）同样以超限拒绝而非 422。
7. **剩余额度**：`remaining_before = limit - used_before`；放行时 `remaining_after = limit - used_before - cost`，拒绝时与 `remaining_before` 相同。
8. **终态占用**：`policies[]` 以**最后一个请求的时间戳**为观察时刻，逐策略给出半开窗口边界、跨主体累计消耗 `total_used`，以及逐主体的窗口内有效事件数 `event_count`、`used` 与 `remaining`（按各主体首次成功记账的顺序；终态窗口内无有效事件的主体不输出）。

### 响应示例

```json
{
  "allowed_count": 3,
  "rejected_count": 2,
  "results": [
    {
      "request_id": "r1", "index": 0, "timestamp": 0.0, "principal": "alpha", "cost": 6.0,
      "allowed": true, "rejected_by": null, "rejection": null,
      "policies": {
        "per_second": {
          "id": "per_second", "window_seconds": 1.0, "limit": 10.0,
          "window": {"start": -1.0, "end": 0.0},
          "used_before": 0.0, "used_after": 6.0,
          "remaining_before": 10.0, "remaining_after": 4.0
        },
        "per_minute": {
          "id": "per_minute", "window_seconds": 60.0, "limit": 15.0,
          "window": {"start": -60.0, "end": 0.0},
          "used_before": 0.0, "used_after": 6.0,
          "remaining_before": 15.0, "remaining_after": 9.0
        }
      }
    },
    {
      "request_id": "r2", "index": 1, "timestamp": 0.0, "principal": "alpha", "cost": 6.0,
      "allowed": false, "rejected_by": "per_second",
      "rejection": {
        "policy_id": "per_second", "reason": "limit_exceeded",
        "window_seconds": 1.0, "limit": 10.0,
        "used": 6.0, "cost": 6.0, "required": 12.0, "available": 4.0,
        "window": {"start": -1.0, "end": 0.0}
      },
      "policies": {
        "per_second": {"id": "per_second", "window_seconds": 1.0, "limit": 10.0,
          "window": {"start": -1.0, "end": 0.0},
          "used_before": 6.0, "used_after": 6.0,
          "remaining_before": 4.0, "remaining_after": 4.0},
        "per_minute": {"id": "per_minute", "window_seconds": 60.0, "limit": 15.0,
          "window": {"start": -60.0, "end": 0.0},
          "used_before": 6.0, "used_after": 6.0,
          "remaining_before": 9.0, "remaining_after": 9.0}
      }
    },
    {
      "request_id": "r3", "index": 2, "timestamp": 1.0, "principal": "alpha", "cost": 6.0,
      "allowed": true, "rejected_by": null, "rejection": null,
      "policies": {
        "per_second": {"id": "per_second", "window_seconds": 1.0, "limit": 10.0,
          "window": {"start": 0.0, "end": 1.0},
          "used_before": 0.0, "used_after": 6.0,
          "remaining_before": 10.0, "remaining_after": 4.0},
        "per_minute": {"id": "per_minute", "window_seconds": 60.0, "limit": 15.0,
          "window": {"start": -59.0, "end": 1.0},
          "used_before": 6.0, "used_after": 12.0,
          "remaining_before": 9.0, "remaining_after": 3.0}
      }
    },
    {
      "request_id": "r4", "index": 3, "timestamp": 1.0, "principal": "alpha", "cost": 4.0,
      "allowed": false, "rejected_by": "per_minute",
      "rejection": {
        "policy_id": "per_minute", "reason": "limit_exceeded",
        "window_seconds": 60.0, "limit": 15.0,
        "used": 12.0, "cost": 4.0, "required": 16.0, "available": 3.0,
        "window": {"start": -59.0, "end": 1.0}
      },
      "policies": {
        "per_minute": {"id": "per_minute", "window_seconds": 60.0, "limit": 15.0,
          "window": {"start": -59.0, "end": 1.0},
          "used_before": 12.0, "used_after": 12.0,
          "remaining_before": 3.0, "remaining_after": 3.0}
      }
    },
    {
      "request_id": "r5", "index": 4, "timestamp": 2.0, "principal": "beta", "cost": 9.0,
      "allowed": true, "rejected_by": null, "rejection": null,
      "policies": {
        "per_second": {"id": "per_second", "window_seconds": 1.0, "limit": 10.0,
          "window": {"start": 1.0, "end": 2.0},
          "used_before": 0.0, "used_after": 9.0,
          "remaining_before": 10.0, "remaining_after": 1.0}
      }
    }
  ],
  "policies": [
    {
      "id": "per_second", "window_seconds": 1.0, "limit": 10.0, "total_used": 9.0,
      "window": {"start": 1.0, "end": 2.0},
      "principals": [
        {"principal": "beta", "event_count": 1, "used": 9.0, "remaining": 1.0,
         "window": {"start": 1.0, "end": 2.0}}
      ]
    },
    {
      "id": "per_minute", "window_seconds": 60.0, "limit": 15.0, "total_used": 12.0,
      "window": {"start": -58.0, "end": 2.0},
      "principals": [
        {"principal": "alpha", "event_count": 2, "used": 12.0, "remaining": 3.0,
         "window": {"start": -58.0, "end": 2.0}}
      ]
    }
  ]
}
```

- r1：两策略窗口均空，6 ≤ 10 且 6 ≤ 15 → 原子记账；
- r2：同刻 alpha 在两策略窗口内各有 6，`6 + 6 = 12 > 10` 先撞 `per_second` → 原子拒绝，`per_minute` 也保持 6；
- r3：t=1 时 `per_second` 窗口为 `(0,1]`，t=0 的事件恰在左边界已过期（used 0，放行）；`per_minute` 窗口仍含该事件（used 6，6+6=12 ≤ 15，放行）；
- r4：仅引用 `per_minute`，used 12 + 4 = 16 > 15 → 拒绝；
- r5：beta 是独立主体，`per_second` 上拥有自己的满额度 10，cost 9 放行；
- 终态观察时刻 t=2：`per_second` 窗口 `(1,2]` 内只剩 beta 的 9（alpha t=1 的事件在左边界过期）；`per_minute` 窗口 `(-58,2]` 内 alpha 两笔共 12。

## 请求格式 `POST /rollover-simulate`

给定多个**周期结转额度策略**与一条请求序列，按时间顺序做跨期结转与链上原子扣减。与滑动窗口一样没有补充概念：每个周期初重新发放满额基础额度，上一周期未用的基础额度可按上限结转一次。

```json
{
  "policies": [
    {"id": "p10", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 5},
    {"id": "p7",  "anchor": 0, "period_seconds": 7,  "allowance": 7,  "carry_cap": 3}
  ],
  "requests": [
    {"id": "r1", "timestamp": 0,  "cost": 4, "policy_refs": ["p10", "p7"]},
    {"id": "r2", "timestamp": 0,  "cost": 4, "policy_refs": ["p10", "p7"]},
    {"id": "r3", "timestamp": 9,  "cost": 4, "policy_refs": ["p10", "p7"]},
    {"id": "r4", "timestamp": 10, "cost": 4, "policy_refs": ["p10", "p7"]},
    {"id": "r5", "timestamp": 10, "cost": 7, "policy_refs": ["p10"]}
  ]
}
```

### 字段说明

**策略 `policies[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 结转策略唯一标识，非空字符串 |
| `anchor` | 是 | 周期对齐锚点时刻（秒，任意一致的时间原点），可为负数 |
| `period_seconds` | 是 | 周期长度（秒），必须为正的有限数值 |
| `allowance` | 是 | 每个周期发放的基础额度，必须为正的有限数值 |
| `carry_cap` | 是 | 上一周期未用基础额度结转到下一周期的上限，必须为非负有限数值；`0` 表示完全不结转 |

**请求 `requests[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 请求唯一标识 |
| `timestamp` | 是 | 请求时间（秒，任意一致的时间原点），可乱序输入，服务内部按时间排序 |
| `cost` | 是 | 消耗量，必须 > 0；对每个引用策略各扣此数量 |
| `policy_refs` | 是 | 引用的策略 ID 列表（至少 1 个），同一请求内不可重复；按此顺序查找首个余额不足策略 |

### 周期结转语义

1. **周期编号**：时刻 `t` 所在周期编号为 `floor((t - anchor) / period_seconds)`；周期 `k` 为半开区间 `[anchor + k·period_seconds, anchor + (k+1)·period_seconds)`。恰好在周期边界上的请求属于**右侧**新周期。`anchor` 之后的周期从 0 开始编号，之前为负编号。周期归属按输入的**十进制数值**精确判定（内部以分数运算求 floor），不受二进制浮点误差影响——`period_seconds` 可以是小数：例如 `anchor = 0`、`period_seconds = 0.1` 时，`t = 0.3` 恰好是周期 3 的起点并进入新周期（而非被浮点误差 `0.3/0.1 = 2.9999999999999996` 错归周期 2），`t = 0.299999999` 仍属周期 2。逐项结果中的 `period` / `period_window` 与终态 `policies[]` 始终按同一周期归属给出。
2. **惰性起步**：策略在**首次被请求触及**的那个周期开始时拥有基础额度余额 `base = allowance`、结转余额 `carry = 0`；没有任何请求引用的策略在终态时刻按其所在周期以满额、零结转起步。
3. **跨期结转**：时间推进到更后的周期时逐期结转——旧 `carry` **立即失效**（绝不跨多期累加），上一周期未用的基础额度结转为新周期的 `carry = min(carry_cap, 上一期未用 base)`，新周期 `base` 重置为 `allowance`。跨多个空周期时结转只在相邻周期间传递：空转一期后 `carry = min(carry_cap, allowance)`，此后保持该不动点。
4. **时间顺序**：请求按 `timestamp` 升序处理，同一时刻按输入数组顺序处理；`results` 仍按原始数组顺序返回（含 `index`）。跨期结转属于**时间推进**——即使请求最终被拒，引用策略的周期与结转也照常前进，只是余额不被扣减。
5. **扣减顺序**：消费时先扣**即将失效的结转余额** `carry`（`carry -= min(carry, cost)`），不足部分再扣本期基础额度 `base`。
6. **原子判定**：仅当请求引用的**全部**策略都满足 `base + carry >= cost`（同一 EPS 容差）时才一次性扣减；任一策略合计不足即拒绝，**所有策略保持不变**，`rejected_by` 取 `policy_refs` 顺序上首个不足的策略，`rejection` 给出 `base_available` / `carry_available` / `available` / `shortage` 等明细。单笔 `cost` 超过两类余额合计（即使新周期满额加结转仍不够）同样以 `insufficient_balance` 拒绝而非 422。
7. **两类余额快照**：每个引用策略的 `balances.base` 与 `balances.carry` 分别给出 `before`（跨期结转之后、扣减之前）与 `after`（扣减之后；被拒绝时与 `before` 相同）。
8. **终态**：`policies[]` 以**最后一个请求的时间戳**为观察时刻，逐策略给出周期编号、周期区间、`base` / `carry` 终态与合计 `available`，按 `policies` 输入顺序排列。

### 响应示例

```json
{
  "allowed_count": 4,
  "rejected_count": 1,
  "results": [
    {
      "request_id": "r1", "index": 0, "timestamp": 0.0, "cost": 4.0,
      "allowed": true, "rejected_by": null, "rejection": null,
      "policies": {
        "p10": {
          "id": "p10", "period": 0,
          "period_window": {"index": 0, "start": 0.0, "end": 10.0},
          "allowance": 10.0, "carry_cap": 5.0,
          "balances": {
            "base": {"before": 10.0, "after": 6.0},
            "carry": {"before": 0.0, "after": 0.0}
          }
        },
        "p7": {
          "id": "p7", "period": 0,
          "period_window": {"index": 0, "start": 0.0, "end": 7.0},
          "allowance": 7.0, "carry_cap": 3.0,
          "balances": {
            "base": {"before": 7.0, "after": 3.0},
            "carry": {"before": 0.0, "after": 0.0}
          }
        }
      }
    },
    {
      "request_id": "r2", "index": 1, "timestamp": 0.0, "cost": 4.0,
      "allowed": false, "rejected_by": "p7",
      "rejection": {
        "policy_id": "p7", "reason": "insufficient_balance",
        "period": 0, "period_window": {"index": 0, "start": 0.0, "end": 7.0},
        "allowance": 7.0, "carry_cap": 3.0,
        "cost": 4.0, "base_available": 3.0, "carry_available": 0.0,
        "available": 3.0, "shortage": 1.0
      },
      "policies": {
        "p10": {
          "id": "p10", "period": 0,
          "period_window": {"index": 0, "start": 0.0, "end": 10.0},
          "allowance": 10.0, "carry_cap": 5.0,
          "balances": {
            "base": {"before": 6.0, "after": 6.0},
            "carry": {"before": 0.0, "after": 0.0}
          }
        },
        "p7": {
          "id": "p7", "period": 0,
          "period_window": {"index": 0, "start": 0.0, "end": 7.0},
          "allowance": 7.0, "carry_cap": 3.0,
          "balances": {
            "base": {"before": 3.0, "after": 3.0},
            "carry": {"before": 0.0, "after": 0.0}
          }
        }
      }
    },
    {
      "request_id": "r3", "index": 2, "timestamp": 9.0, "cost": 4.0,
      "allowed": true, "rejected_by": null, "rejection": null,
      "policies": {
        "p10": {
          "id": "p10", "period": 0,
          "period_window": {"index": 0, "start": 0.0, "end": 10.0},
          "allowance": 10.0, "carry_cap": 5.0,
          "balances": {
            "base": {"before": 6.0, "after": 2.0},
            "carry": {"before": 0.0, "after": 0.0}
          }
        },
        "p7": {
          "id": "p7", "period": 1,
          "period_window": {"index": 1, "start": 7.0, "end": 14.0},
          "allowance": 7.0, "carry_cap": 3.0,
          "balances": {
            "base": {"before": 7.0, "after": 6.0},
            "carry": {"before": 3.0, "after": 0.0}
          }
        }
      }
    },
    {
      "request_id": "r4", "index": 3, "timestamp": 10.0, "cost": 4.0,
      "allowed": true, "rejected_by": null, "rejection": null,
      "policies": {
        "p10": {
          "id": "p10", "period": 1,
          "period_window": {"index": 1, "start": 10.0, "end": 20.0},
          "allowance": 10.0, "carry_cap": 5.0,
          "balances": {
            "base": {"before": 10.0, "after": 8.0},
            "carry": {"before": 2.0, "after": 0.0}
          }
        },
        "p7": {
          "id": "p7", "period": 1,
          "period_window": {"index": 1, "start": 7.0, "end": 14.0},
          "allowance": 7.0, "carry_cap": 3.0,
          "balances": {
            "base": {"before": 6.0, "after": 2.0},
            "carry": {"before": 0.0, "after": 0.0}
          }
        }
      }
    },
    {
      "request_id": "r5", "index": 4, "timestamp": 10.0, "cost": 7.0,
      "allowed": true, "rejected_by": null, "rejection": null,
      "policies": {
        "p10": {
          "id": "p10", "period": 1,
          "period_window": {"index": 1, "start": 10.0, "end": 20.0},
          "allowance": 10.0, "carry_cap": 5.0,
          "balances": {
            "base": {"before": 8.0, "after": 1.0},
            "carry": {"before": 0.0, "after": 0.0}
          }
        }
      }
    }
  ],
  "policies": [
    {
      "id": "p10", "anchor": 0.0, "period_seconds": 10.0,
      "allowance": 10.0, "carry_cap": 5.0,
      "period": 1, "period_window": {"index": 1, "start": 10.0, "end": 20.0},
      "base": 1.0, "carry": 0.0, "available": 1.0
    },
    {
      "id": "p7", "anchor": 0.0, "period_seconds": 7.0,
      "allowance": 7.0, "carry_cap": 3.0,
      "period": 1, "period_window": {"index": 1, "start": 7.0, "end": 14.0},
      "base": 2.0, "carry": 0.0, "available": 2.0
    }
  ]
}
```

- r1：两策略周期 0 均满额起步，各扣 4：`p10` base 10→6、`p7` base 7→3；
- r2：`p7` 仅剩 3 < 4（缺口 1），按引用顺序（`p10` → `p7`）撞 `p7` → 原子拒绝，`p10` 也保持 6；
- r3：t=9 时 `p10` 仍在周期 0，base 6 扣 4 → 2；`p7` 已跨入周期 1——周期 0 未用 base 3 结转为 `carry=min(3,3)=3`、旧结转（0）失效、base 重置为 7，cost 4 先扣结转 3（→0）再扣 base 1（7→6）；
- r4：t=10 时 `p10` 跨入周期 1，周期 0 未用 base 2 → `carry=2`、base 重置为 10，cost 4 先扣结转 2（→0）再扣 base 2（10→8）；`p7` 仍在周期 1，base 6→2；
- r5：同刻只引用 `p10`，可用 8 ≥ 7 → base 8→1；
- 终态观察时刻 t=10：`p10` 在周期 1（区间 `[10,20)`）base=1、carry=0；`p7` 在周期 1（区间 `[7,14)`）base=2、carry=0。

## 请求格式 `POST /debt-simulate`

桶在 `/simulate` 的字段上增加 `debt_limit`（累计债务上限）：余额不足时允许借债，但每次连续补充的令牌**先用于还债**，只有还清债务后的补充才进入可用余额。

```json
{
  "buckets": [
    {"id": "api_qps",  "capacity": 10, "refill_rate": 1, "initial": 2, "debt_limit": 5},
    {"id": "db_conns", "capacity": 4,  "refill_rate": 0, "initial": 0, "debt_limit": 4}
  ],
  "requests": [
    {"id": "r1", "timestamp": 0, "cost": 4, "chain": ["api_qps", "db_conns"]},
    {"id": "r2", "timestamp": 0, "cost": 9, "chain": ["api_qps"]},
    {"id": "r3", "timestamp": 3, "cost": 2, "chain": ["api_qps"]},
    {"id": "r4", "timestamp": 3, "cost": 1, "chain": ["api_qps", "db_conns"]},
    {"id": "r5", "timestamp": 3, "cost": 20, "chain": ["api_qps"]}
  ]
}
```

### 字段说明

**桶 `buckets[]`**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 桶唯一标识，非空字符串 |
| `capacity` | 是 | 桶容量（可用余额上限），必须 > 0 |
| `refill_rate` | 是 | 连续补充速率（令牌/秒），必须 ≥ 0 |
| `initial` | 否 | 初始可用余额，默认等于 `capacity`；要求 0 ≤ initial ≤ capacity。所有桶初始债务为 0 |
| `debt_limit` | 是 | 累计债务上限，必须为非负有限数值；`0` 表示完全不允许借债（此时判定与 `/simulate` 相同） |

**请求 `requests[]`**：与 `/simulate` 完全相同（`id` / `timestamp` / `cost` / `chain`），字段与跨字段校验规则一致。

### 借债试算语义

1. **时间顺序**：请求按 `timestamp` 升序处理（可乱序输入，服务内部排序），同一时刻按输入数组顺序处理；`results` 仍按原始数组顺序返回（含 `index`）。
2. **先还债的连续补充**：桶在最早请求时刻持有 `initial` 余额、零债务。每次被请求触及时补充 `(当前时间 - 上次更新时间) × refill_rate` 个令牌，**先偿还累计债务**，偿还后剩余的令牌才加入可用余额，余额仍封顶 `capacity`。还债会等量恢复借贷额度。时间推进（含还债）无论请求最终是否放行都会发生。
3. **借债扣减**：放行一笔 `cost` 时，桶先用可用余额支付（扣到 0 为止），仍有的缺口计入累计债务；借入后累计债务不得超过 `debt_limit`。因此判定时刻该桶**可支付总额** = `balance + (debt_limit - debt)`（可用余额 + 剩余借贷额度）。
4. **永久不可达**：`cost > capacity + debt_limit` 的请求即使在满桶零债务时也付不起，对该桶永远不可达（即使 `refill_rate = 0` 也不改变这一判定），拒绝原因固定为 `cost_exceeds_limit`。
5. **当前额度不足**：成本未超总上限，但判定时刻 `balance + (debt_limit - debt) < cost` 时以 `debt_limit_exceeded` 拒绝，`rejection` 给出 `balance_available` / `debt_before` / `remaining_credit` / `available` / `shortage`（= `cost - available`）等明细。零补充且债务未还清时，借贷额度不会自行恢复。
6. **链上原子判定**：按 `chain` 顺序找第一个无法在借贷上限内满足成本的桶（先判定永久不可达，再判定当前额度不足）；任一桶失败则整笔拒绝，**链上所有桶的余额与债务保持判定前状态**，`rejected_by` 指向该桶。
7. **快照**：每个链上桶的 `buckets[]` 同时给出 `balance` 与 `debt` 两组 `before` / `after`——`before` 为补充还债之后、扣减之前，`after` 为扣减之后（被拒绝时两者均与 `before` 相同）。
8. **终态快照**：`final_buckets` 按 `buckets` 输入顺序给出每个桶推进到最后一个时间戳后的 `balance`、`debt` 与可立即支付总额 `available = balance + (debt_limit - debt)`。

### 响应示例

```json
{
  "allowed_count": 2,
  "rejected_count": 3,
  "results": [
    {
      "request_id": "r1", "index": 0, "timestamp": 0.0, "cost": 4.0,
      "allowed": true, "rejected_by": null, "rejection": null,
      "buckets": {
        "api_qps": {
          "balance": {"before": 2.0, "after": 0.0},
          "debt": {"before": 0.0, "after": 2.0}
        },
        "db_conns": {
          "balance": {"before": 0.0, "after": 0.0},
          "debt": {"before": 0.0, "after": 4.0}
        }
      }
    },
    {
      "request_id": "r2", "index": 1, "timestamp": 0.0, "cost": 9.0,
      "allowed": false, "rejected_by": "api_qps",
      "rejection": {
        "bucket_id": "api_qps", "reason": "debt_limit_exceeded",
        "capacity": 10.0, "debt_limit": 5.0, "cost": 9.0,
        "balance_available": 0.0, "debt_before": 2.0,
        "available": 3.0, "remaining_credit": 3.0, "shortage": 6.0
      },
      "buckets": {
        "api_qps": {
          "balance": {"before": 0.0, "after": 0.0},
          "debt": {"before": 2.0, "after": 2.0}
        }
      }
    },
    {
      "request_id": "r3", "index": 2, "timestamp": 3.0, "cost": 2.0,
      "allowed": true, "rejected_by": null, "rejection": null,
      "buckets": {
        "api_qps": {
          "balance": {"before": 1.0, "after": 0.0},
          "debt": {"before": 0.0, "after": 1.0}
        }
      }
    },
    {
      "request_id": "r4", "index": 3, "timestamp": 3.0, "cost": 1.0,
      "allowed": false, "rejected_by": "db_conns",
      "rejection": {
        "bucket_id": "db_conns", "reason": "debt_limit_exceeded",
        "capacity": 4.0, "debt_limit": 4.0, "cost": 1.0,
        "balance_available": 0.0, "debt_before": 4.0,
        "available": 0.0, "remaining_credit": 0.0, "shortage": 1.0
      },
      "buckets": {
        "api_qps": {
          "balance": {"before": 0.0, "after": 0.0},
          "debt": {"before": 1.0, "after": 1.0}
        },
        "db_conns": {
          "balance": {"before": 0.0, "after": 0.0},
          "debt": {"before": 4.0, "after": 4.0}
        }
      }
    },
    {
      "request_id": "r5", "index": 4, "timestamp": 3.0, "cost": 20.0,
      "allowed": false, "rejected_by": "api_qps",
      "rejection": {
        "bucket_id": "api_qps", "reason": "cost_exceeds_limit",
        "capacity": 10.0, "debt_limit": 5.0, "cost": 20.0,
        "balance_available": 0.0, "debt_before": 1.0,
        "available": 4.0, "remaining_credit": 4.0, "shortage": 16.0
      },
      "buckets": {
        "api_qps": {
          "balance": {"before": 0.0, "after": 0.0},
          "debt": {"before": 1.0, "after": 1.0}
        }
      }
    }
  ],
  "final_buckets": [
    {"id": "api_qps",  "capacity": 10.0, "refill_rate": 1.0, "debt_limit": 5.0,
     "balance": 0.0, "debt": 1.0, "available": 4.0},
    {"id": "db_conns", "capacity": 4.0,  "refill_rate": 0.0, "debt_limit": 4.0,
     "balance": 0.0, "debt": 4.0, "available": 0.0}
  ]
}
```

- r1（t=0）：`api_qps` 余额 2 扣光后借入 2（债务=2）；零余额的 `db_conns` 全额借入 4（债务=4，恰好触顶）→ 链上两桶都在限额内，放行；
- r2（t=0）：`api_qps` 余额 0、剩余借贷额度 5−2=3，可支付 3 < 9（但 9 ≤ 10+5=15，并非永久不可达）→ `debt_limit_exceeded`，原子拒绝；
- r3（t=3）：`api_qps` 3 秒补充 3 个令牌，**先还债 2**（债务→0）、剩余 1 进入余额；cost 2 再扣光余额并借入 1（债务=1）→ 放行；
- r4（t=3）：`api_qps` 可支付（0 + 4 = 4）足够，但零补充的 `db_conns` 债务仍为 4、剩余额度 0，付不起 1 → 整笔原子拒绝，`api_qps` 也保持余额 0、债务 1；
- r5（t=3）：cost 20 > capacity + debt_limit = 15 → `cost_exceeds_limit`，任何时刻都不可达；
- 终态观察时刻 t=3：`api_qps` 余额 0、债务 1、可立即支付 4；零补充的 `db_conns` 债务永远停在 4，可立即支付 0。

## 请求格式 `POST /debt-diff`

对两组与 `/debt-simulate` **完全同格式**的情景（`baseline` 与 `candidate`）分别独立回放同一请求序列，再按**原始请求索引**对齐比较。典型用途：评估调整 `debt_limit` / `initial` / `refill_rate` 等配置后，哪些请求的放行结论会改变，以及余额、债务的逐点差异。

```json
{
  "baseline": {
    "buckets": [
      {"id": "api_qps",  "capacity": 10, "refill_rate": 1, "initial": 2, "debt_limit": 0},
      {"id": "db_conns", "capacity": 4,  "refill_rate": 0, "initial": 0, "debt_limit": 0}
    ],
    "requests": [
      {"id": "r1", "timestamp": 0, "cost": 4, "chain": ["api_qps", "db_conns"]},
      {"id": "r2", "timestamp": 3, "cost": 2, "chain": ["api_qps"]}
    ]
  },
  "candidate": {
    "buckets": [
      {"id": "api_qps",  "capacity": 10, "refill_rate": 1, "initial": 2, "debt_limit": 5},
      {"id": "db_conns", "capacity": 4,  "refill_rate": 0, "initial": 0, "debt_limit": 4}
    ],
    "requests": [
      {"id": "r1", "timestamp": 0, "cost": 4, "chain": ["api_qps", "db_conns"]},
      {"id": "r2", "timestamp": 3, "cost": 2, "chain": ["api_qps"]}
    ]
  }
}
```

### 字段说明

**顶层**

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `baseline` | 是 | 基线情景，格式与 `/debt-simulate` 请求体完全相同（`buckets` + `requests`） |
| `candidate` | 是 | 候选情景，格式同上；与基线对比的配置差异放在此处 |

**情景一致性要求**（两组 `buckets` / `requests` 各自仍完整执行 `/debt-simulate` 的全部字段与跨字段校验）：

| 必须一致 | 允许不同 |
| --- | --- |
| 请求数量 | 桶参数 `capacity` / `refill_rate` / `initial` / `debt_limit` |
| 逐索引请求 `id` 与 `timestamp` | 请求的 `cost` 与 `chain` |
| 桶 ID 集合（桶定义顺序可不同，按 ID 对齐） | |

### 差异语义

1. **独立回放、无状态**：两组情景各自从零状态（`initial` 余额、零债务）按与 `/debt-simulate` 完全一致的时间排序、补充先还债与链上原子借贷规则回放，互不影响；服务不保存任何状态，差异对比不会写入或污染任何数据。
2. **按原始索引对齐**：即使请求乱序输入，两侧结果都按原始 `requests` 数组下标逐一对齐（`request_id` 与 `timestamp` 必须一致，否则 422）。
3. **结论分歧**：同一索引上两侧 `allowed` 不同，**或**两侧都拒绝但 `rejected_by` / `reason`（`cost_exceeds_limit` / `debt_limit_exceeded`）不同，即记为一次分歧；两侧都放行、或同桶同原因拒绝（哪怕余额快照不同）不算分歧。
4. **首个分歧** `first_divergence`：取原始索引顺序上的第一次分歧，给出请求 ID/下标/时间戳与双方各自的 `allowed` / `rejected_by` / `reason`；全程无分歧时为 `null`。
5. **逐请求桶差值** `results[].buckets`：覆盖该请求两侧链上引用桶的**并集**（基线链序优先）。每个桶给出双方该请求**之后**的余额/债务与差值（`delta = candidate − baseline`）；某桶只被一侧链引用时，两侧数值仍给出（未引用侧仅做时间推进/还债，`before == after`），但对应 `balance_delta` / `debt_delta` 为 `null`。
6. **终态快照差值** `final_buckets`：按**基线桶输入顺序**逐桶给出两侧在最后请求时刻的完整终态快照（与 `/debt-simulate` 的 `final_buckets` 同构，含各自桶参数），以及 `balance_delta` / `debt_delta` / `available_delta`（均为 `candidate − baseline`）。

### 响应示例（对应上面的请求体）

```json
{
  "request_count": 2,
  "divergence_count": 1,
  "first_divergence": {
    "request_id": "r1",
    "index": 0,
    "timestamp": 0.0,
    "baseline": {"allowed": false, "rejected_by": "db_conns", "reason": "debt_limit_exceeded"},
    "candidate": {"allowed": true, "rejected_by": null, "reason": null}
  },
  "results": [
    {
      "request_id": "r1", "index": 0, "timestamp": 0.0, "diverged": true,
      "baseline": {"allowed": false, "rejected_by": "db_conns", "reason": "debt_limit_exceeded"},
      "candidate": {"allowed": true, "rejected_by": null, "reason": null},
      "buckets": {
        "api_qps": {
          "baseline_balance": 2.0, "candidate_balance": 0.0, "balance_delta": -2.0,
          "baseline_debt": 0.0, "candidate_debt": 2.0, "debt_delta": 2.0
        },
        "db_conns": {
          "baseline_balance": 0.0, "candidate_balance": 0.0, "balance_delta": 0.0,
          "baseline_debt": 0.0, "candidate_debt": 4.0, "debt_delta": 4.0
        }
      }
    },
    {
      "request_id": "r2", "index": 1, "timestamp": 3.0, "diverged": false,
      "baseline": {"allowed": true, "rejected_by": null, "reason": null},
      "candidate": {"allowed": true, "rejected_by": null, "reason": null},
      "buckets": {
        "api_qps": {
          "baseline_balance": 3.0, "candidate_balance": 0.0, "balance_delta": -3.0,
          "baseline_debt": 0.0, "candidate_debt": 1.0, "debt_delta": 1.0
        }
      }
    }
  ],
  "final_buckets": [
    {
      "id": "api_qps",
      "baseline": {"id": "api_qps", "capacity": 10.0, "refill_rate": 1.0, "debt_limit": 0.0,
                   "balance": 3.0, "debt": 0.0, "available": 3.0},
      "candidate": {"id": "api_qps", "capacity": 10.0, "refill_rate": 1.0, "debt_limit": 5.0,
                    "balance": 0.0, "debt": 1.0, "available": 4.0},
      "balance_delta": -3.0, "debt_delta": 1.0, "available_delta": 1.0
    },
    {
      "id": "db_conns",
      "baseline": {"id": "db_conns", "capacity": 4.0, "refill_rate": 0.0, "debt_limit": 0.0,
                   "balance": 0.0, "debt": 0.0, "available": 0.0},
      "candidate": {"id": "db_conns", "capacity": 4.0, "refill_rate": 0.0, "debt_limit": 4.0,
                    "balance": 0.0, "debt": 4.0, "available": 0.0},
      "balance_delta": 0.0, "debt_delta": 4.0, "available_delta": 0.0
    }
  ]
}
```

- r1（t=0）：基线不允许借债，`db_conns` 余额 0、借贷额度 0 付不起 4，整笔原子拒绝（链上各桶保持）；候选允许借贷，`api_qps` 余额 2 扣光借入 2（债务=2）、`db_conns` 全额借入 4（债务=4）→ 放行，这是首个（也是唯一一个）结论分歧；
- r2（t=3）：基线 r1 被拒、`api_qps` 余额仍为 2，补充 3 后为 5，cost 2 → 余额 3；候选补充 3 **先还债 2**（债务→0）、余 1 进余额，cost 2 再扣光并借入 1（债务=1）→ 双方都放行，不算分歧但余额/债务差值照常给出；
- 终态：`api_qps` 候选较基线余额少 3、债务多 1，但因借贷额度恢复，可立即支付总额反而多 1；零补充的 `db_conns` 候选债务停在 4，两侧可立即支付都是 0。

## 校验规则（HTTP 422）

以下非法输入都会被拒绝，错误体统一为 `{"detail": [ {"type", "loc", "msg", ...}, ... ]}`：

- **重复桶**：`buckets[].id` 重复（`duplicate_bucket`）；
- **非法速率/容量**：`capacity <= 0`、`refill_rate < 0`、非有限数值、`initial`/`balance` 越界；
- **未知引用**：`chain` 引用了未在 `buckets` 中定义的 ID（`unknown_bucket_ref`）；
- **非法窗口宽度**：`window_seconds` 缺失、非有限数值或 `<= 0`（`/usage-report` 与 `/fairness-report`）；
- **非法主体字段**（仅 `/fairness-report`）：`principal` 缺失或不是非空字符串；`weight` 非有限数值或 `<= 0`；同一 `principal` 在不同请求中权重不一致（`inconsistent_principal_weight`）；
- **非法预留区间**：`start` / `end` 非有限数值，或 `end <= start`（仅 `/reservation-simulate`）；
- **非法候选链**（仅 `/route-simulate`）：`chains` 为空、候选链为空、候选链内桶重复；候选链引用未定义的桶同样报 `unknown_bucket_ref`（`loc` 定位到具体候选链）；
- **非法 deadline**（仅 `/queue-simulate`）：`deadline` 缺失、非有限数值或早于 `timestamp`；
- **非法滑动窗口策略/请求**（仅 `/window-simulate`）：`policies[].id` 重复（`duplicate_policy`）；`window_seconds` / `limit` 缺失、非有限数值或 `<= 0`；`policy_refs` 引用未定义策略（`unknown_policy_ref`）、为空或在同一请求内重复；`principal` 缺失或不是非空字符串；请求 ID 重复（`duplicate_request`）；
- **非法周期结转策略/请求**（仅 `/rollover-simulate`）：`policies[].id` 重复（`duplicate_policy`）；`anchor` 非有限数值（允许负数）；`period_seconds` / `allowance` 缺失、非有限数值或 `<= 0`；`carry_cap` 缺失、非有限数值或 `< 0`；`policy_refs` 引用未定义策略（`unknown_policy_ref`）、为空或在同一请求内重复；请求 ID 重复（`duplicate_request`）；
- **非法债务上限**（仅 `/debt-simulate`）：`debt_limit` 缺失、非有限数值或 `< 0`（`0` 合法，表示不允许借债）；桶的其余字段与请求字段沿用 `/simulate` 的校验规则，链未知引用同样报 `unknown_bucket_ref`、请求 ID 重复报 `duplicate_request`。注意：`cost > capacity + debt_limit` 等业务不可达情形**不是** 422，而是 200 响应中的 `cost_exceeds_limit` 拒绝；
- **情景不一致**（仅 `/debt-diff`）：`baseline` 与 `candidate` 两组情景各自先完整执行上述 `/debt-simulate` 校验，随后再做跨情景对齐校验——请求数量不一致（`scenario_request_count_mismatch`）、桶 ID 集合不一致（`scenario_bucket_set_mismatch`，同时给出 `missing_in_candidate` / `extra_in_candidate`）、同一原始索引上请求 ID 不一致（`scenario_request_id_mismatch`）或时间戳不一致（`scenario_timestamp_mismatch`）。错误 `loc` 一律从情景名（`baseline` / `candidate`）开始并定位到具体索引与字段，多个不一致会一并返回；任一类不一致都不会触发回放，服务无状态、不写入任何数据；
- 此外还校验：链为空、链内桶重复、`cost <= 0`、请求 ID 重复（`duplicate_request`，仅 `/simulate` 系接口、`/route-simulate` 与 `/queue-simulate`）、多余字段（`extra="forbid"`）。

以上规则对 `/simulate`、`/forecast`、`/plan-initial-balances`、`/usage-report`、`/fairness-report`、`/reservation-simulate`、`/route-simulate`、`/queue-simulate`、`/window-simulate`、`/rollover-simulate`、`/debt-simulate` 与 `/debt-diff` 同时生效（按各自字段；`/plan-initial-balances`、`/usage-report` 与 `/fairness-report` 复用 `/simulate` 的桶与请求跨字段校验；`/reservation-simulate` 使用独立的容量桶模型；`/route-simulate` 复用 `/simulate` 的桶模型并对每条候选链做引用校验；`/queue-simulate` 复用 `/simulate` 的桶、请求字段与跨字段校验，并在请求上增加 `deadline` 校验；`/window-simulate` 使用独立的滑动窗口策略模型，按主体记账并对 `policy_refs` 做引用校验；`/rollover-simulate` 使用独立的周期结转策略模型，对 `policy_refs` 做引用校验，不按主体隔离；`/debt-simulate` 复用 `/simulate` 的请求模型与跨字段校验，桶模型在 `/simulate` 桶字段上扩展非负 `debt_limit`；`/debt-diff` 在 `baseline` / `candidate` 两个字段内嵌套复用 `/debt-simulate` 的全部模型与校验，并额外做两情景的请求数量、请求 ID、时间戳与桶集合对齐校验）。

## 本地调用示例

```bash
curl -X POST http://localhost:8000/simulate \
  -H 'Content-Type: application/json' \
  -d @examples/request.json

curl -X POST http://localhost:8000/forecast \
  -H 'Content-Type: application/json' \
  -d @examples/forecast.json

curl -X POST http://localhost:8000/plan-initial-balances \
  -H 'Content-Type: application/json' \
  -d @examples/plan-initial-balances.json

curl -X POST http://localhost:8000/usage-report \
  -H 'Content-Type: application/json' \
  -d @examples/usage-report.json

curl -X POST http://localhost:8000/fairness-report \
  -H 'Content-Type: application/json' \
  -d @examples/fairness-report.json

curl -X POST http://localhost:8000/reservation-simulate \
  -H 'Content-Type: application/json' \
  -d @examples/reservation-simulate.json

curl -X POST http://localhost:8000/route-simulate \
  -H 'Content-Type: application/json' \
  -d @examples/route-simulate.json

curl -X POST http://localhost:8000/queue-simulate \
  -H 'Content-Type: application/json' \
  -d @examples/queue-simulate.json

curl -X POST http://localhost:8000/window-simulate \
  -H 'Content-Type: application/json' \
  -d @examples/window-simulate.json

curl -X POST http://localhost:8000/rollover-simulate \
  -H 'Content-Type: application/json' \
  -d @examples/rollover-simulate.json

curl -X POST http://localhost:8000/debt-simulate \
  -H 'Content-Type: application/json' \
  -d @examples/debt-simulate.json

curl -X POST http://localhost:8000/debt-diff \
  -H 'Content-Type: application/json' \
  -d @examples/debt-diff.json
```

## 不使用 Docker 的本地开发（可选）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

## 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由与异常处理
│   ├── models/          # Pydantic 模型与输入校验（按领域拆分，app.models 为稳定门面）
│   │   ├── __init__.py  # 门面：原样再导出全部公开模型名，历史导入路径保持可用
│   │   ├── common.py    # 公共基础：EPS、有限数校验、BucketBalance
│   │   ├── simulation/  # 基础模拟域（/simulate、/plan-initial-balances）
│   │   ├── analysis/    # 分析域（/forecast、/usage-report、/fairness-report）
│   │   ├── scheduling/  # 预留/路由/排队域（reservation/route/queue）
│   │   ├── windows/     # 窗口/结转域（/window-simulate、/rollover-simulate）
│   │   └── debt/        # 债务对比域（/debt-simulate、/debt-diff）
│   ├── simulation.py    # 连续补充 + 原子扣减模拟引擎
│   ├── forecast.py      # 等待时间预测引擎（不扣减状态）
│   ├── planner.py       # 初始余额规划引擎（反推最低 initial）
│   ├── usage.py         # 配额使用分析引擎（按窗口统计消耗/拒绝/峰值占用）
│   ├── fairness.py      # 主体公平性分析引擎（加权 Jain 指数/最受限主体/首个短缺桶）
│   ├── reservation.py   # 区间预留试算引擎（容量受限的链上原子预留）
│   ├── routing.py       # 候选链路由试算引擎（多候选链选路 + 原子扣减）
│   ├── queueing.py      # 严格 FIFO 排队试算引擎（队首等待至可执行时刻或 deadline）
│   ├── windowing.py     # 半开滑动窗口试算引擎（多策略/多主体链上原子记账）
│   ├── rollover.py      # 周期结转额度试算引擎（跨期失效/结转、先扣结转额的链上原子扣减）
│   ├── debt.py          # 可借贷桶试算引擎（补充先还债、限额内借债的链上原子扣减）
│   ├── debt_diff.py     # 两情景差异引擎（baseline/candidate 各自回放后按原始索引对齐）
│   └── errors.py        # 跨字段校验错误类型
├── tests/               # pytest 测试（引擎语义 + HTTP + 模型结构/OpenAPI 契约）
├── examples/request.json
├── examples/forecast.json
├── examples/plan-initial-balances.json
├── examples/usage-report.json
├── examples/fairness-report.json
├── examples/reservation-simulate.json
├── examples/route-simulate.json
├── examples/queue-simulate.json
├── examples/window-simulate.json
├── examples/rollover-simulate.json
├── examples/debt-simulate.json
├── examples/debt-diff.json
├── requirements.txt
├── Dockerfile           # 依赖安装在镜像构建阶段完成
├── docker-compose.yml   # 单命令启动，映射 8000 端口
├── entrypoint.sh        # 容器启动初始化
└── README.md
```
