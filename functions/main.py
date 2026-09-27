# -*- coding: utf-8 -*-
"""理答練功的兩支後端代理（Firebase Cloud Functions, Python 3.12）

    answering_transcribe   音訊 → Groq Whisper → 文字
    answering_judge        老師原話 → Gemini → 六策略判定 JSON ＋ 分支

**紅線（agents.md）**
- API key 一律走後端。金鑰存 Secret Manager，不進版控、不進前端。
- 錄音絕不落地儲存：音訊只在記憶體裡待到轉完文字，不寫檔、不進 Storage。
- 不記錄逐字內容：log 只寫長度與判定結果，不寫老師說了什麼
  （規格 §5：localStorage 只存判定結果，不存逐字文字；後端沒有理由比前端存更多）。

部署（需先開通 Blaze）：
    firebase deploy --only functions:answering
    # codebase 具名為 answering，不會動到同專案裡其他 repo 的函式

金鑰設定（一次性）：
    firebase functions:secrets:set GROQ_API_KEY
    firebase functions:secrets:set GEMINI_API_KEY
"""
import json
import os
import urllib.error
import urllib.request
import uuid

from firebase_functions import https_fn, options
from firebase_functions.params import SecretParam
from opencc import OpenCC

from strategy import CARDS, DEFAULT_CARD, build_prompt, to_branch, looks_hallucinated, looks_silent, evidence_ok

GROQ_API_KEY = SecretParam("GROQ_API_KEY")
GEMINI_API_KEY = SecretParam("GEMINI_API_KEY")

JUDGE_MODEL = "gemini-3.7-flash"      # judge/README.md：3 次重複 14/14 全過；3.5-flash 第 13 題不穩
WHISPER_MODEL = "whisper-large-v3"

# Whisper 帶 language=zh 仍常吐簡體（「学习单」「再说一次」）。回傳前過一次簡轉繁，
# 不改用 prompt 引導——提示詞會讓它順稿（judge/README.md 發現二）。
# s2twp：簡→台灣正體＋台灣用語（软件→軟體）。純 Python 實作，雲端不需編譯
S2TW = OpenCC("s2twp")

MAX_AUDIO_BYTES = 8 * 1024 * 1024     # 約 8 分鐘 opus。單回合用不到，純粹擋濫用
HTTP_TIMEOUT = 90

# Groq 走 Cloudflare，預設的 urllib／requests UA 會被擋成 403 error code 1010
UA = "slc-answering/0.1"

# 判定 prompt 冷啟動時組好：共通判準 prompt.md ＋ 各卡情境 cards/<id>.md（strategy.py 讀檔，
# 路徑相對 __file__——雲端的 CWD 剛好是函式目錄，本機不是）
JUDGE_PROMPTS = {c: build_prompt(c) for c in CARDS}

CORS = options.CorsOptions(
    cors_origins=[
        r"https://slc-answering\.web\.app",
        r"https://slc-answering\.firebaseapp\.com",
        r"http://localhost:\d+",       # 本機 emulator
    ],
    cors_methods=["POST", "OPTIONS"],
)


def _multipart(blob, filename, mime, fields):
    """手刻 multipart。用 urllib 而非 requests：全專案一致，且 requests 只認 certifi 的
    憑證包，在有 TLS 攔截的機器上本機測不了（urllib 會讀 Windows 憑證存放區）。"""
    b = uuid.uuid4().hex
    sep = "\r\n"
    out = []
    for k, v in fields.items():
        head = '--%s%sContent-Disposition: form-data; name="%s"%s%s%s' % (
            b, sep, k, sep, sep, v)
        out.append((head + sep).encode())
    head = ('--%s%sContent-Disposition: form-data; name="file"; filename="%s"%s'
            'Content-Type: %s%s%s' % (b, sep, filename, sep, mime, sep, sep))
    out.append(head.encode())
    out.append(blob)
    out.append((sep + "--" + b + "--" + sep).encode())
    return b"".join(out), "multipart/form-data; boundary=" + b


def _err(msg, code=400):
    return https_fn.Response(json.dumps({"error": msg}, ensure_ascii=False),
                             status=code, mimetype="application/json")


def _ok(payload):
    return https_fn.Response(json.dumps(payload, ensure_ascii=False),
                             mimetype="application/json")


# ------------------------------------------------------------------ 語音辨識
@https_fn.on_request(cors=CORS, secrets=[GROQ_API_KEY], memory=options.MemoryOption.MB_512)
def answering_transcribe(req: https_fn.Request) -> https_fn.Response:
    """音訊 → 文字。

    前端在送出之前就跑過靜音閘門（起伏比 < 2.0 直接判 WAIT，錄音不離開裝置），
    所以這裡收到的原則上都有語音。`looks_hallucinated` 是第二道防線。
    """
    if req.method != "POST":
        return _err("只接受 POST", 405)

    audio = req.get_data()                      # bytes，只在記憶體，不落地
    if not audio:
        return _err("沒有音訊內容")
    if len(audio) > MAX_AUDIO_BYTES:
        return _err("音訊過大", 413)

    mime = req.headers.get("Content-Type", "audio/webm")
    # Safari 的 MediaRecorder 只出 audio/mp4，Groq 也吃 m4a；其餘一律當 ogg
    ext = ("webm" if "webm" in mime else "wav" if "wav" in mime
           else "m4a" if ("mp4" in mime or "m4a" in mime) else "ogg")

    try:
        # 一律不帶 prompt：提示詞會讓 Whisper「順稿」，把重複與口吃磨平，
        # 而那些正是理答判定的依據（judge/README.md 發現二）
        body, ctype = _multipart(audio, "a." + ext, mime,
                                 {"model": WHISPER_MODEL, "language": "zh",
                                  "response_format": "verbose_json", "temperature": "0"})
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/audio/transcriptions", body,
            {"Authorization": "Bearer " + GROQ_API_KEY.value,
             "Content-Type": ctype, "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            res = json.load(r)
        text = (res.get("text") or "").strip()
        segments = res.get("segments")
    except urllib.error.HTTPError as e:
        return _err("辨識服務回應 %s" % e.code, 502)
    except Exception:
        return _err("辨識服務無法連線", 502)
    finally:
        del audio                                # 轉完就丟，不留在記憶體等 GC

    # 幻覺檢查用 Whisper 原文：s2twp 會改台灣用語（打賞支持→打賞支援），轉完再比對會漏
    if looks_hallucinated(text):
        # 安靜音檔的固定幻覺句型。當成沒有語音處理，交給 WAIT 分支
        return _ok({"text": "", "silent": True, "reason": "hallucination"})
    if looks_silent(segments):
        # 閘門被尾音或點擊聲觸發，但 Whisper 認為整段沒有人聲
        return _ok({"text": "", "silent": True, "reason": "no_speech"})

    text = S2TW.convert(text)
    return _ok({"text": text, "silent": not text})


# ------------------------------------------------------------------ 策略判定
def _call_gemini(system, user):
    # 金鑰走 header，**不放 query string**：網址會出現在例外訊息與 Cloud Logging 的
    # 堆疊裡，key 放進去等於自己把它寫進日誌，與「金鑰一律後端」的用意抵觸
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "%s:generateContent" % JUDGE_MODEL)
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    req = urllib.request.Request(url, json.dumps(body).encode(),
                                 {"Content-Type": "application/json",
                                  "User-Agent": UA,
                                  "x-goog-api-key": GEMINI_API_KEY.value})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        d = json.load(r)
    return json.loads(d["candidates"][0]["content"]["parts"][0]["text"])


@https_fn.on_request(cors=CORS, secrets=[GEMINI_API_KEY], memory=options.MemoryOption.MB_256)
def answering_judge(req: https_fn.Request) -> https_fn.Response:
    """老師原話 → 六策略判定 ＋ 分支。

    `branch_id` 由程式推導，**不由 LLM 輸出**（規格 §4.3）。
    `evidence_span` 必須是原話子字串，不符則重試一次（規格 §4.2）。
    """
    if req.method != "POST":
        return _err("只接受 POST", 405)

    body = req.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    card = body.get("card") or DEFAULT_CARD      # 舊版前端不帶 card，視為 C01
    if card not in CARDS:
        return _err("沒有這張卡")
    if not text:
        # 全程無語音由前端直接判 WAIT，不該打到這裡；防呆用
        return _ok({"strategy": "等待", "branch_id": "WAIT", "confidence": 1.0})
    if len(text) > 2000:
        return _err("輸入過長")

    got, span_ok = None, False
    for _ in range(2):                           # 子字串驗證不過就重試一次
        try:
            got = _call_gemini(JUDGE_PROMPTS[card], text)
        except Exception:
            return _err("判定服務無法連線", 502)
        span_ok = evidence_ok(got.get("evidence_span"), text)
        if span_ok:
            break

    flags = got.get("flags") or {}
    confidence = got.get("confidence")
    branch = to_branch(got.get("strategy"), confidence, flags, card)
    if not span_ok:
        # 兩次都編造依據＝模型在幻覺，不要拿這個結果去驅動分支
        branch = "RETRY"

    return _ok({
        "strategy": got.get("strategy"),
        "secondary": got.get("secondary"),
        "confidence": confidence,
        "evidence_span": got.get("evidence_span") if span_ok else None,
        "addressee": got.get("addressee"),
        "flags": flags,
        "recognition_note": got.get("recognition_note"),
        "branch_id": branch,
        "card": card,
        "evidence_verified": span_ok,
    })
