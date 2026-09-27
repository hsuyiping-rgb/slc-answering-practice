# -*- coding: utf-8 -*-
"""半鏈路端對端測試：真實課堂音檔 → Whisper → 判定器

用 503 公開課的原始錄音，測「語音辨識的錯字會不會讓判定翻掉」。
測試集裡有七題本來就有原始錄音，正好拿來當端對端樣本。

用法：
    python judge/run_e2e.py
    python judge/run_e2e.py --model gemini-3.5-flash

前置：ffmpeg、GROQ_API_KEY（Whisper）、GEMINI_API_KEY（判定器）
"""
import argparse, json, os, subprocess, sys, tempfile, io, urllib.request, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_eval import call_gemini, to_branch, build_prompt, FUNCTIONS  # 共用同一套判定邏輯
from strategy import HALLUCINATION, looks_hallucinated  # noqa: F401  ← functions/ 的那一份

HERE = os.path.dirname(os.path.abspath(__file__))
# stdout 已在 run_eval 匯入時包成 UTF-8，這裡不可再包一次（會關掉底層 buffer）

AUDIO_DIR = os.path.join(os.path.dirname(HERE), "..", "學習共同體課堂影片分析", "公開課檔案506")
F1 = os.path.join(AUDIO_DIR, "全班影片1_音檔.wav")
F2 = os.path.join(AUDIO_DIR, "全班影片2_音檔.wav")

# 逐字稿依據見 素材_503數學_省水馬桶_講者重建.md
CLIPS = [
    {"id": "E1", "src": F1, "ss": "00:13:41", "t": 18, "test": 5,
     "ref": "我沒有聽得很清楚，不太懂你的意思，你可以再說一次，或者是你旁邊的人，沒有辦法幫忙說一下，還是你要再說一次",
     "expect": {"strategy": "釐清"}},
    {"id": "E2", "src": F1, "ss": "00:14:05", "t": 13, "test": 3,
     "ref": "叫前面三個，你所謂的前面三個，回到題目來，你告訴我前面三個是指什麼",
     "expect": {"strategy": "深究", "flags": {"returns_to_text": True}}},
    {"id": "E3", "src": F1, "ss": "00:14:18", "t": 4, "test": 4,
     "ref": "有人可以幫他嗎",
     "expect": {"strategy_in": ["轉引", "轉問"], "branch": "B3"}},
    {"id": "E4", "src": F1, "ss": "00:16:49", "t": 5, "test": 6,
     "ref": "好，然後",
     "expect": {"branch": "RETRY"},
     "note": "壓力樣本。「好，然後？」在遠場錄音裡與學生語音重疊，切不乾淨（加寬視窗只會吃進更多別人的話）。"
             "產品情境是老師戴麥克風獨自說話，不會有這個狀況。此處驗證的是：**辨識結果無意義時，系統要走 RETRY 而不是亂猜**"},
    {"id": "E5", "src": F2, "ss": "00:11:05", "t": 8, "test": 7,
     "ref": "好，這是你用算式告訴我，可不可以不要用算式，再告訴我一次",
     "expect": {"strategy": "深究"}},
    {"id": "E6", "src": F1, "ss": "00:14:49", "t": 10, "test": 10,
     "ref": "聽清楚嗎，小賢講的我覺得很清楚，有沒有人可以再用你的話幫小賢再講一次",
     "expect": {"flags": {"is_evaluative": True}},
     "note": "負向樣本。辨識若吃掉「我覺得很清楚」，蓋章就偵測不到"},
    {"id": "E7", "src": F2, "ss": "00:02:11", "t": 7, "test": None,
     "ref": "省下多少，水費，水費還是水量，水量",
     "expect": {"strategy_in": ["反問", "釐清"]},
     "judge": False,
     "note": "**只驗證辨識，不驗證判定**。視窗從 4 秒放寬到 7 秒後，關鍵的重複「水費水費」才辨識得出來"
             "（CER 從 25% 降到 0%）——那個重複正是反問的成立要件，所以產品端不可修剪錄音前後的靜音。"
             "但放寬後的片段同時錄進了學生的「省下多少」與「水量」，判定器會把答案當成老師講的而判代答。"
             "遠場課堂錄音切不出單一講者，這後半段要等產品情境（老師戴麥克風獨自說話）的實錄才能測"},
]


try:                                     # Whisper 有時吐簡體，內容其實是對的
    import opencc                        # 不轉換的話 CER 會嚴重虛高
    _s2t = opencc.OpenCC("s2twp").convert
except Exception:
    _s2t = lambda x: x


def cer(ref, hyp):
    """字元錯誤率（Levenshtein / len(ref)）。標點去掉、簡體轉繁體再比。"""
    strip = lambda s: "".join(c for c in _s2t(s) if c not in "，。？！、；：「」『』…　 .,?!")
    r, h = strip(ref), strip(hyp)
    if not r:
        return 0.0
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i]
        for j, hc in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / len(r)


# 實測：15 秒安靜送進 Whisper 會吐出「多謝您的收看」這類幻覺。
# 等待是本系統的核心教學點，靜音必須在送出前就攔下來，不能靠模型自律。
# 靜音判斷不能用絕對音量：實測這支麥克風的底噪中位數 0.0095，
# 而 15 秒安靜（R17）的整體 RMS 是 0.0091——跟語音只差 2.3 倍，換支麥克風就失效。
# 改看「起伏比」：語音有明顯高低起伏，底噪是平的。
#   實測 R17 = 1.1，16 個語音樣本 = 3.4～10.6。閾值 2.0，兩邊各有 3 倍餘裕。
DYNAMIC_MIN = 2.0


def dynamic_ratio(path, sr=16000, win=0.03):
    """幀級 RMS 的 P95 / 中位數。語音 >3，靜音 ~1。"""
    import wave, numpy as np
    w = wave.open(path)
    x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float) / 32768.0
    n = int(sr * win)
    if len(x) < n * 4:
        return 0.0
    v = np.array([np.sqrt((x[i:i + n] ** 2).mean()) for i in range(0, len(x) - n, n)])
    return float(np.percentile(v, 95) / (np.median(v) + 1e-9))


def is_silent(path):
    """靜音閘門：全靜音就不要呼叫 Whisper，直接判 WAIT。

    實測 15 秒安靜送進 Whisper 會吐出「多謝您的收看」這類幻覺。等待是本系統的
    核心教學點，靜音必須在送出前攔下來，不能靠模型自律，也省一次 API 呼叫。
    """
    return dynamic_ratio(path) < DYNAMIC_MIN




def whisper(path):
    """Groq whisper-large-v3。不帶提示詞——正式服務不會知道老師要說什麼。"""
    boundary = uuid.uuid4().hex
    parts = []
    def field(name, value):
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, name, value)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                  "Content-Type: audio/wav\r\n\r\n" % boundary).encode())
    parts.append(open(path, "rb").read())
    parts.append(b"\r\n")
    field("model", "whisper-large-v3")
    field("language", "zh")
    field("response_format", "json")
    field("temperature", "0")
    parts.append(("--%s--\r\n" % boundary).encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/transcriptions", body,
        {"Content-Type": "multipart/form-data; boundary=" + boundary,
         "Authorization": "Bearer " + os.environ["GROQ_API_KEY"],
         "User-Agent": "slc-answering-judge/0.1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r).get("text", "").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemini-3.7-flash")
    a = ap.parse_args()
    system = build_prompt()                  # C01；與線上組法相同
    tmp = tempfile.mkdtemp()

    print("半鏈路端對端：真實課堂音檔 → Whisper → 判定器（%s）\n" % a.model)
    ok_all = True
    for c in CLIPS:
        wav = os.path.join(tmp, c["id"] + ".wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", c["ss"], "-t", str(c["t"]),
                        "-i", c["src"], "-ac", "1", "-ar", "16000", wav], check=True)
        hyp = whisper(wav)
        e = cer(c["ref"], hyp)

        if c.get("judge") is False:
            print("%s  CER %.0f%%（只驗證辨識）" % (c["id"], e * 100))
            print("   逐字稿：%s" % c["ref"])
            print("   辨識　：%s" % hyp)
            print("   備註　：%s" % c["note"]); print()
            continue

        try:
            got = json.loads(call_gemini(a.model, system, hyp))
        except Exception as ex:
            print("%s  判定失敗：%s\n" % (c["id"], ex)); ok_all = False; continue

        strat = got.get("strategy"); flags = got.get("flags") or {}
        br = to_branch(strat, got.get("confidence"), flags)
        errs = []
        exp = c["expect"]
        if "strategy" in exp and strat != exp["strategy"]:
            errs.append("strategy=%s 應為 %s" % (strat, exp["strategy"]))
        if "strategy_in" in exp and strat not in exp["strategy_in"]:
            errs.append("strategy=%s 不在 %s" % (strat, exp["strategy_in"]))
        for k, v in (exp.get("flags") or {}).items():
            if bool(flags.get(k)) != v:
                errs.append("flags.%s=%s 應為 %s" % (k, flags.get(k), v))
        if "branch" in exp and br != exp["branch"]:
            errs.append("branch=%s 應為 %s" % (br, exp["branch"]))
        span_ok = (got.get("evidence_span") or "") in hyp
        if not span_ok:
            errs.append("evidence_span 不是辨識結果的子字串")
        ok_all = ok_all and not errs

        print("%s（對應測試集第 %s 題）  CER %.0f%%" % (c["id"], c["test"], e * 100))
        print("   逐字稿：%s" % c["ref"])
        print("   辨識　：%s" % hyp)
        print("   判定　：%s / %s  %s" % (strat, br, "✅" if not errs else "❌ " + "；".join(errs)))
        if c.get("note"):
            print("   備註　：%s" % c["note"])
        print()

    print("=" * 80)
    print("端對端%s" % ("全部通過 ✅" if ok_all else "有失敗項 ❌"))


if __name__ == "__main__":
    main()
