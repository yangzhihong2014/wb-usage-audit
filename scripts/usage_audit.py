# -*- coding: utf-8 -*-
"""
usage_audit.py —— WorkBuddy 模型用量审计（请求级真实 usage）

数据源：~/.workbuddy/projects/<工作空间>/<会话>.jsonl 里每条记录的
        providerData.rawUsage（供应商响应里回的真实 usage，不是估算）
        providerData.model          → 模型名
        记录顶层 timestamp（毫秒）  → 时间
        子代理 subagents/agent-*.jsonl 自动归属父会话

用途：回答「我这个模型用了多少 token / 花了多少 / 哪个项目最贵 / 缓存命中率多少」，
      输出纯文本（默认）、Markdown（--out）、JSON（--json）或 CSV（--csv）。

仅依赖 Python 标准库（3.8+）。

常用示例：
    python usage_audit.py                                   # 全量总览
    python usage_audit.py --days 7                          # 最近 7 天
    python usage_audit.py --since 2026-10-01 --until 2026-10-14
    python usage_audit.py --model deepseek                   # 只看某模型（子串匹配）
    python usage_audit.py --ws novel-writer-agent             # 只看某工作空间（子串匹配）
    python usage_audit.py --group day                         # 只按天看
    python usage_audit.py --requests 15                       # 附最近 15 条请求明细
    python usage_audit.py --context                           # 附会话上下文占用
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
DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".workbuddy", "workbuddy.db")
DEFAULT_SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "sessions")
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PRICES = os.path.join(os.path.dirname(HERE), "assets", "prices.json")
INTERNAL_TAG = re.compile(r"^\s*<")


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
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print("[已写出 JSON] %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
