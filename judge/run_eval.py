# -*- coding: utf-8 -*-
"""情境卡判定器評測（每張卡一份測試集：judge/testset_<card>.json）

用法：
    python judge/run_eval.py --backend gemini --model gemini-3.7-flash
    python judge/run_eval.py --backend groq   --model qwen/qwen3.8-27b
    python judge/run_eval.py --backend gemini --model gemini-3.5-flash --repeat 3
    python judge/run_eval.py --backend gemini --model gemini-3.7-flash --card c04

金鑰從環境變數讀（GEMINI_API_KEY / GROQ_API_KEY），不寫進檔案、不進版控。
"""
import argparse, json, os, sys, urllib.request, urllib.error, io, time

HERE = os.path.dirname(os.path.abspath(__file__))
FUNCTIONS = os.path.join(os.path.dirname(HERE), "functions")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 分支對映與 prompt 都住在 functions/，因為那是實際部署的那一份。
# 測試集必須驗證「線上跑的東西」，不是它的複本——否則兩邊走鐘，14/14 就失去意義。
sys.path.insert(0, FUNCTIONS)
from strategy import BRANCH, MIN_CONFIDENCE, to_branch, build_prompt, CARDS  # noqa: E402,F401


# ---------------------------------------------------------------- backends
def call_gemini(model, system, user, timeout=90):
    key = os.environ["GEMINI_API_KEY"]
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 {"Content-Type": "application/json",
                                  "User-Agent": "slc-answering-judge/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return d["candidates"][0]["content"]["parts"][0]["text"]


def call_groq(model, system, user, timeout=90):
    key = os.environ["GROQ_API_KEY"]
    body = {
        "model": model, "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        json.dumps(body).encode(),
        {"Content-Type": "application/json", "Authorization": "Bearer " + key,
         # Groq 走 Cloudflare，urllib 的預設 UA 會被擋成 403 error code 1010
         "User-Agent": "slc-answering-judge/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return d["choices"][0]["message"]["content"]


BACKENDS = {"gemini": call_gemini, "groq": call_groq}


# ---------------------------------------------------------------- 檢查
def check(case, got, card="c01"):
    """回傳 (通過的檢查, 失敗訊息 list)"""
    errs = []
    exp = case["expect"]
    strat = got.get("strategy")
    flags = got.get("flags") or {}

    if "strategy_in" in exp:
        if strat not in exp["strategy_in"]:
            errs.append("strategy=%s 不在 %s" % (strat, exp["strategy_in"]))
    elif "strategy" in exp:
        allow = exp.get("strategy_in", [exp["strategy"]])
        if strat not in allow:
            errs.append("strategy=%s 應為 %s" % (strat, exp["strategy"]))

    for k, v in (exp.get("flags") or {}).items():
        if bool(flags.get(k)) != v:
            errs.append("flags.%s=%s 應為 %s" % (k, flags.get(k), v))

    if "secondary_in" in exp:
        if got.get("secondary") not in exp["secondary_in"]:
            errs.append("secondary=%s 不在 %s" % (got.get("secondary"), exp["secondary_in"]))

    # evidence_span 必須是原話的連續子字串（擋幻覺）
    span = got.get("evidence_span") or ""
    if case.get("input") and span not in case["input"]:
        errs.append("evidence_span 不是原話子字串：%r" % span[:30])

    if "branch" in exp:
        b = to_branch(strat, got.get("confidence"), flags, card)
        if b != exp["branch"]:
            errs.append("branch=%s 應為 %s" % (b, exp["branch"]))
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True, choices=list(BACKENDS))
    ap.add_argument("--model", required=True)
    ap.add_argument("--repeat", type=int, default=1, help="每題跑幾次，看穩定度")
    ap.add_argument("--card", default="c01", choices=sorted(CARDS))
    a = ap.parse_args()

    system = build_prompt(a.card)            # 與線上 answering_judge 組出來的是同一份
    ts = json.load(open(os.path.join(HERE, "testset_%s.json" % a.card), encoding="utf-8"))
    call = BACKENDS[a.backend]

    print("後端 %s / 模型 %s / 每題 %d 次\n" % (a.backend, a.model, a.repeat))
    print("%-3s %-28s %-6s %-6s %s" % ("#", "輸入（截斷）", "判定", "分支", "結果"))
    print("-" * 88)

    results = {}
    for c in ts["cases"]:
        cid = c["id"]
        if c.get("llm") is False:
            b = to_branch("等待", 1.0, {}, a.card)
            ok = b == c["expect"]["branch"]
            results[cid] = [[] if ok else ["branch=%s" % b]]
            print("%-3d %-28s %-6s %-6s %s" % (cid, "（無語音，程式路徑）", "等待", b,
                                               "✅" if ok else "❌"))
            continue

        runs = []
        for _ in range(a.repeat):
            try:
                raw = call(a.model, system, c["input"])
                got = json.loads(raw)
            except urllib.error.HTTPError as e:
                runs.append(["HTTP %s: %s" % (e.code, e.read()[:120])]); continue
            except Exception as e:
                runs.append(["呼叫或解析失敗: %s" % e]); continue
            errs = check(c, got, a.card)
            runs.append(errs)
            last = (got, errs)
            time.sleep(0.3)
        results[cid] = runs

        got = locals().get("last", ({}, []))[0] if runs else {}
        try:
            strat = got.get("strategy", "?")
            br = to_branch(strat, got.get("confidence"), got.get("flags") or {}, a.card)
        except Exception:
            strat, br = "?", "?"
        npass = sum(1 for r in runs if not r)
        mark = "✅" if npass == len(runs) else ("⚠️ %d/%d" % (npass, len(runs)) if npass else "❌")
        detail = "" if npass == len(runs) else "  ← " + "；".join(runs[-1][:2])
        print("%-3d %-28s %-6s %-6s %s%s" % (cid, (c["input"] or "")[:26], strat, br, mark, detail))

    # -------- 硬性門檻（寫在各卡測試集的 gates 裡）--------
    print("\n" + "=" * 88)
    def allpass(ids):
        return all(all(not r for r in results.get(i, [["缺"]])) for i in ids)

    gates = [(g["name"], allpass(g["ids"])) for g in ts["gates"]]
    for name, ok in gates:
        print("  %s  %s" % ("✅" if ok else "❌", name))
    print("\n判定器%s" % ("通過 ✅" if all(g[1] for g in gates) else "未通過 ❌"))


if __name__ == "__main__":
    main()
