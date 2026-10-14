# WorkBuddy 用量数据源与口径

本文件是 `wb-usage-audit` 技能的参考文档：记录数据在哪、字段叫什么、口径怎么算。
排查「某个数字对不上」时先查这里。

---

## 一、三个数据源，各管一段

| 想知道什么 | 数据源 | 粒度 | 外部 API 是否覆盖 |
|---|---|---|---|
| 每次模型调用的真实 token | `~/.workbuddy/projects/<工作区>/<会话>.jsonl` → `providerData.rawUsage` | 请求级 | ✅ 供应商返回什么就记什么 |
| 每轮对话的上下文占用 | `~/.workbuddy/workbuddy.db` → 表 `session_usage` | 每轮 | ✅（与用哪个模型无关） |
| 单次调用的耗时 / 首字延迟 | `~/.workbuddy/logs/<日期>/<工作区>__<hash>.log` 的 `qua.end` 行 | 请求级 | ✅ |
| 真实账单 / 余额 | 你自己的 API 供应商控制台 | 账户级 | ✅ 唯一权威 |

WorkBuddy 界面本身**没有** token 汇总仪表盘。走外部 API 时，`session_usage.credit_json`（积分）
与你的账单无关，不要拿它当成本口径。

---

## 二、jsonl 字段映射（主数据源）

目录结构（工作空间目录名 = 工作目录路径的 slug）：

```
~/.workbuddy/projects/
└── d-home-me-projects-myapp/                             ← 工作空间（对应 D:\home\me\projects\myapp）
    ├── fe0ad6da-b171-44b2-990d-8ce607357eaf.jsonl         ← 会话
    └── 485a6a5f-aa4a-483e-9673-5487c56c6121/
        └── subagents/agent-*.jsonl                        ← 子代理，归属上面的父会话
```

一条带 usage 的记录（只列关键字段）：

```json
{
  "id": "01a138f8-...",
  "timestamp": 1791957222513,
  "type": "function_call",
  "providerData": {
    "model": "deepseek/deepseek-v4.1-flash",
    "requestModelId": "custom-local:deepseek/deepseek-v4.1-flash",
    "conversationRequestId": "01a138f8...",
    "messageId": "01a138f9...",
    "agentName": "cli",
    "rawUsage": {
      "prompt_tokens": 36913,
      "completion_tokens": 1140,
      "total_tokens": 38053,
      "prompt_tokens_details": { "cached_tokens": 34944, "audio_tokens": 0 }
    }
  }
}
```

不同协议的用量字段命名不一样，脚本两套都认：

| 协议 | 字段位置 | 输入 | 输出 | 合计 | 缓存 |
|---|---|---|---|---|---|
| OpenAI / DeepSeek / GLM | `providerData.rawUsage` | `prompt_tokens` | `completion_tokens` | `total_tokens` | `prompt_cache_hit_tokens` 或 `prompt_tokens_details.cached_tokens` |
| Anthropic 等驼峰风格 | `providerData.usage` | `inputTokens` / `promptTokens` | `outputTokens` / `completionTokens` | `totalTokens` | `cacheReadInputTokens` |

| 报表字段 | 取的字段 | 说明 |
|---|---|---|
| 时间 | `timestamp` | 毫秒时间戳；缺失则跳过该条 |
| 工作空间 | 相对路径第 1 段 | `-WorkBuddy-` 之后的部分作为短名 |
| 会话 | 相对路径第 2 段文件名 | 子代理文件保持归属父会话 |
| 模型 | `providerData.model` → `requestModelId` → `requestModelName` → `modelId` | 依次兜底 |
| 输入 | `prompt_tokens` / `inputTokens` | **含缓存命中** |
| 输出 | `completion_tokens` / `outputTokens` | |
| 合计 | `total_tokens` / `totalTokens`；两者都缺时取「输入 + 输出」 | 完全没有用量信息的记录直接跳过 |
| 缓存读 | `prompt_cache_hit_tokens` → `prompt_tokens_details.cached_tokens` → `cacheReadInputTokens` | 前者是 DeepSeek 风格顶层字段，中者是 OpenAI/Claude/GLM 风格嵌套字段 |
| 缓存写 | `prompt_tokens_details.cached_creation_tokens` / `cache_write_tokens` / `cacheCreationInputTokens` | Claude 系按 1.25× 输入价计 |
| 代理 | `providerData.agentName` / `agent_name` | 主会话常为 `cli`，子代理为角色名 |

**有 usage 才算一次模型调用**，其余都是本地事件（用户消息、工具调用结果、文件快照等）。

---

## 三、金额口径

```
全价输入 = 输入 - 缓存读 - 缓存写
金额(元) = 全价输入 × 输入价 ÷ 1e6
         + 输出 × 输出价 ÷ 1e6
         + 缓存读 × 缓存价 ÷ 1e6
         + 缓存写 × 输入价 × 1.25 ÷ 1e6
```

价格表在 `assets/prices.json`，单位「元 / 百万 token」，按**最长前缀**匹配模型名。

> ⚠️ 这些是内置牌价参考值。用第三方中转时，真实单价往往不同，金额只能用于**横向比较哪个项目/模型更费**，不能当账单。

---

## 四、上下文占用（`session_usage` 表）

```sql
select session_id, used, size, updated_at from session_usage order by updated_at desc;
```

- `used` / `size`：已用上下文与窗口大小，对应界面输入框上方的占用指示器。
- `credit_json`：每轮的内置积分消耗。**走外部 API 时不会产生积分**，对本技能无意义。
- 想映射到工作目录，可用 `~/.workbuddy/sessions/<pid>.json` 里的 `sessionId → cwd`。

---

## 五、日志里的耗时/首字延迟（补充源）

```
~/.workbuddy/logs/<日期>/<工作区>__<hash>.log
```

搜 `[ModelProvider] ... qua.end:` 那一行，JSON 里有：

`model` / `agent_name` / `scene` / `request_type` / `prompt_tokens` / `completion_tokens` /
`total_tokens` / `ttft_ms`（首字延迟）/ `total_cost_ms` / `finish_reason` / `text_total_tokens` /
`think_total_tokens`（思维链 token）。

同目录里 `Request failed with status code N` 与 `Error response: status=N` 是失败记录，用于排查。

---

## 六、常见坑

1. **`prompt_tokens` 含缓存命中**。把「输入 24 亿」直接乘输入价会把成本算大好几倍。
2. **供应商不回 `usage` 就没有数据**。某些中转在流式模式下不返回 usage，此时报表会缺该模型，只能去控制台看。
3. **重复计数**：同一个 `messageId` 理论上只出现一次；如果怀疑有重复，用 `messageId` 去重核对。
4. **jsonl 含完整对话与 reasoning 内容**，是最敏感的本机文件之一：不要整目录外发，也不要用在线工具分析。
5. **日志体积大**（单个工作区单日日志可达 10~30 MB），不要肉眼翻，用脚本聚合。
