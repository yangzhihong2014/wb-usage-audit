# -*- coding: utf-8 -*-
"""
usage_audit.py —— WorkBuddy 模型用量审计（请求级真实 usage）

数据源一（用量与成本）：~/.workbuddy/projects/<工作空间>/<会话>.jsonl 里每条记录的
        providerData.rawUsage（供应商响应里回的真实 usage，不是估算）
        providerData.model          → 模型名
        记录顶层 timestamp（毫秒）  → 时间
        子代理 subagents/agent-*.jsonl 自动归属父会话

数据源二（生成速度）：~/.workbuddy/logs/<日期>/<工作区>__<hash>.log 里 qua.end 行的
        completion_tokens / ttft_ms / total_cost_ms / sse_connect_cost_ms
        用量账本里没有耗时字段，速度只能从日志取；日志按日期分目录且有留存期限，
        归档（目录名带 .expired）或删除后该日期的速度不再可得。

用途：回答「我这个模型用了多少 token / 花了多少 / 哪个项目最贵 / 缓存命中率多少 /
      回复速度多快、慢在排队还是慢在生成」，
      输出纯文本（默认）、Markdown（--out）、JSON（--json）或 CSV（--csv）。

仅依赖 Python 标准库（3.8+）。

常用示例：
    python usage_audit.py                                   # 全量总览（含生成速度）
    python usage_audit.py --days 7                          # 最近 7 天
    python usage_audit.py --since 2026-10-01 --until 2026-10-14
    python usage_audit.py --model deepseek                   # 只看某模型（子串匹配）
    python usage_audit.py --ws novel-writer-agent             # 只看某工作空间（子串匹配）
    python usage_audit.py --group day                         # 只按天看
    python usage_audit.py --requests 15                       # 附最近 15 条请求明细
    python usage_audit.py --context                           # 附会话上下文占用
    python usage_audit.py --no-speed                          # 只报用量，跳过生成速度
    python usage_audit.py --csv 用量.csv --out 用量报告.md     # 同时导出
"""

import argparse
import collections
import csv
import datetime
import glob
import json
import os
import re
import sys

try:                                     # Windows 控制台 GBK 保护
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DEFAULT_PROJECTS = os.path.join(os.path.expanduser("~"), ".workbuddy", "projects")
DEFAULT_LOGS = os.path.join(os.path.expanduser("~"), ".workbuddy", "logs")
DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".workbuddy", "workbuddy.db")
DEFAULT_SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "sessions")
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PRICES = os.path.join(os.path.dirname(HERE), "assets", "prices.json")
INTERNAL_TAG = re.compile(r"^\s*<")

# 日志目录名：2026-10-16 或 2026-10-16.expired-<ts>-<hash>（归档后仍留在本机）
LOG_DATE_DIR = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\.expired-.*)?$")
# qua.end 行首形如：[2026/10/16 11:27:10.530] [Info] [pid=23996] [ModelProvider] ...
LOG_LINE_TIME = re.compile(r"^\[(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2}):(\d{2})")
LOG_JSON = re.compile(r"\{.*\}")
# 未指定时间范围时，生成速度默认只看最近这么多天的日志（避免逐行扫全部日志）
SPEED_DEFAULT_DAYS = 7


# ------------------------------------------------------------------ 小工具

def num(n):
    return "{:,}".format(int(n))


def pct(a, b):
    return ("%.1f%%" % (100.0 * a / b)) if b else "-"


def fmt_ts(seconds):
    if not seconds:
        return "-"
    return datetime.datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")


def to_ms(date_str, end_of_day=False):
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    if end_of_day:
        d = d + datetime.timedelta(days=1) - datetime.timedelta(milliseconds=1)
    return int(d.timestamp() * 1000)


def day_key(ts):
    return datetime.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")


def short_ws(name):
    """工作空间目录名是路径 slug，取 -WorkBuddy- 之后的部分作为短名"""
    i = name.rfind("-WorkBuddy-")
    if i >= 0 and name[i + 11:]:
        return name[i + 11:]
    return name


class PriceTable:
    """价格表：[输入, 输出, 缓存命中]，单位「元 / 百万 token」；按最长前缀匹配模型名"""

    def __init__(self, path, override=None):
        self.exact = {}
        self.source = "内置默认"
        self.default = [2.0, 8.0, 0.4]
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    raw = json.load(fh)
                for k, v in (raw.get("models") or {}).items():
                    if isinstance(v, list) and len(v) >= 3:
                        self.exact[k.lower()] = [float(x) for x in v[:3]]
                d = raw.get("default")
                if isinstance(d, list) and len(d) >= 3:
                    self.default = [float(x) for x in d[:3]]
                self.source = os.path.basename(path)
            except Exception as exc:
                print("[warn] 价格表读取失败，改用内置默认：%s" % exc, file=sys.stderr)
        if override:
            self.default = list(override)

    def of(self, model):
        name = (model or "").lower()
        best = None
        for prefix in self.exact:
            if name.startswith(prefix) and (best is None or len(prefix) > len(best)):
                best = prefix
        return self.exact[best] if best else self.default

    def cost(self, model, inp, out, cache_read, cache_write):
        """按实际缓存命中计价：读命中走缓存价、缓存写入按 1.25× 输入价、其余走输入价"""
        p_in, p_out, p_cache = self.of(model)
        fresh = max(0, inp - cache_read - cache_write)
        return (fresh * p_in + out * p_out + cache_read * p_cache + cache_write * p_in * 1.25) / 1e6


def zero():
    return collections.Counter()


def bump(bucket, r):
    bucket["calls"] += 1
    bucket["in"] += r["in"]
    bucket["out"] += r["out"]
    bucket["total"] += r["total"]
    bucket["cache_read"] += r["cache_read"]
    bucket["cache_write"] += r["cache_write"]


# ------------------------------------------------------------------ 数据装载

def extract_title(obj):
    """取首条非内部标签的用户消息前 42 字作为会话标题"""
    if obj.get("role") != "user":
        return None
    c = obj.get("content")
    if isinstance(c, str):
        t = " ".join(c.split())
        return t[:42] if t and not INTERNAL_TAG.match(t) else None
    if isinstance(c, list):
        for seg in c:
            if isinstance(seg, dict) and seg.get("type") in ("input_text", "text"):
                t = (seg.get("text") or "").strip()
                if t and not INTERNAL_TAG.match(t):
                    return " ".join(t.split())[:42]
    return None


def grab_usage(pd):
    """从 providerData 里抽取一次调用的 usage。

    兼容两套字段命名（不同供应商/协议不一样）：
      - OpenAI / DeepSeek / GLM 风格：rawUsage.prompt_tokens / completion_tokens / total_tokens
      - Anthropic 等驼峰风格        ：usage.inputTokens / outputTokens / totalTokens
    返回 None 表示这条记录不是一次模型调用（本地事件），直接跳过。
    """
    ru = pd.get("rawUsage") or {}
    u = pd.get("usage") or {}

    inp = ru.get("prompt_tokens") or u.get("inputTokens") or u.get("promptTokens") or 0
    outp = ru.get("completion_tokens") or u.get("outputTokens") or u.get("completionTokens") or 0
    total = ru.get("total_tokens") or u.get("totalTokens")
    if not total:
        total = int(inp or 0) + int(outp or 0)      # 缺 total 时自行求和
    if not total:
        return None                                  # 完全没有用量信息 → 非调用记录

    ptd = ru.get("prompt_tokens_details") or {}
    cache_read = ru.get("prompt_cache_hit_tokens")
    if cache_read is None:
        cache_read = ptd.get("cached_tokens")
    if cache_read is None:
        cache_read = u.get("cacheReadInputTokens") or u.get("cache_read_input_tokens") or 0
    cache_write = ptd.get("cached_creation_tokens") or ptd.get("cache_write_tokens")
    if cache_write is None:
        cache_write = u.get("cacheCreationInputTokens") or u.get("cache_creation_input_tokens") or 0

    model = (pd.get("model") or pd.get("requestModelId") or pd.get("requestModelName")
             or pd.get("modelId") or "unknown")

    return {
        "model": model,
        "agent": pd.get("agentName") or pd.get("agent_name") or "",
        "in": int(inp or 0),
        "out": int(outp or 0),
        "total": int(total or 0),
        "cache_read": int(cache_read or 0),
        "cache_write": int(cache_write or 0),
    }


def load_records(projects_dir, since_ms, until_ms, ws_filter, model_filter):
    files = glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True)
    records = []
    sessions = set()

    for fp in files:
        rel = os.path.relpath(fp, projects_dir)
        parts = rel.split(os.sep)
        ws_raw = parts[0]
        ws = short_ws(ws_raw)
        sid = os.path.splitext(parts[1])[0] if len(parts) > 1 else "(unknown)"
        if ws_filter:
            f = ws_filter.lower()
            if f not in ws_raw.lower() and f not in ws.lower():
                continue

        title = None
        with open(fp, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                if title is None:
                    title = extract_title(obj)

                pd = obj.get("providerData")
                if not isinstance(pd, dict):
                    continue
                usage = grab_usage(pd)
                if not usage:
                    continue                      # 无 usage 的记录（纯本地事件）跳过

                if model_filter and model_filter.lower() not in usage["model"].lower():
                    continue

                ts = obj.get("timestamp")
                if not isinstance(ts, (int, float)):
                    continue                      # 缺时间戳的记录无法归入任何一天，跳过
                ts = int(ts)
                if since_ms and ts < since_ms:
                    continue
                if until_ms and ts > until_ms:
                    continue

                records.append({
                    "ts": ts,
                    "ws": ws,
                    "session": sid,
                    "title": title or "",
                    "model": usage["model"],
                    "agent": usage["agent"],
                    "in": usage["in"],
                    "out": usage["out"],
                    "total": usage["total"],
                    "cache_read": usage["cache_read"],
                    "cache_write": usage["cache_write"],
                })
                sessions.add((ws_raw, sid))     # 只统计「筛选后确实有调用」的会话

    return records, sessions


def read_context_usage(db_path, sessions_dir):
    """读 workbuddy.db 的 session_usage：各会话「上下文占用 / 窗口大小」"""
    import sqlite3
    if not os.path.exists(db_path):
        return [], "数据库不存在"
    pid_map = {}
    if os.path.isdir(sessions_dir):
        for f in glob.glob(os.path.join(sessions_dir, "*.json")):
            try:
                d = json.load(open(f, encoding="utf-8"))
                pid_map[d.get("sessionId")] = d.get("cwd") or ""
            except Exception:
                pass
    rows = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
        for sid, used, size, updated in con.execute(
                "select session_id, used, size, updated_at from session_usage order by updated_at desc"):
            rows.append({"session_id": sid, "used": used, "size": size,
                         "updated_at": updated, "cwd": pid_map.get(sid, "")})
        con.close()
    except Exception as exc:
        return [], str(exc)
    return rows, None


# ------------------------------------------------------------------ 生成速度（取自日志）

def to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def ws_slugs(projects_dir):
    """列出工作空间目录名（全路径 slug），用来把日志短名对应回工作空间"""
    if not os.path.isdir(projects_dir):
        return []
    return [d for d in os.listdir(projects_dir)
            if os.path.isdir(os.path.join(projects_dir, d))]


def match_log_ws(short, slugs):
    """日志文件名里是工作空间短名（cclear），projects 里是路径 slug
    （d-1-a-mycode-ai-code-cclear）——短名就是 slug 的最后一段。

    匹配不上的一律不计入：那些是 WorkBuddy 自身功能的日志
    （teams / sdk / daemon-* / edge-sync / unknown-workspace 等），不属于用户的工作空间。
    """
    if not short:
        return None
    hits = [s for s in slugs if s == short or s.endswith("-" + short)]
    return max(hits, key=len) if hits else None


def load_speed(logs_dir, slugs, since_ms, until_ms, ws_filter):
    """扫描 logs/<日期>/<工作区>__<hash>.log 的 qua.end 行，取出耗时与输出 token。

    qua.end 是每次模型调用结束时写出的一行 JSON，含：
        completion_tokens    输出 token（已含思考链）
        ttft_ms              首字延迟
        total_cost_ms        整轮耗时
        sse_connect_cost_ms  建连 / 等服务端首个响应的耗时

    返回 (rows, meta)；meta["error"] 非空表示这一次没取到速度数据（不影响用量部分）。
    """
    meta = {"days": [], "skipped": collections.Counter(), "error": None}
    if not os.path.isdir(logs_dir):
        meta["error"] = "日志目录不存在：%s" % logs_dir
        return [], meta

    rows = []
    for daydir in sorted(os.listdir(logs_dir)):
        m = LOG_DATE_DIR.match(daydir)
        if not m:
            continue                                  # sdk / teams 等非日期目录跳过
        day = m.group(1)
        try:
            day_start = int(datetime.datetime.strptime(day, "%Y-%m-%d").timestamp() * 1000)
        except ValueError:
            continue
        if since_ms and day_start + 86400000 - 1 < since_ms:
            continue
        if until_ms and day_start > until_ms:
            continue
        base = os.path.join(logs_dir, daydir)
        if not os.path.isdir(base):
            continue
        meta["days"].append(day)

        for fp in sorted(glob.glob(os.path.join(base, "*.log"))):
            fname = os.path.basename(fp)
            short = fname.rsplit("__", 1)[0] if "__" in fname else ""
            if short.endswith(".log"):
                short = short[:-4]
            ws_key = match_log_ws(short, slugs)
            if not ws_key:
                if short:
                    meta["skipped"][short] += 1
                continue
            ws = short                                 # 日志里就是短名，比全路径 slug 好读
            if ws_filter:
                f = ws_filter.lower()
                if f not in ws_key.lower() and f not in ws.lower():
                    continue

            try:
                fh = open(fp, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line in fh:
                    idx = line.find("qua.end:")
                    if idx < 0:
                        continue                       # 绝大多数行在这里就被跳过
                    jm = LOG_JSON.search(line, idx + 8)
                    if not jm:
                        continue
                    try:
                        o = json.loads(jm.group(0))
                    except ValueError:
                        continue
                    if str(o.get("http_end_status_code")) != "200":
                        continue                       # 失败请求不计入速度
                    outp = to_int(o.get("completion_tokens"))
                    total = to_int(o.get("total_cost_ms"))
                    ttft = to_int(o.get("ttft_ms"))
                    if outp <= 0 or total <= 0:
                        continue

                    ts = 0
                    tm = LOG_LINE_TIME.match(line)
                    if tm:
                        try:
                            ts = int(datetime.datetime(
                                int(tm.group(1)), int(tm.group(2)), int(tm.group(3)),
                                int(tm.group(4)), int(tm.group(5)), int(tm.group(6))
                            ).timestamp() * 1000)
                        except ValueError:
                            ts = 0
                    if since_ms and ts and ts < since_ms:
                        continue
                    if until_ms and ts and ts > until_ms:
                        continue

                    gen_ms = total - ttft                # 真正在出字的时长
                    rows.append({
                        "ts": ts,
                        "day": day,
                        "ws": ws,
                        "model": (o.get("model") or "").split("/")[-1],
                        "out": outp,
                        "ttft": ttft,
                        "total": total,
                        "connect": to_int(o.get("sse_connect_cost_ms")),
                        "gen_tps": (outp / gen_ms * 1000.0) if gen_ms > 0 else None,
                        "e2e_tps": outp / total * 1000.0,
                    })
    if not rows and meta["error"] is None:
        meta["error"] = "日志里没有可用的速度记录"
    return rows, meta


def median(values):
    v = sorted(values)
    if not v:
        return 0.0
    mid = len(v) // 2
    return float(v[mid]) if len(v) % 2 else (v[mid - 1] + v[mid]) / 2.0


def percentile(values, p):
    """线性插值分位（p 取 0~100）"""
    v = sorted(values)
    if not v:
        return 0.0
    if len(v) == 1:
        return float(v[0])
    k = (len(v) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def speed_summary(rows):
    """把速度记录汇总成（全局统计, 按工作空间明细）"""
    if not rows:
        return None, []
    tps = [r["gen_tps"] for r in rows if r["gen_tps"]]
    overall = {
        "requests": len(rows),
        "out": sum(r["out"] for r in rows),
        "gen_tps": median(tps),
        "gen_tps_p10": percentile(tps, 10),
        "gen_tps_p90": percentile(tps, 90),
        "ttft": median([r["ttft"] for r in rows]),
        "ttft_max": max(r["ttft"] for r in rows),
        "total": median([r["total"] for r in rows]),
        "total_max": max(r["total"] for r in rows),
        "connect_share": median([r["connect"] / r["total"] * 100.0
                                 for r in rows if r["total"]]),
    }
    grouped = collections.defaultdict(list)
    for r in rows:
        grouped[r["ws"]].append(r)
    per_ws = []
    for ws, rs in grouped.items():
        per_ws.append({
            "ws": ws,
            "requests": len(rs),
            "out": sum(x["out"] for x in rs),
            "gen_tps": median([x["gen_tps"] for x in rs if x["gen_tps"]]),
            "ttft": median([x["ttft"] for x in rs]),
            "total": median([x["total"] for x in rs]),
            "connect_share": median([x["connect"] / x["total"] * 100.0
                                     for x in rs if x["total"]]),
        })
    per_ws.sort(key=lambda x: -x["requests"])
    return overall, per_ws


# ------------------------------------------------------------------ 聚合与报表

def aggregate(records, prices):
    buckets = {
        "model": collections.defaultdict(zero),
        "day": collections.defaultdict(zero),
        "ws": collections.defaultdict(zero),
        "session": collections.defaultdict(zero),
    }
    titles = {}
    grand = zero()
    for r in records:
        r["cost"] = prices.cost(r["model"], r["in"], r["out"], r["cache_read"], r["cache_write"])
        for key in ("model", "day", "ws", "session"):
            k = r[key] if key != "day" else day_key(r["ts"])
            if key == "session":
                k = (r["ws"], r["session"])
            bump(buckets[key][k], r)
            buckets[key][k]["cost"] += r["cost"]
        bump(grand, r)
        grand["cost"] += r["cost"]
        if r["title"]:
            titles[(r["ws"], r["session"])] = r["title"]
    return buckets, titles, grand


def top_items(d, top, by="total"):
    items = sorted(d.items(), key=lambda kv: -kv[1][by])
    return items[:top] if top else items


def build_report(records, sessions, prices, args, buckets, titles, grand, extra):
    out = []
    add = out.append
    ts_list = [r["ts"] for r in records]

    add("WorkBuddy 模型用量审计")
    add("=" * 70)
    add("数据源    : %s" % args.projects)
    add("价格表    : %s" % prices.source)
    if ts_list:
        span = (max(ts_list) - min(ts_list)) / 86400000.0
        add("时间范围  : %s → %s（跨度 %.1f 天）"
            % (fmt_ts(min(ts_list) / 1000), fmt_ts(max(ts_list) / 1000), span))
    else:
        add("时间范围  : 无匹配数据")
    cond = []
    if args.days:
        cond.append("最近 %d 天" % args.days)
    if args.since or args.until:
        cond.append("%s ~ %s" % (args.since or "开始", args.until or "现在"))
    if args.ws:
        cond.append("工作空间含 '%s'" % args.ws)
    if args.model:
        cond.append("模型含 '%s'" % args.model)
    if cond:
        add("筛选条件  : %s" % "；".join(cond))
    add("")

    add("【总览】")
    add("  请求数（含子代理） : %s" % num(grand["calls"]))
    add("  会话数（有调用）   : %s" % num(len(sessions)))
    add("  工作空间数         : %s" % num(len(buckets["ws"])))
    add("  涉及模型数         : %s" % num(len(buckets["model"])))
    add("  输入 tokens        : %s" % num(grand["in"]))
    add("    └ 缓存命中       : %s（%s）" % (num(grand["cache_read"]), pct(grand["cache_read"], grand["in"])))
    if grand["cache_write"]:
        add("    └ 缓存写入       : %s" % num(grand["cache_write"]))
    fresh = max(0, grand["in"] - grand["cache_read"] - grand["cache_write"])
    add("    └ 全价输入       : %s（%s）" % (num(fresh), pct(fresh, grand["in"])))
    add("  输出 tokens        : %s" % num(grand["out"]))
    add("  合计 tokens        : %s" % num(grand["total"]))
    if grand["calls"]:
        add("  单次请求平均       : 输入 %s / 输出 %s / 合计 %s" % (
            num(grand["in"] // grand["calls"]), num(grand["out"] // grand["calls"]),
            num(grand["total"] // grand["calls"])))
    if not args.no_cost:
        add("  预估金额           : ≈ %.2f 元（按牌价估算，仅供参考）" % grand["cost"])
    add("")

    if not args.no_speed:
        add("【生成速度】数据源：logs/qua.end（用量账本没有耗时字段，速度只能取自日志）")
        speed_rows, speed_meta = extra.get("speed", ([], {}))
        if not speed_rows:
            if args.ws:
                add("  本次未取到速度数据：筛选条件 '%s' 下没有对应的日志记录。" % args.ws)
            else:
                add("  本次未取到速度数据：%s" % (speed_meta.get("error") or "无"))
        else:
            overall, per_ws = speed_summary(speed_rows)
            add("  %-30s %6s %11s %12s %11s %11s" % (
                "工作区（短名）", "请求", "输出tok", "生成tok/s", "首字ms", "整轮ms"))
            for w in per_ws[:args.top]:
                add("  %-30s %6s %11s %12.1f %11s %11s" % (
                    w["ws"][:30], num(w["requests"]), num(w["out"]),
                    w["gen_tps"], num(w["ttft"]), num(w["total"])))
            add("  ——汇总—— 生成速度中位 %.1f tok/s（P10~P90 %.1f ~ %.1f）｜ "
                "首字延迟中位 %s ms（最慢 %s）"
                % (overall["gen_tps"], overall["gen_tps_p10"], overall["gen_tps_p90"],
                   num(overall["ttft"]), num(overall["ttft_max"])))
            add("            整轮耗时中位 %s ms（最慢 %s）｜ 等待首字占整轮 %s"
                % (num(overall["total"]), num(overall["total_max"]),
                   pct(overall["connect_share"], 100)))
            days = speed_meta.get("days") or []
            if days:
                add("  日志覆盖：%s ~ %s（共 %d 天）" % (min(days), max(days), len(days)))
        add("")

    groups = args.group or ["model", "day", "ws"]
    if "model" in groups:
        add("【按模型】")
        for key, c in top_items(buckets["model"], args.top):
            add("  %-34s 调用 %6s  输入 %14s  输出 %11s  合计 %14s  命中率 %6s%s" % (
                key[:34], num(c["calls"]), num(c["in"]), num(c["out"]), num(c["total"]),
                pct(c["cache_read"], c["in"]),
                "" if args.no_cost else "  预估 %9.2f 元" % c["cost"]))
        add("")

    if "day" in groups:
        add("【按天】")
        for key, c in sorted(buckets["day"].items()):
            add("  %-12s 调用 %6s  输入 %14s  输出 %11s  合计 %14s%s" % (
                key, num(c["calls"]), num(c["in"]), num(c["out"]), num(c["total"]),
                "" if args.no_cost else "  预估 %9.2f 元" % c["cost"]))
        add("")

    if "ws" in groups:
        add("【按工作空间】")
        for key, c in top_items(buckets["ws"], args.top):
            add("  %-42s 调用 %6s  合计 %14s%s" % (
                key[:42], num(c["calls"]), num(c["total"]),
                "" if args.no_cost else "  预估 %9.2f 元" % c["cost"]))
        add("")

    if "session" in groups:
        add("【按会话】")
        for (ws, sid), c in top_items(buckets["session"], args.top):
            add("  %-18s %-30s 调用 %5s  合计 %13s" % (
                ws[:18], (titles.get((ws, sid)) or sid)[:30], num(c["calls"]), num(c["total"])))
        add("")

    if args.requests:
        add("【最近 %d 条请求】" % args.requests)
        add("  %-17s %-22s %-28s %9s %7s %10s %7s" % (
            "时间", "工作空间", "模型", "输入", "输出", "合计", "金额"))
        for r in sorted(records, key=lambda x: -x["ts"])[:args.requests]:
            add("  %-17s %-22s %-28s %9s %7s %10s %7.4f" % (
                fmt_ts(r["ts"] / 1000), r["ws"][:22], r["model"][:28],
                num(r["in"]), num(r["out"]), num(r["total"]), r["cost"]))
        add("")

    if args.context:
        add("【会话上下文占用】workbuddy.db → session_usage（与模型计费无关）")
        rows, err = extra.get("context", ([], None))
        if err:
            add("  读取失败：%s" % err)
        elif not rows:
            add("  （无记录）")
        else:
            add("  %-12s %-30s %12s %12s %8s" % ("更新时间", "工作目录", "已用", "窗口", "占比"))
            for r in rows[:args.top]:
                add("  %-12s %-30s %12s %12s %8s" % (
                    fmt_ts((r["updated_at"] or 0) / 1000)[5:16], (r["cwd"] or "-")[:30],
                    num(r["used"]), num(r["size"]), pct(r["used"], r["size"])))
            add("  说明：credit_json 是内置积分模型的积分，走外部 API 时不产生消耗，故不列出。")
        add("")

    add("【口径说明】")
    add("  1. 数值取自本机 jsonl 的 providerData.rawUsage，是供应商返回的真实 usage，不是估算。")
    add("  2. 输入 tokens 已包含缓存命中；全价输入 = 输入 - 缓存读 - 缓存写。")
    add("  3. 预估金额按价格表牌价计算，与实际套餐/中转商单价无关，仅用于横向比较。")
    add("     → 要贴合真实账单，请修改 assets/prices.json，或用 --price-in/--price-out/--price-cache 覆盖。")
    add("  4. 只统计「有 usage 字段」的请求；供应商不回 usage 时该模型不会出现在报表里。")
    add("  5. 全程本地读取，无任何上传。")
    if not args.no_speed:
        add("  6. 【生成速度】取自 logs 下的 qua.end 记录：")
        add("     生成速度 = 输出token ÷ (总耗时 − 首字延迟)；端到端速度 = 输出token ÷ 总耗时；")
        add("     输出 token 已含思考链。它与用量账本不同源，只覆盖日志仍在的日期——")
        add("     日志按日期留存，一旦被归档清理，那几天的速度就不再可得。")
        add("     只统计日志里成功（HTTP 200）且有输出 token 的请求，故请求数可能与用量节的略有出入；")
        add("     工作区在速度节显示为短名（如 cclear），对应用量节里 slug 的最后一段。")
    return "\n".join(out)


def write_csv(path, records):
    cols = ["ts", "datetime", "ws", "session", "title", "model", "agent",
            "in", "out", "total", "cache_read", "cache_write", "cost"]
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in sorted(records, key=lambda x: x["ts"]):
            w.writerow([r["ts"], fmt_ts(r["ts"] / 1000), r["ws"], r["session"], r["title"],
                        r["model"], r["agent"], r["in"], r["out"], r["total"],
                        r["cache_read"], r["cache_write"], "%.6f" % r["cost"]])
    return path


# ------------------------------------------------------------------ 入口

def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 模型用量审计")
    ap.add_argument("--projects", default=DEFAULT_PROJECTS, help="会话 jsonl 目录")
    ap.add_argument("--db", default=DEFAULT_DB, help="workbuddy.db 路径")
    ap.add_argument("--sessions-dir", default=DEFAULT_SESSIONS_DIR, help="会话状态目录")
    ap.add_argument("--days", type=int, default=0, help="最近 N 天")
    ap.add_argument("--since", default="", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--until", default="", help="结束日期 YYYY-MM-DD")
    ap.add_argument("--ws", default="", help="工作空间过滤（子串）")
    ap.add_argument("--model", default="", help="模型过滤（子串）")
    ap.add_argument("--group", action="append", default=[],
                    choices=["model", "day", "ws", "session"], help="输出哪些分组，可重复")
    ap.add_argument("--top", type=int, default=25, help="每组最多显示行数")
    ap.add_argument("--requests", type=int, default=0, help="附最近 N 条请求明细")
    ap.add_argument("--context", action="store_true", help="附会话上下文占用")
    ap.add_argument("--logs", default=DEFAULT_LOGS, help="日志目录（生成速度的数据源）")
    ap.add_argument("--no-speed", action="store_true", help="不统计生成速度")
    ap.add_argument("--speed-days", type=int, default=SPEED_DEFAULT_DAYS,
                    help="未指定时间范围时速度只看最近 N 天日志，0=不限（默认 %d）" % SPEED_DEFAULT_DAYS)
    ap.add_argument("--prices", default=DEFAULT_PRICES, help="价格表 json 路径")
    ap.add_argument("--price-in", type=float, default=None, help="兜底输入价（元/百万）")
    ap.add_argument("--price-out", type=float, default=None, help="兜底输出价（元/百万）")
    ap.add_argument("--price-cache", type=float, default=None, help="兜底缓存价（元/百万）")
    ap.add_argument("--no-cost", action="store_true", help="不计算金额")
    ap.add_argument("--json", action="store_true", help="额外输出 JSON")
    ap.add_argument("--csv", default="", help="导出逐条明细 CSV")
    ap.add_argument("--out", default="", help="把文本报告写入指定文件（建议 .md）")
    args = ap.parse_args()

    if not os.path.isdir(args.projects):
        print("找不到会话目录：%s\n本机可能还没用过 WorkBuddy。" % args.projects, file=sys.stderr)
        return 2

    override = None
    if args.price_in is not None or args.price_out is not None or args.price_cache is not None:
        override = [args.price_in if args.price_in is not None else 2.0,
                    args.price_out if args.price_out is not None else 8.0,
                    args.price_cache if args.price_cache is not None else 0.4]
    prices = PriceTable(args.prices, override)

    since_ms = until_ms = None
    if args.days:
        since_ms = int((datetime.datetime.now() - datetime.timedelta(days=args.days)).timestamp() * 1000)
    if args.since:
        since_ms = to_ms(args.since)
    if args.until:
        until_ms = to_ms(args.until, end_of_day=True)

    records, sessions = load_records(args.projects, since_ms, until_ms, args.ws, args.model)
    buckets, titles, grand = aggregate(records, prices)

    extra = {}
    if args.context:
        extra["context"] = read_context_usage(args.db, args.sessions_dir)
    if not args.no_speed:
        s_since, s_until = since_ms, until_ms
        if not since_ms and not until_ms and args.speed_days:
            s_since = int((datetime.datetime.now()
                           - datetime.timedelta(days=args.speed_days)).timestamp() * 1000)
        extra["speed"] = load_speed(args.logs, ws_slugs(args.projects),
                                    s_since, s_until, args.ws)

    report = build_report(records, sessions, prices, args, buckets, titles, grand, extra)
    print(report)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print("\n[已写出报告] %s" % args.out)
    if args.csv:
        write_csv(args.csv, records)
        print("[已写出明细] %s（%d 行）" % (args.csv, len(records)))
    if args.json:
        base = args.out[:-3] if args.out.lower().endswith(".md") else "usage-audit"
        path = base + ".json"
        payload = {
            "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "source": args.projects,
            "price_source": prices.source,
            "range": {
                "from": fmt_ts(min((r["ts"] for r in records), default=0) / 1000),
                "to": fmt_ts(max((r["ts"] for r in records), default=0) / 1000),
            },
            "totals": dict(grand),
            "by_model": {k: dict(v) for k, v in buckets["model"].items()},
            "by_day": {k: dict(v) for k, v in buckets["day"].items()},
            "by_ws": {k: dict(v) for k, v in buckets["ws"].items()},
        }
        s_rows, s_meta = extra.get("speed", ([], {}))
        s_overall, s_per_ws = speed_summary(s_rows)
        if s_overall:
            payload["speed"] = {
                "source": args.logs,
                "days": s_meta.get("days", []),
                "overall": s_overall,
                "by_ws": s_per_ws,
            }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print("[已写出 JSON] %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
