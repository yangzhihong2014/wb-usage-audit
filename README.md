# wb-usage-audit

审计 WorkBuddy 的模型 token 用量与成本。扫描本机会话日志里**请求级真实 usage**，输出用量报告，
支持控制台表格、Markdown、JSON、CSV 四种形式。全程离线，不上传任何数据。

适用于**任何模型来源**，包括在 `models.json` 里配置的外部 API / 中转商。

## 能力

- **总览**：请求数、会话数、工作空间数、模型数、输入/输出/合计 token、单次请求均值
- **缓存视角**：缓存命中率、全价输入占比（这是外部 API 成本的关键变量）
- **多维分组**：按模型 / 按天 / 按工作空间 / 按会话，可任意组合
- **预估金额**：按可配置价格表（`assets/prices.json`）计算，含缓存读价与缓存写 1.25× 输入价
- **上下文占用**：可选读取 `workbuddy.db` 的 `session_usage`，看每个会话当前占了多少窗口
- **明细导出**：`--csv` 逐条请求、`--json` 机器可读汇总
- **子代理归属**：`subagents/agent-*.jsonl` 自动归到父会话，不漏计也不重复计

## 安装

WorkBuddy 用户级技能目录：

```
~/.workbuddy/skills/wb-usage-audit/
├── SKILL.md
├── scripts/usage_audit.py
├── assets/prices.json
└── references/data-sources.md
```

- Windows：`C:\Users\<你>\.workbuddy\skills\wb-usage-audit\`
- macOS / Linux：`~/.workbuddy/skills/wb-usage-audit/`

装好后对 WorkBuddy 说「看看我这个月用了多少 token」即可触发。

## 手动运行

Python 3.8+，**仅标准库，无需安装任何依赖**：

```bash
python scripts/usage_audit.py                                   # 全量总览
python scripts/usage_audit.py --days 7                          # 最近 7 天
python scripts/usage_audit.py --since 2026-10-01 --until 2026-10-14
python scripts/usage_audit.py --model deepseek                   # 只看某模型（子串匹配）
python scripts/usage_audit.py --ws my-project                    # 只看某工作空间（子串匹配）
python scripts/usage_audit.py --group day                        # 只按天看
python scripts/usage_audit.py --requests 20                      # 附最近 20 条请求明细
python scripts/usage_audit.py --context                          # 附会话上下文占用
python scripts/usage_audit.py --out 报告.md --csv 明细.csv --json  # 一次性导出
```

### 参数

| 参数 | 说明 |
|---|---|
| `--projects <目录>` | 会话 jsonl 目录，默认 `~/.workbuddy/projects` |
| `--db <文件>` | `workbuddy.db` 路径，用于 `--context` |
| `--sessions-dir <目录>` | 会话状态目录，用于把会话映射到工作目录 |
| `--days N` / `--since` / `--until` | 时间范围（`--since/--until` 优先级更高） |
| `--ws` / `--model` | 工作空间 / 模型名子串过滤 |
| `--group model\|day\|ws\|session` | 输出哪些分组，可重复；默认 model+day+ws |
| `--top N` | 每组最多显示多少行，默认 25 |
| `--requests N` | 附最近 N 条请求明细 |
| `--context` | 附会话上下文占用（读 workbuddy.db） |
| `--prices <json>` | 自定义价格表 |
| `--price-in/--price-out/--price-cache` | 临时覆盖兜底单价（元/百万 token） |
| `--no-cost` | 不计算金额 |
| `--out` / `--csv` / `--json` | 写出 Markdown 报告 / 逐条 CSV / JSON |

## 数据源与口径

数据来自 `~/.workbuddy/projects/<工作空间>/<会话>.jsonl` 中每条记录的 usage 字段，
即**供应商响应里返回的真实用量**，不是本地估算。两套字段命名都支持：

- `providerData.rawUsage`：`prompt_tokens` / `completion_tokens` / `total_tokens`（OpenAI / DeepSeek / GLM 风格）
- `providerData.usage`：`inputTokens` / `outputTokens` / `totalTokens`（Anthropic 等驼峰风格）

金额口径：

```
全价输入 = 输入 − 缓存读 − 缓存写
金额 = 全价输入×输入价 + 输出×输出价 + 缓存读×缓存价 + 缓存写×输入价×1.25   （单位：元/百万 token）
```

价格表在 `assets/prices.json`，**内置的是牌价参考值**。如果你走第三方中转或自建渠道，
请把条目改成真实单价，否则金额只能用于横向比较「哪个模型/项目更费」，不能当账单。

更细的字段映射、SQL 与常见坑见 `references/data-sources.md`。

## 与 token-dashboard 的分工

| 需求 | 用哪个 |
|---|---|
| 文字报告、CSV/JSON、问答式审计 | 本技能 |
| 可视化看板（日历热力图、0–24 时模型分布、三级下钻） | [token-dashboard](https://github.com/fuyi-git/token-dashboard) |

## 自测

技能自带一个可移植性 / 兼容性测试，用合成数据跑，不依赖本机日志：

```bash
python tests/test_generic.py
```

它会在临时目录里造一份「异机」账本（别的系统的工作空间名、Anthropic 驼峰风格 usage、
损坏 JSON 行、缺时间戳的记录、无 usage 的本地事件、缺失的价格表），
然后断言脚本仍能正确统计。预期以「✅ 全部通过」结尾。

## 注意

- 会话 jsonl 含完整对话内容，是最敏感的本机文件之一：不要整目录外发，也不要用在线工具分析。
- 只统计带 usage 的请求；供应商不回 usage 时该模型不会出现在报表里，需去供应商控制台核对。
- 报告中出现的工作空间名、会话标题来自本机日志，分享前先确认。
