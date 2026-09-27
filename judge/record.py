# -*- coding: utf-8 -*-
"""錄音助手：照著清單逐句錄，直接存成 R01.wav…R17.wav

    python judge/record.py              # 只錄還沒錄過的
    python judge/record.py --redo R04   # 重錄某幾句
    python judge/record.py --list       # 只看清單不錄

操作：按 Enter 開始錄 → 說話 → 再按 Enter 停止。
每段會自動在前後各補 1 秒安靜（實測顯示這能讓短句的 CER 大幅下降）。
"""
import argparse, json, os, subprocess, sys, io

HERE = os.path.dirname(os.path.abspath(__file__))
REC = os.path.join(HERE, "recordings")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

TIPS = {
    "R01": "平淡，不是質問",
    "R02": "尾音稍微上揚",
    "R03": "短促",
    "R04": "真誠好奇，不是責問",
    "R05": "",
    "R06": "尾音真的拖長——這是講到一半的未完成句",
    "R07": "兩處停頓各約 1 秒，真的停住，不要用「嗯——」拖過去",
    "R08": "示弱，帶點驚訝",
    "R09": "喊停的語氣",
    "R10": "", "R11": "", "R12": "", "R13": "",
    "R14": "整句一氣呵成，「我覺得很清楚」不要刻意加重",
    "R15": "",
    "R16": "「水費」真的說兩次，中間停半拍",
    "R17": "什麼都不要說，就讓它錄 15 秒安靜",
}


def default_device():
    out = subprocess.run(["ffmpeg", "-hide_banner", "-list_devices", "true",
                          "-f", "dshow", "-i", "dummy"],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace").stderr
    for line in out.splitlines():
        if "(audio)" in line and '"' in line:
            return line.split('"')[1]
    return None


def record(device, out_path, fixed_sec=None):
    """錄到使用者按 Enter（或固定秒數）。前後各補 1 秒安靜。"""
    raw = out_path + ".raw.wav"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "dshow", "-i", "audio=" + device]
    if fixed_sec:
        cmd += ["-t", str(fixed_sec)]
    cmd += ["-ac", "1", "-ar", "16000", raw]

    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    if fixed_sec:
        print("   ● 錄音中…%d 秒後自動停止" % fixed_sec)
        p.wait()
    else:
        print("   ● 錄音中…說完後按 Enter 停止")
        try:
            input()
        except EOFError:
            pass
        try:
            p.communicate(b"q", timeout=5)
        except Exception:
            p.terminate()

    # 前後各補 1 秒安靜（產品端不修剪靜音，這裡模擬按鈕前後的自然留白）
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", raw, "-af", "adelay=1000|1000,apad=pad_dur=1",
                    "-ac", "1", "-ar", "16000", out_path], check=True)
    os.remove(raw)


def existing(rid):
    for e in (".wav", ".mp3", ".m4a"):
        p = os.path.join(REC, rid + e)
        if os.path.exists(p):
            return p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device")
    ap.add_argument("--redo", nargs="*", default=None, help="要重錄的編號，例如 --redo R04 R07")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    items = json.load(open(os.path.join(REC, "expected.json"), encoding="utf-8"))["items"]

    if a.list:
        for it in items:
            mark = "✅" if existing(it["id"]) else "  "
            print("%s %s  %s" % (mark, it["id"], it["ref"] or "（15 秒安靜）"))
        return

    device = a.device or default_device()
    if not device:
        print("找不到錄音裝置。用 --device \"裝置名稱\" 指定。"); return
    print("錄音裝置：%s" % device)
    print("存到　　：%s\n" % REC)

    keep = (lambda it: it["id"] in a.redo) if a.redo else (lambda it: not existing(it["id"]))
    todo = [it for it in items if keep(it)]
    if not todo:
        print("清單已經錄完了。要重錄某幾句用 --redo R04 R07"); return

    print("共 %d 句。每句：按 Enter 開始 → 說 → 按 Enter 停止。\n" % len(todo))
    group = None
    for it in todo:
        if it["group"] != group:
            group = it["group"]
            print("\n── %s " % group + "─" * 46)
        print("\n%s  「%s」" % (it["id"], it["ref"] or "（不要說話）"))
        if TIPS.get(it["id"]):
            print("     語氣：%s" % TIPS[it["id"]])
        try:
            input("     按 Enter 開始錄音（或 Ctrl+C 中止）")
        except (EOFError, KeyboardInterrupt):
            print("\n中止。已錄好的都保留了。"); return
        out = os.path.join(REC, it["id"] + ".wav")
        record(device, out, fixed_sec=15 if it.get("silence") else None)
        print("   ✅ 已存 %s.wav" % it["id"])

    print("\n全部錄完。接著跑：python judge/run_mic.py")


if __name__ == "__main__":
    main()
