# -*- coding: utf-8 -*-
"""
wb-usage-audit 自测：验证脚本的可移植性与字段兼容性。

用「异机」合成数据跑一遍，确认脚本不依赖本机特征：
  - 工作空间目录名是别的机器/别的系统的 slug
  - 混入 Anthropic 风格 providerData.usage（inputTokens/outputTokens/totalTokens）
  - 混入损坏 JSON 行、缺 timestamp 的行、没有 usage 的本地事件
  - 账本放在临时目录，并且**不提供** prices.json

跑法（在技能目录下）：
    python tests/test_generic.py
预期输出以「✅ 全部通过」结尾。任何一项 ❌ 都说明改动破坏了兼容性。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
SCRIPT = os.path.join(SKILL_DIR, "scripts", "usage_audit.py")


def build_fixture(root):
    proj = os.path.join(root, "projects")
    ws = os.path.join(proj, "home-bot-macbook-Documents-work-2026-10-01")
    os.makedirs(os.path.join(ws, "sess-bbb", "subagents"))

    recs_a = [
        {"timestamp": 1791900000000, "role": "user", "content": "帮我看看这个月的用量"},
        '{"timestamp": 1791900001000, "broken": ',                 # 损坏行
        {"timestamp": 1791900002000, "type": "message", "role": "assistant",
         "content": "好的"},                                        # 无 usage
        # Anthropic 驼峰风格
        {"timestamp": 1791900003000, "type": "function_call",
         "providerData": {"model": "claude-opus-5", "agentName": "cli",
                          "usage": {"inputTokens": 1000, "outputTokens": 200,
                                    "totalTokens": 1200}}},
        # OpenAI 风格 + 缓存命中
        {"timestamp": 1791900004000, "type": "function_call",
         "providerData": {"model": "gpt-4o", "agentName": "cli",
                          "rawUsage": {"prompt_tokens": 500, "completion_tokens": 50,
                                       "total_tokens": 550,
                                       "prompt_tokens_details": {"cached_tokens": 400}}}},
        # 缺 timestamp，应被跳过
        {"type": "function_call",
         "providerData": {"model": "should-not-appear",
                          "rawUsage": {"prompt_tokens": 1, "completion_tokens": 1,
                                       "total_tokens": 2}}},
    ]
    with open(os.path.join(ws, "sess-aaa.jsonl"), "w", encoding="utf-8") as fh:
        for r in recs_a:
            fh.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")

    with open(os.path.join(ws, "sess-bbb", "subagents", "agent-x.jsonl"),
              "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"timestamp": 1791900005000, "type": "function_call",
                             "providerData": {"model": "some-model",
                                              "rawUsage": {"prompt_tokens": 100,
                                                           "completion_tokens": 10,
                                                           "total_tokens": 110}}}) + "\n")
    return proj


def main():
    root = tempfile.mkdtemp(prefix="wb-usage-test-")
    try:
        proj = build_fixture(root)
        proc = subprocess.run(
            [sys.executable, SCRIPT, "--projects", proj,
             "--db", os.path.join(root, "none.db"),
             "--sessions-dir", os.path.join(root, "none"),
             "--prices", os.path.join(root, "no-such-prices.json"),
             "--group", "model", "--group", "ws", "--group", "session"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = proc.stdout
        print(out)
        if proc.stderr.strip():
            print("--- stderr ---\n" + proc.stderr)

        checks = [
            ("识别 Anthropic 驼峰风格 usage（共 3 条记录）", "请求数（含子代理） : 3" in out),
            ("会话数 = 2（子代理归属父会话）", "会话数（有调用）   : 2" in out),
            ("工作空间数 = 1", "工作空间数         : 1" in out),
            ("涉及模型数 = 3（损坏行/无 usage/缺时间戳均被跳过）", "涉及模型数         : 3" in out),
            ("价格表缺失时回退内置默认而非崩溃", "价格表    : " in out),
            ("上下文库缺失时安全跳过", proc.returncode == 0),
            ("缓存命中被正确识别（缓存 400 / 输入 1600 = 25.0%）", "25.0%" in out),
        ]
        ok = True
        print("=" * 70)
        for name, cond in checks:
            print(("  ✅ " if cond else "  ❌ ") + name)
            ok = ok and cond
        print("\n结论：" + ("✅ 全部通过" if ok else "❌ 存在失败"))
        return 0 if ok else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
