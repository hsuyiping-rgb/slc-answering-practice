# -*- coding: utf-8 -*-
"""把某張卡的學生語音組成一頁試聽表（音檔內嵌，可離線聽、可寄給人）

    python tts/make_preview.py              # C01
    python tts/make_preview.py --card c04

台詞與分支直接讀 public/cards/<id>.json，不另外抄一份。
產出 public/audio/<id>/試聽.html。這頁是衍生物（約 1MB 的 base64，等於把 mp3 存兩份），
故不進版控——換電腦時重跑這支腳本即可。
"""
import argparse, base64, json, os, subprocess, io, sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOICE = {"小宇": "Zephyr", "小靚": "Despina", "小賢": "Achird", "小妃": "Laomedeia"}     # 與 synth_c01.py 的定案一致


def segments(card):
    """(檔名, 標籤, 說話者, 台詞) 依卡片順序：開場 → 各分支 → 等待。"""
    o = card["opening"]
    yield o["audio"], "開場", o["speaker"], o["text"]
    for b in card["branches"]:
        yield b["audio"], "%s %s" % (b["id"], b["strategy"]), b["speaker"], b["text"]
    w = card["wait"]
    yield w["audio"], "WAIT", w["speaker"], w["text"]


CSS = """
:root{--bg:#faf9f7;--fg:#1c1b19;--mut:#6b6863;--card:#fff;--line:#e5e2dc;--accent:#3f6f52}
@media (prefers-color-scheme:dark){:root{--bg:#17181a;--fg:#e9e7e3;--mut:#9a968f;--card:#1f2124;--line:#33363a;--accent:#8fbfa2}}
*{box-sizing:border-box}
body{margin:0;padding:28px 20px 60px;background:var(--bg);color:var(--fg);
 font-family:"Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,sans-serif;line-height:1.7}
.wrap{max-width:720px;margin:0 auto}
h1{font-size:1.5rem;margin:0 0 4px}
.sub{color:var(--mut);font-size:.9rem;margin:0 0 28px}
.row{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:12px}
.head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.tag{font-weight:600;color:#fff;background:var(--accent);padding:2px 10px;border-radius:99px;font-size:.8rem}
.voice{font-weight:600;font-size:.9rem}
.meta{color:var(--mut);font-size:.8rem;margin-left:auto}
.line{margin:0 0 10px;border-left:3px solid var(--line);padding-left:12px}
audio{width:100%;height:36px}
.fn{color:var(--mut);font-size:.75rem;font-family:ui-monospace,monospace;margin-top:6px}
"""


def dur(p):
    d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", p], capture_output=True, text=True).stdout.strip()
    return float(d) if d else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", default="c01")
    cid = ap.parse_args().card
    card = json.load(open(os.path.join(ROOT, "public", "cards", cid + ".json"), encoding="utf-8"))
    d = os.path.join(ROOT, "public", card["audio_base"])
    rows = []
    for fn, tag, who, line in segments(card):
        p = os.path.join(d, fn)
        if not os.path.exists(p):
            print("  略過（尚未合成）：%s" % fn); continue
        b64 = base64.b64encode(open(p, "rb").read()).decode()
        rows.append("""
  <div class="row">
    <div class="head"><span class="tag">%s</span><span class="voice">%s · %s</span>
      <span class="meta">%.1f 秒</span></div>
    <p class="line">「%s」</p>
    <audio controls preload="none" src="data:audio/mpeg;base64,%s"></audio>
    <div class="fn">%s</div>
  </div>""" % (tag, who, VOICE.get(who, "?"), dur(p), line.replace("\n", ""), b64, fn))

    title = "%s〈%s〉學生語音" % (cid.upper(), card["title"])
    html = ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>%s</title><style>%s</style></head><body><div class="wrap">'
            '<h1>%s</h1><p class="sub">%s · 輸出於 <code>public/%s</code></p>%s</div></body></html>'
            % (title, CSS, title, card["subject"], card["audio_base"], "".join(rows)))
    out = os.path.join(d, "試聽.html")
    open(out, "w", encoding="utf-8").write(html)
    print("已產生 %s（%.1f MB）" % (out, os.path.getsize(out) / 1048576))


if __name__ == "__main__":
    main()
