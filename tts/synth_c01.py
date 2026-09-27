# -*- coding: utf-8 -*-
"""C01〈聽不懂的那句話〉七段語音合成

音色 2026-09-03 定案（使用者試聽 tts/samples/ 後選定）：
  小宇 = Zephyr、小靚 = Despina
  2026-09-26 補：小賢 = Achird、小妃 = Laomedeia（試聽 tts/samples/試聽_小賢小妃.html）

    python tts/synth_c01.py                # 全部七段
    python tts/synth_c01.py --only b5      # 只重合成某一段

輸出：public/audio/c01/*.mp3（規格 §5 的檔名）
"""
import argparse, base64, json, os, subprocess, sys, io, time, urllib.request, wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "public", "audio", "c01")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

MODEL = "gemini-3.1-flash-tts-preview"
VOICE = {"xiaoyu": "Zephyr", "xiaoliang": "Despina", "xiaoxian": "Achird", "xiaofei": "Laomedeia"}

# 台詞逐字取自 規格_情境卡C01_聽不懂的那句話.md §2.2、§3、§4.6。
# 不流暢寫進文本本身，不靠 TTS 演（Q7）；括號的動作提示只進 style，不進台詞。
BASE = {
    "xiaoyu": "用國小五年級男生的聲音說這句話。聲音偏小、有點怯。不要唸稿腔。",
    "xiaoliang": "用國小五年級女生的聲音說這句話。課堂上自然的說話，不要唸稿腔。",
    "xiaoxian": "用國小五年級男生的聲音說這句話。中等音量、語速偏快，認真想講清楚。不要唸稿腔。",
    "xiaofei": "用國小五年級女生的聲音說這句話。活潑、有精神，敢講。不要唸稿腔。",
}
# 刪節號的停頓長度逐段指定：B2 若照一秒走會拖到 12 秒，遠超規格的 7 秒
PAUSE_1S = "每個刪節號要真的停住大約一秒，不是拖長音。"

SEGS = [
    ("intro_yu", "xiaoyu", "開場發話（§2.2）",
     "就是……那個……嗯……要怎麼把它變成分析……就前面三個……",
     PAUSE_1S + "講到一半才發現自己講不清楚，語速越來越慢，尾音往下掉。"
     "最後一句像是講不下去而停掉，不是講完。", 0),

    ("b1_yu", "xiaoyu", "B1 釐清",
     "就是……前面三個……要先寫那個……條件……然後才可以算。",
     PAUSE_1S + "比第一次稍微大聲一點，仍然有停頓。「條件」兩個字講得比較確定。", 0),

    ("b2_yu", "xiaoyu", "B2 深究",
     "……條件語句、然後……可以推論的訊息、然後……解題邏輯。",
     "刪節號只是換氣，各停約三分之一秒，不要拖。語速接近正常說話，"
     "念到三個欄位名稱時轉穩，像是低頭照著紙上一口氣念完三個。整句控制在七秒以內。", 0),

    ("b3_liang", "xiaoliang", "B3 轉引（小靚）",
     "就是先找出題目給的條件，然後從條件去想可以知道什麼，最後才算。",
     "表達清楚、有條理，幾乎沒有停頓，像是心裡已經想好了才開口。"
     "與一個講話結巴、聲音很小的同學形成明顯對比。", 0),

    ("b4_yu", "xiaoyu", "B4 提示",
     "條件語句。",
     "短、乾脆、音量正常，沒有停頓。這是照著紙上念出來的，不是想出來的。", 0),

    # §3 B5：講完「對」之後接 3 秒全班安靜，靜音在後製補（見 PAD）
    ("b5_yu", "xiaoyu", "B5 代答",
     "……對。",
     PAUSE_1S + "極小聲，只有一個字，尾音往下掉。像是被說中了而不再多說。", 3.0),

    ("wait_yu", "xiaoyu", "WAIT 無語音（§4.6）",
     "……就是……先看題目給了什麼……",
     "刪節號要停住一秒半，比開場更小聲、更慢。"
     "像是自己在跟自己確認，不是說給別人聽。", 0),
]


# 修頭尾空白。舊版 start_duration=0.15 會把「句尾單獨一個字」當雜音整個切掉
# （2026-09-26 C06 B1：「……換成」停 0.1 秒後單獨的「度」只有 0.16 秒，被整段刪除；
# 使用者試聽反映收尾被截斷）。現在一碰到聲音就停，句尾另外保留 0.35 秒自然收尾。
def _sr(keep):
    return ("silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB:"
            "start_silence=%s:detection=peak" % keep)


TRIM = _sr(0.05) + ",areverse," + _sr(0.35) + ",areverse"

# 2026-09-26 使用者試聽 C06 後定案：孩子講慢一點、內容一字不漏。所有卡共用
TEMPO = 0.88
SLOW = "語速比平常慢一點，每個字都要講清楚，台詞一個字都不能省略或改動。"


def synth(voice, style, text, out_wav, tries=3):
    """API 偶爾回傳沒有 audio part，故重試（見 judge/README.md 的坑）。"""
    key = os.environ["GEMINI_API_KEY"]
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "%s:generateContent?key=%s" % (MODEL, key))
    body = {
        # 指示與台詞要明確分開：2026-09-26 C06 B1 的指示變長後，模型把整段指示也念了出來（52 秒）
        "contents": [{"parts": [{"text": "【朗讀指示，不要念出來】\n" + style +
                                          "\n\n【只念出下面這一段台詞，一字不漏】\n" + text}]}],
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


def render(segs, out, only=None, tempo=TEMPO):
    """逐段合成 → 修頭尾空白 → mp3。各卡的腳本共用這一段（synth_c04.py 匯入）。

    tempo < 1 用 atempo 放慢（音高不變）。2026-09-26 使用者試聽 C06 後要求孩子講慢一點，
    光靠風格指令放慢不穩定，所以在後製統一放慢。
    """
    os.makedirs(out, exist_ok=True)
    for name, who, label, line, extra, pad in segs:
        if only and only.lower() not in name:
            continue
        wav = os.path.join(out, name + ".wav")
        mp3 = os.path.join(out, name + ".mp3")
        try:
            dur, n = synth(VOICE[who], BASE[who] + SLOW + extra, line, wav)
        except Exception as e:
            print("  %-11s ✗ %s" % (name, e)); continue
        # 頭尾的空白是模型的起手／收尾，不是設計出來的停頓（B2 實測頭 0.5 秒、尾 0.4 秒），
        # 播放時只會變成無意義的延遲，修到頭留 0.05 秒、尾留 0.35 秒。段落內部的停頓不動。
        af = [TRIM]
        if tempo != 1.0:
            af.append("atempo=%s" % tempo)
        if pad:
            af.append("apad=pad_dur=%s" % pad)        # 補靜音，不是延長尾音
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", wav,
               "-af", ",".join(af), "-b:a", "128k", mp3]
        subprocess.run(cmd, check=True)
        os.remove(wav)
        final = subprocess.run(   # 報修剪後的實際長度，不是原始 PCM 長度
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", mp3], capture_output=True, text=True).stdout.strip()
        print("  %-11s ✅ %-16s %.1f 秒（原始 %.1f）%s%s" % (
            name, label, float(final or 0), dur,
            "（含 %.0f 秒靜音）" % pad if pad else "",
            "（重試 %d 次）" % (n - 1) if n > 1 else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="只合成這一段（檔名去掉副檔名，如 b5_yu 或 b5）")
    a = ap.parse_args()
    render(SEGS, OUT, a.only)


if __name__ == "__main__":
    main()
