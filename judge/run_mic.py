# -*- coding: utf-8 -*-
"""風險 #2 正式驗收：產品情境錄音 → Whisper → 判定器

錄音清單見 judge/recordings/錄音清單.md。把 R01…R17 放進 judge/recordings/
（或它的子資料夾，例如 mic-headset/、mic-laptop/），然後執行：

    python judge/run_mic.py
    python judge/run_mic.py --dir judge/recordings/mic-laptop

會輸出每句的 CER、辨識結果、判定結果，並與課堂遠場錄音的數字對照。
"""
import argparse, glob, json, os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_eval import call_gemini, to_branch, build_prompt, FUNCTIONS
from run_e2e import cer, whisper, is_silent, looks_hallucinated, dynamic_ratio

HERE = os.path.dirname(os.path.abspath(__file__))
EXTS = (".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm", ".aac")


def find(d, rid):
    for e in EXTS:
        for p in (os.path.join(d, rid + e), os.path.join(d, rid.lower() + e)):
            if os.path.exists(p):
                return p
    hits = [p for p in glob.glob(os.path.join(d, rid + "*")) if p.lower().endswith(EXTS)]
    return hits[0] if hits else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(HERE, "recordings"))
    ap.add_argument("--model", default="gemini-3.7-flash")
    a = ap.parse_args()

    spec = json.load(open(os.path.join(HERE, "recordings", "expected.json"), encoding="utf-8"))
    system = build_prompt()                  # C01；與線上組法相同
    tmp = tempfile.mkdtemp()

    print("風險 #2 驗收：%s（判定器 %s）\n" % (a.dir, a.model))
    missing, rows, group = [], [], None

    for it in spec["items"]:
        src = find(a.dir, it["id"])
        if not src:
            missing.append(it["id"]); continue
        if it["group"] != group:
            group = it["group"]; print("── %s " % group + "─" * 50)

        wav = os.path.join(tmp, it["id"] + ".wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src,
                        "-ac", "1", "-ar", "16000", wav], check=True)
        # 靜音閘門先跑，全靜音就不送 Whisper（見 README 發現四）
        silent = is_silent(wav)
        hyp = "" if silent else whisper(wav)   # 一律不帶提示詞（見 README 發現二）

        if it.get("silence"):
            print("%s  起伏比 %.1f → %s  %s" % (it["id"], dynamic_ratio(wav),
                  "靜音，走 WAIT" if silent else "判為有語音", "✅" if silent else "❌"))
            if not silent:
                print("   Whisper 會吐出：%r" % whisper(wav))
            print()
            rows.append((it["id"], None, silent)); continue

        if looks_hallucinated(hyp):
            print("%s  辨識疑似幻覺，攔下：%r" % (it["id"], hyp)); print()
            rows.append((it["id"], None, True)); continue

        e = cer(it["ref"], hyp)
        line = "%s  CER %3.0f%%" % (it["id"], e * 100)
        if "compare_far_field" in it:
            d = e - it["compare_far_field"]
            line += "（課堂遠場 %.0f%%，%s%.0f 個百分點）" % (
                it["compare_far_field"] * 100, "＋" if d > 0 else "－", abs(d) * 100)
        print(line)
        print("   應說：%s" % it["ref"])
        print("   辨識：%s" % hyp)

        if it.get("judge") is False:
            print("   （只驗證辨識）%s\n" % ("　※ " + it["note"] if it.get("note") else ""))
            rows.append((it["id"], e, None)); continue

        try:
            got = json.loads(call_gemini(a.model, system, hyp))
        except Exception as ex:
            print("   判定失敗：%s\n" % ex); rows.append((it["id"], e, False)); continue

        strat, flags = got.get("strategy"), (got.get("flags") or {})
        br = to_branch(strat, got.get("confidence"), flags)
        errs, exp = [], it["expect"]
        if "strategy" in exp and strat != exp["strategy"]:
            errs.append("strategy=%s 應為 %s" % (strat, exp["strategy"]))
        if "strategy_in" in exp and strat not in exp["strategy_in"]:
            errs.append("strategy=%s 不在 %s" % (strat, exp["strategy_in"]))
        for k, v in (exp.get("flags") or {}).items():
            if bool(flags.get(k)) != v:
                errs.append("flags.%s=%s 應為 %s" % (k, flags.get(k), v))
        if "branch" in exp and br != exp["branch"]:
            errs.append("branch=%s 應為 %s" % (br, exp["branch"]))
        print("   判定：%s / %s  %s\n" % (strat, br, "✅" if not errs else "❌ " + "；".join(errs)))
        rows.append((it["id"], e, not errs))

    # ---------------- 總結 ----------------
    print("=" * 72)
    if missing:
        print("缺少錄音：%s\n" % "、".join(missing))
    short = [r for r in rows if r[0] in ("R01", "R02", "R03", "R04", "R05") and r[1] is not None]
    if short:
        avg = sum(r[1] for r in short) / len(short)
        npass = sum(1 for r in short if r[2])
        print("A 組極短句：平均 CER %.0f%%，判定通過 %d/%d" % (avg * 100, npass, len(short)))
        print("  （課堂遠場的短句 CER 是 71–100%%。若這裡明顯改善，代表問題出在遠場錄音而非句長）")
    judged = [r for r in rows if r[2] is not None]
    if judged:
        print("整體判定通過：%d/%d" % (sum(1 for r in judged if r[2]), len(judged)))


if __name__ == "__main__":
    main()
