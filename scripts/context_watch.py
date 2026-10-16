# -*- coding: utf-8 -*-
"""
context_watch.py —— 会话上下文膨胀巡检

作用：找出「上下文已经变得很胖、但还在继续用」的会话，在账单爆掉之前提醒切会话。

原理：LLM API 无状态，每次调用都要重发完整上下文。所以
      单日输入 token ≈ Σ(每次调用的上下文长度) ≈ 平均上下文 × 调用次数。
      会话越长，每一次新调用就越贵 —— 这是高缓存命中场景下最主要的成本来源。

用法：
    python context_watch.py                       # 最近 24 小时，阈值 20 万 token
    python context_watch.py --hours 48 --threshold 150000
    python context_watch.py --json                # 机器可读，便于自动化
    python context_watch.py --rate 0.19           # 自定义估算单价（美元/百万 token）

数据源：~/.workbuddy/projects/**/*.jsonl（请求级真实 usage），只读、不外发。
"""
import argparse
import collections
import datetime
import glob
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECTS = os.path.join(os.path.expanduser("~"), ".workbuddy", "projects")
BEIJING = datetime.timezone(datetime.timedelta(hours=8))
# 默认按「输入+输出」的统一估算单价（美元/百万 token）。0.19 是实测反推出的量级，
# 想更准就用 --rate 传你自己的单价。
DEFAULT_RATE = 0.19


def short_ws(name):
    i = name.rfind("-WorkBuddy-")
    return name[i + 11:] if i >= 0 and name[i + 11:] else name


def num(n):
    return "{:,}".format(int(n))


def collect(hours):
    since = int((datetime.datetime.now() - datetime.timedelta(hours=hours)).timestamp() * 1000)
    sess = collections.defaultdict(lambda: {"inputs": [], "out": 0, "ws": "", "sid": ""})
    titles = {}
    for fp in glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True):
        rel = os.path.relpath(fp, PROJECTS)
        parts = rel.split(os.sep)
        if len(parts) != 2:                      # 子代理文件不单独统计
            continue
        ws = short_ws(parts[0])
        sid = os.path.splitext(parts[1])[0]
        key = (ws, sid)
        for line in open(fp, encoding="utf-8", errors="ignore"):
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            ts = o.get("timestamp")
            if not isinstance(ts, (int, float)) or ts < since:
                continue
            # 会话标题：首条非内部标签的用户消息
            if key not in titles and o.get("role") == "user":
                c = o.get("content")
                t = None
                if isinstance(c, str):
                    t = c
                elif isinstance(c, list):
                    for seg in c:
                        if isinstance(seg, dict) and seg.get("type") in ("input_text", "text"):
                            t = seg.get("text")
                            break
                if t and not t.lstrip().startswith("<"):
                    titles[key] = " ".join(t.split())[:36]
            pd = o.get("providerData")
            if not isinstance(pd, dict):
                continue
            ru = pd.get("rawUsage") or pd.get("usage") or {}
            inp = ru.get("prompt_tokens") or ru.get("inputTokens") or ru.get("promptTokens")
            if not inp:
                continue
            s = sess[key]
            s["ws"], s["sid"] = ws, sid
            s["inputs"].append((int(ts), int(inp)))
            s["out"] += int(ru.get("completion_tokens") or ru.get("outputTokens") or 0)
    for key, s in sess.items():
        s["inputs"].sort()
    return sess, titles


def main():
    ap = argparse.ArgumentParser(description="会话上下文膨胀巡检")
    ap.add_argument("--hours", type=int, default=24, help="回看小时数，默认 24")
    ap.add_argument("--threshold", type=int, default=200000, help="上下文告警阈值（token），默认 200000")
    ap.add_argument("--top", type=int, default=8, help="最多列出几个会话，默认 8")
    ap.add_argument("--rate", type=float, default=DEFAULT_RATE, help="估算单价（美元/百万 token）")
    ap.add_argument("--projects", default=PROJECTS)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    rows = []
    sess, titles = collect(args.hours)
    for (ws, sid), s in sess.items():
        ins = [v for _, v in s["inputs"]]
        if not ins:
            continue
        rows.append({
            "ws": ws, "sid": sid,
            "calls": len(ins),
            "total_in": sum(ins),
            "avg_in": sum(ins) // len(ins),
            "peak_in": max(ins),
            "last_in": ins[-1],
            "out": s["out"],
        })
    rows.sort(key=lambda r: -r["total_in"])
    gross = sum(r["total_in"] + r["out"] for r in rows)
    est_usd = gross / 1e6 * args.rate

    fat = [r for r in rows if r["last_in"] >= args.threshold]

    if args.json:
        print(json.dumps({
            "hours": args.hours, "threshold": args.threshold, "rate_usd_per_m": args.rate,
            "sessions": len(rows), "gross_tokens": gross, "est_usd": round(est_usd, 2),
            "fat_count": len(fat),
            "top": [dict(r, title=titles.get((r["ws"], r["sid"]), "")) for r in rows[:args.top]],
        }, ensure_ascii=False, indent=2))
        return 0

    print("会话上下文巡检 ｜ 最近 %d 小时 ｜ 告警阈值 %s tokens" % (args.hours, num(args.threshold)))
    print("=" * 96)
    if not rows:
        print("这段时间没有模型调用记录。")
        return 0

    print("%-4s%-24s %-10s %7s %11s %11s %11s" % ("", "工作空间", "会话", "调用", "输入合计", "平均上下文", "最新上下文"))
    for r in rows[:args.top]:
        flag = "🔴" if r["last_in"] >= args.threshold else ("🟡" if r["last_in"] >= args.threshold * 0.6 else "  ")
        title = titles.get((r["ws"], r["sid"]), "")
        print("%-4s%-24s %-10s %7s %11s %11s %11s  %s"
              % (flag, r["ws"][:24], r["sid"][:8], num(r["calls"]),
                 num(r["total_in"]), num(r["avg_in"]), num(r["last_in"]), title[:18]))

    print("-" * 96)
    print("会话数 %d ｜ 输入合计 %s tokens ｜ 含输出共 %s tokens"
          % (len(rows), num(sum(r["total_in"] for r in rows)), num(gross)))
    print("按 $%.3f/百万 token 估算 ≈ $%.2f" % (args.rate, est_usd))
    print()
    if fat:
        print("⚠️ 有 %d 个会话的最新上下文已超过 %s tokens：" % (len(fat), num(args.threshold)))
        for r in fat[:args.top]:
            print("   - %s / %s（最新 %s，已调用 %s 次）"
                  % (r["ws"][:24], r["sid"][:8], num(r["last_in"]), num(r["calls"])))
        print("   → 建议：这段任务告一段落就**开新会话**（新会话基线只有约 3.5 万 token），")
        print("     不要把上下文继续堆在同一个会话里；否则后面每次调用都在为前面的历史付费。")
    else:
        print("✅ 没有会话的最新上下文超过阈值，暂不需要干预。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
