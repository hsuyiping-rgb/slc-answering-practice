# -*- coding: utf-8 -*-
"""Gemini TTS 試打：挑音色用

agents.md Q7 於 2026-09-03 改定為 Gemini TTS（原為 OpenAI gpt-4o-mini-tts）。
理由：本機無 OpenAI 金鑰；Groq 的 TTS 只有英文與阿拉伯文。

    python tts/try_gemini.py                  # 兩個角色的候選音色都生
    python tts/try_gemini.py --who xiaoyu     # 只生小宇
    python tts/try_gemini.py --voice Puck --who xiaoliang
"""
import argparse, base64, json, os, subprocess, sys, io, time, urllib.request, wave

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "samples")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
MODEL = "gemini-3.1-flash-tts-preview"

# 台詞取自 規格_情境卡C01_聽不懂的那句話.md。不流暢寫進文本本身，不靠 TTS 演（Q7）
ROLES = {
    "xiaoyu": {
        "label": "小宇（開場白）",
        "line": "就是……那個……嗯……要怎麼把它變成分析……就前面三個……",
        "style": ("用國小五年級男生的聲音說這句話。聲音偏小、有點怯。"
                  "講到一半才發現自己講不清楚，語速越來越慢，尾音往下掉。"
                  "每個刪節號要真的停住大約一秒，不是拖長音。"
                  "最後一句像是講不下去而停掉，不是講完。"),
        "voices": ["Puck", "Fenrir", "Zephyr", "Orus", "Iapetus"],
    },
    "xiaoliang": {
        "label": "小靚（B3 轉引分支）",
        "line": "就是先找出題目給的條件，然後從條件去想可以知道什麼，最後才算。",
        "style": ("用國小五年級女生的聲音說這句話。表達清楚、有條理，幾乎沒有停頓，"
                  "像是心裡已經想好了才開口。不要唸稿腔，是課堂上自然的說話。"
                  "與一個講話結巴、聲音很小的同學形成明顯對比。"),
        "voices": ["Leda", "Aoede", "Callirrhoe", "Autonoe", "Despina"],
    },
    # 2026-09-26：台詞取自 素材_503數學_省水馬桶_講者重建.md（段落 E 15:11、段落 M 05:38）。
    # 候選避開已定案的 Zephyr（小宇）與 Despina（小靚）
    "xiaoxian": {
        "label": "小賢（段落 E 再說一次）",
        "line": ("條件語句是你要先從題目找出它的條件，然後還有它的線索。"
                 "第二個「可以得知的訊息」，是你要再把前面你知道的……在前面你知道是哪一句，"
                 "你要再把它……你要知道裡面的意思。然後最後你才能說計算。"),
        "style": ("用國小五年級男生的聲音說這段話。中等音量、語速偏快，願意講長句，"
                  "腦中有架構但用詞含糊，講到一半會卡住換個說法再接下去。"
                  "每個刪節號是卡住重來，停一下就接，不要拖長音。"
                  "口氣是認真想講清楚，不是害羞。"),
        "voices": ["Puck", "Fenrir", "Orus", "Iapetus", "Sadachbia", "Achird"],
    },
    "xiaofei": {
        "label": "小妃（段落 M 上台解說）",
        "line": "一度水是 9 元，一天用 0.12 度水。所以乘起來……就是一天省下的錢！",
        "style": ("用國小五年級女生的聲音說這句話。站在黑板前面對全班解說，活潑、有精神，"
                  "敢講但不太精準，講到後面自己想通了，語氣變得有點得意。"
                  "刪節號停半秒。不要唸稿腔。"),
        "voices": ["Leda", "Aoede", "Laomedeia", "Autonoe", "Callirrhoe", "Pulcherrima"],
    },
}


def synth(model, voice, style, text, out_wav, tries=3):
    """API 偶爾回傳沒有 audio part（實測 Kore 兩次皆失敗、Puck 第一次失敗），故重試。"""
    key = os.environ["GEMINI_API_KEY"]
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "%s:generateContent?key=%s" % (model, key))
    body = {
        "contents": [{"parts": [{"text": style + "\n\n" + text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }
    last = None
    for n in range(tries):
        try:
            req = urllib.request.Request(url, json.dumps(body).encode(),
                                         {"Content-Type": "application/json",
                                          "User-Agent": "slc-tts/0.1"})
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.load(r)
            part = d["candidates"][0]["content"]["parts"][0]
            pcm = base64.b64decode(part["inlineData"]["data"])
            rate = 24000
            for kv in part["inlineData"].get("mimeType", "").split(";"):
                if kv.strip().startswith("rate="):
                    rate = int(kv.split("=")[1])
            with wave.open(out_wav, "wb") as w:   # 回傳是裸 PCM，要自己包 wav 標頭
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
                w.writeframes(pcm)
            return len(pcm) / (rate * 2), n + 1
        except Exception as e:
            last = e
            time.sleep(1.5 * (n + 1))
    raise RuntimeError("重試 %d 次仍失敗：%s" % (tries, last))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--who", choices=list(ROLES))
    ap.add_argument("--voice")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    for who in ([a.who] if a.who else list(ROLES)):
        r = ROLES[who]
        print("\n── %s ──\n台詞：%s" % (r["label"], r["line"]))
        for v in ([a.voice] if a.voice else r["voices"]):
            wav = os.path.join(OUT, "%s_%s.wav" % (who, v))
            mp3 = wav.replace(".wav", ".mp3")
            try:
                dur, n = synth(a.model, v, r["style"], r["line"], wav)
            except Exception as e:
                print("  %-12s ✗ %s" % (v, e)); continue
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-i", wav, "-b:a", "128k", mp3], check=True)
            os.remove(wav)
            print("  %-12s ✅ %.1f 秒%s" % (v, dur, "（重試 %d 次）" % (n - 1) if n > 1 else ""))


if __name__ == "__main__":
    main()
