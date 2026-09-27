# -*- coding: utf-8 -*-
"""語音內容完整性檢查：每段 mp3 → Groq Whisper → 繁體 → 與卡片台詞逐字比對

TTS 偶爾會吞字、改字或自己加字，聽的時候不容易發現。這支把每段轉回文字，
列出與台詞的差異與字元錯誤率（CER），差異大的段落要重聽、重合成。

    python tts/check_audio.py --card c06

金鑰：優先讀 functions/.secret.local（部署用的那把），沒有才用環境變數 GROQ_API_KEY。
"""
import argparse, difflib, io, json, os, re, sys, time, urllib.error, urllib.request, uuid

import opencc

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
S2TW = opencc.OpenCC("s2twp")

# Whisper 會把國字數字寫成阿拉伯數字（一百二十→120、零點一二→0.12），比對前唸回國字
ZH = "零一二三四五六七八九"


def _int_zh(n):
    if n < 10:
        return ZH[n]
    out, s = "", str(n)
    for i, d in enumerate(s):
        unit = ["", "十", "百", "千"][len(s) - 1 - i]
        if d == "0":
            if out and not out.endswith("零") and s[i:].strip("0"):
                out += "零"
            continue
        out += ZH[int(d)] + unit
    return out[1:] if out.startswith("一十") else out


def num_zh(m):
    whole, _, frac = m.group().partition(".")
    return _int_zh(int(whole)) + ("點" + "".join(ZH[int(d)] for d in frac) if frac else "")


def groq_key():
    p = os.path.join(ROOT, "functions", ".secret.local")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if line.startswith("GROQ_API_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ["GROQ_API_KEY"]


def padded(path):
    """頭尾各補 0.5 秒靜音。合成流程把頭尾空白修得很乾淨，Whisper 對「一開口就有聲、
    最後一個字一結束就沒了」的音檔常漏掉第一或最後一個字（2026-09-26 C06：「我們」「度」）。"""
    import subprocess
    return subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-af", "adelay=500:all=1,apad=pad_dur=0.5",
                           "-f", "mp3", "-"], capture_output=True, check=True).stdout


def whisper(path, key):
    b = "----" + uuid.uuid4().hex
    parts = []
    for k, v in {"model": "whisper-large-v3", "language": "zh", "response_format": "json",
                 "temperature": "0"}.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (b, k, v)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.mp3\"\r\n"
                  "Content-Type: audio/mpeg\r\n\r\n" % b).encode() + open(path, "rb").read() + b"\r\n")
    parts.append(("--%s--\r\n" % b).encode())
    req = urllib.request.Request("https://api.groq.com/openai/v1/audio/transcriptions", b"".join(parts),
                                 {"Authorization": "Bearer " + key, "User-Agent": "slc-check/0.1",
                                  "Content-Type": "multipart/form-data; boundary=" + b})
    for n in range(6):
        try:
            return json.load(urllib.request.urlopen(req, timeout=60)).get("text", "")
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(10 * (n + 1))
    raise RuntimeError("Groq 一直回 429")


def norm(s):
    s = re.sub(r"\d+(\.\d+)?", num_zh, S2TW.convert(s))
    return re.sub(r"[^\w]", "", s)


def cer(ref, hyp):
    sm = difflib.SequenceMatcher(None, ref, hyp)
    edits = sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")
    return edits / max(len(ref), 1), sm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", required=True)
    ap.add_argument("--max-cer", type=float, default=0.15, help="超過就標 ⚠️")
    a = ap.parse_args()
    card = json.load(open(os.path.join(ROOT, "public", "cards", a.card + ".json"), encoding="utf-8"))
    segs = [("開場", card["opening"])] + [(b["id"], b) for b in card["branches"]] + [("WAIT", card["wait"])]
    key = groq_key()
    bad = 0
    for label, s in segs:
        path = os.path.join(ROOT, "public", card["audio_base"], s["audio"])
        hyp_raw = whisper(path, key)
        ref, hyp = norm(s["text"]), norm(hyp_raw)
        e, sm = cer(ref, hyp)
        diff = ["%s「%s」→「%s」" % ({"replace": "改", "delete": "漏", "insert": "多"}[op], ref[i1:i2], hyp[j1:j2])
                for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"]
        flag = "⚠️" if e > a.max_cer else "✅"
        bad += e > a.max_cer
        print("%s %-5s %-4s CER %.2f  %s" % (flag, label, s["speaker"], e, s["audio"]))
        print("      台詞：%s\n      聽到：%s" % (s["text"].replace("\n", ""), S2TW.convert(hyp_raw)))
        if diff:
            print("      差異：" + "；".join(diff))
        time.sleep(2)
    print("\n%d 段超過 CER %.2f" % (bad, a.max_cer))


if __name__ == "__main__":
    main()
