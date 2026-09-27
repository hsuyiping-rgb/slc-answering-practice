# -*- coding: utf-8 -*-
"""整張卡重新合成＋逐段驗證內容：合成 → Whisper 轉回文字 → 與卡片台詞比對，對不上就重試

TTS 偶爾會吞字、多字或把指示念出來（見 check_audio.py），光靠耳朵很難一句一句抓。
這支每段合成完立刻檢查，CER 超過門檻就重合成，最多 --tries 次，保留 CER 最低的一版。

    python tts/rebuild.py --card c06
    python tts/rebuild.py --card c01 c02 c03 c04 c05 c06
    python tts/rebuild.py --card c05 --only b1

結果另寫一份到 tts/rebuild_<日期>.log（tts/ 下的 log 不進版控）。
"""
import argparse, datetime, importlib, io, json, os, shutil, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import check_audio as C                   # noqa: E402
_keep = sys.stdout                         # 兩個模組都會重包 stdout；留住參照，免得被回收時把串流關掉
import synth_c01                           # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", nargs="+", required=True)
    ap.add_argument("--only")
    ap.add_argument("--tries", type=int, default=4)
    ap.add_argument("--accept", type=float, default=0.12, help="CER 不超過這個值就收（同音字差異難免）")
    ap.add_argument("--backoff", type=float, default=30, help="合成失敗後等幾秒再試")
    a = ap.parse_args()
    key = C.groq_key()
    log = open(os.path.join(HERE, "rebuild_%s.log" % datetime.date.today()), "a", encoding="utf-8")

    def say(msg):
        print(msg, flush=True); log.write(msg + "\n"); log.flush()

    for cid in a.card:
        mod = importlib.import_module("synth_" + cid)
        card = json.load(open(os.path.join(ROOT, "public", "cards", cid + ".json"), encoding="utf-8"))
        text = {card["opening"]["audio"]: card["opening"]["text"], card["wait"]["audio"]: card["wait"]["text"]}
        text.update({b["audio"]: b["text"] for b in card["branches"]})
        say("\n══ %s〈%s〉══" % (cid, card["title"]))
        for seg in mod.SEGS:
            name = seg[0]
            if a.only and a.only.lower() not in name:
                continue
            mp3 = os.path.join(mod.OUT, name + ".mp3")
            if name + ".mp3" not in text:
                say("  %-11s ⚠️ 卡片裡沒有這個音檔，跳過" % name); continue
            ref = C.norm(text[name + ".mp3"])
            best = None
            # 舊檔先移開：合成失敗（429、回傳沒有音訊）時 render 只印 ✗、不產生檔案，
            # 若不移開，接下來會拿舊檔去比對、把舊語音當成新的（2026-09-26 第一次整批重建踩到）
            old = mp3 + ".old"
            if os.path.exists(mp3):
                shutil.move(mp3, old)
            for n in range(a.tries):
                synth_c01.render([seg], mod.OUT)
                if not os.path.exists(mp3):
                    time.sleep(a.backoff)       # 限流時連打只會一直 429
                    continue
                hyp = C.whisper(mp3, key)
                e, _ = C.cer(ref, C.norm(hyp))
                if best is None or e < best[0]:
                    best = (e, n + 1, C.S2TW.convert(hyp)); shutil.move(mp3, mp3 + ".best")
                else:
                    os.remove(mp3)
                if e <= a.accept:
                    break
                time.sleep(2)
            if best is None:
                if os.path.exists(old):
                    shutil.move(old, mp3)
                say("  %-11s ✗ 沒有重建成功，保留舊檔" % name); continue
            shutil.move(mp3 + ".best", mp3)
            if os.path.exists(old):
                os.remove(old)
            say("  %-11s %s CER %.2f（第 %d 次）聽到：%s" % (name, "✅" if best[0] <= a.accept else "⚠️",
                                                      best[0], best[1], best[2]))


if __name__ == "__main__":
    main()
