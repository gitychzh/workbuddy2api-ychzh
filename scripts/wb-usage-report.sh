#!/usr/bin/env bash
# wb-usage-report.sh — 4 账号池消耗日报（命令行直接看，无需开面板）
#
# 用法（本机直跑或 ECS 上跑）：
#   ./wb-usage-report.sh                 # 全部历史
#   ./wb-usage-report.sh 24              # 最近 24 小时
#   ./wb-usage-report.sh 0               # 全部历史（显式，含 90 天前折叠日桶）
#
# 数据源：面板 GET /panel/api/usage?hours=N（usage.SnapshotWithRates 聚合：
# 内存用量桶 + request-logs JSONL），单次 HTTP，无凭证读取。
set -euo pipefail

PANEL="${WB2A_PANEL:-http://127.0.0.1:1114}"
KEY="${WB2A_API_KEY:?需要 WB2A_API_KEY（面板 api_key）}"
HOURS="${1:-0}"

if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi

TMPJSON=$(mktemp)
trap 'rm -f "$TMPJSON"' EXIT
curl -sf "$PANEL/panel/api/usage?hours=$HOURS" \
  -H "Authorization: Bearer $KEY" > "$TMPJSON"

"$PY" - "$HOURS" "$TMPJSON" <<'PYEOF'
import json, sys

hours = sys.argv[1]
d = json.load(open(sys.argv[2], encoding="utf-8"))
win = "全部历史" if hours in ("0",) else f"最近 {hours} 小时"

t = d["totals"]
print("=" * 76)
print(f"  wb2api 账号池消耗报告  [{win}]  生成于 {d.get('generated','')[:19]}")
print("=" * 76)
print(f"  请求 {t['requests']}  失败 {t['errors']}  "
      f"输入 {t['prompt_tokens']:,} tok  输出 {t['completion_tokens']:,} tok  "
      f"积分消耗 {t['credits']:.2f} (样本 {t['credit_samples']})")
print(f"  平均延迟 {t['avg_latency_ms']:.0f}ms   平均吐字 {t['avg_tokens_per_second']:.1f} tok/s   "
      f"≈{t['credits_per_1m_tokens']:.1f} 积分/百万tok")
print()

uid2nick = {}
for a in d["by_account"]:
    uid2nick[a["key"][:8]] = a.get("extra") or ""

print("─" * 76)
print("  按账号")
print("─" * 76)
hdr = f"  {'账号':<14s} {'请求':>5s} {'失败':>4s} {'输入tok':>13s} {'输出tok':>10s} {'积分':>9s} {'积分/1M':>9s} {'延迟':>7s} {'tok/s':>6s}"
print(hdr)
for a in sorted(d["by_account"], key=lambda x: -x["credits"]):
    nick = a.get("extra") or a["key"][:8]
    print(f"  {nick:<14s} {a['requests']:>5d} {a['errors']:>4d} {a['prompt_tokens']:>13,d} "
          f"{a['completion_tokens']:>10,d} {a['credits']:>9.2f} {a['credits_per_1m_tokens']:>9.2f} "
          f"{a['avg_latency_ms']:>5.0f}ms {a['avg_tokens_per_second']:>6.1f}")

print()
print("─" * 76)
print("  按模型")
print("─" * 76)
print(f"  {'模型':<26s} {'请求':>5s} {'失败':>4s} {'输入tok':>13s} {'输出tok':>10s} {'积分':>9s} {'积分/1M':>9s}")
for m in sorted(d["by_model"], key=lambda x: -x["credits"]):
    name = m.get("extra") or m["key"]
    print(f"  {name:<26s} {m['requests']:>5d} {m['errors']:>4d} {m['prompt_tokens']:>13,d} "
          f"{m['completion_tokens']:>10,d} {m['credits']:>9.2f} {m['credits_per_1m_tokens']:>9.2f}")

print()
print("─" * 76)
print("  时序（按积分消耗排序的前 10 个小时桶）")
print("─" * 76)
series = [s for s in d.get("series", []) if s.get("scope") == "hour"]
for s in sorted(series, key=lambda x: -x.get("credits", 0))[:10]:
    print(f"  {s['t']}  req={s['requests']:>4d} err={s['errors']}  "
          f"in={s['prompt_tokens']:>10,d} out={s['completion_tokens']:>8,d}  credits={s['credits']:.2f}")

print()
print(f"  (明细 JSONL: data/request-logs/*.jsonl，保留 7 天 / 100MiB；"
      f"聚合桶: data/usage.json)")
PYEOF
