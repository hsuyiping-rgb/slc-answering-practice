# -*- coding: utf-8 -*-
"""判定結果 → 分支的對映，以及 Whisper 幻覺偵測。

**這是唯一的真相來源。** `main.py`（線上服務）與 `judge/run_eval.py`（驗收測試集）
都從這裡匯入，確保「測過的」與「部署的」是同一套邏輯——否則改了一邊、
另一邊悄悄走鐘，14/14 的驗收結果就失去意義。

判定 prompt 同理：共通判準在 `functions/prompt.md`，各卡情境在 `functions/cards/<id>.md`
（deploy 只上傳 functions/ 目錄，所以真相來源必須住在這裡）。加卡＝在 cards/ 放一組 .json＋.md。
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_CARDS_DIR = os.path.join(_HERE, "cards")

# 每張卡一組：cards/<id>.json 的分支對映＋cards/<id>.md 的情境段。
# 分支對映**在程式裡決定，不交給 LLM**：分支是設計決定，必須可預測、可測試（規格 §4.3）。
# 冷啟動時讀一次；卡片 id 只能是這裡讀到的，前端送來的 id 不會被拿去拼路徑。
CARDS = {}
for _f in sorted(os.listdir(_CARDS_DIR)):
    if _f.endswith(".json"):
        _id = _f[:-5]
        with open(os.path.join(_CARDS_DIR, _f), encoding="utf-8") as _fh:
            CARDS[_id] = json.load(_fh)
        with open(os.path.join(_CARDS_DIR, _id + ".md"), encoding="utf-8") as _fh:
            CARDS[_id]["situation"] = _fh.read().strip()

with open(os.path.join(_HERE, "prompt.md"), encoding="utf-8") as _fh:
    _BASE_PROMPT = _fh.read()

DEFAULT_CARD = "c01"
BRANCH = CARDS[DEFAULT_CARD]["branch"]      # 舊介面相容：judge/ 的腳本以 C01 為預設
MIN_CONFIDENCE = 0.5


def build_prompt(card=DEFAULT_CARD):
    """共通判準（prompt.md）＋該卡情境（cards/<id>.md）。"""
    return _BASE_PROMPT.replace("{{CARD}}", CARDS[card]["situation"])


# Whisper 對安靜音檔常吐的固定句型。第二道防線——第一道是前端的靜音閘門
# 子字串比對：老師在課堂上不會講出這些字
HALLUCINATION = ("多謝您的收看", "謝謝您的收看", "請訂閱", "打賞支持", "字幕由",
                 "感謝觀看", "多谢您的收看", "请订阅",
                 "訂閱", "订阅", "按讚", "按赞", "點讚", "点赞")
# 整句比對：老師可能真的講這幾個字，只有「整段只有這句」才算幻覺。
# 2026-09-26 以合成的底噪／點擊聲／喇叭尾音實測，Whisper 在靜默上最常吐的就是這兩句
HALLUCINATION_EXACT = ("響鐘", "响钟", "謝謝大家", "谢谢大家")

# 每一段的 no_speech_prob 都 ≥ 這個值才當成沒講話。實測（judge/README.md 2026-09-26 段）：
# 老師講話 0.006–0.113（含「嗯？」「水費？」單字短句、很小聲、先等 9 秒才開口），
# 靜默＋干擾 0.106–0.53。兩組有重疊，所以門檻取在講話組上方、剩下的交給幻覺清單
NO_SPEECH_MIN = 0.2


def to_branch(strategy, confidence, flags, card=DEFAULT_CARD):
    """規格 §4.3：分支對映。

    `gives_answer` 與低信心都在程式裡強制改道，不依賴模型自律。
    低信心時走 RETRY，不硬塞一條分支給老師。
    """
    branch = CARDS[card]["branch"]
    if (flags or {}).get("gives_answer"):
        return branch["代答"]
    if confidence is not None and confidence < MIN_CONFIDENCE:
        return "RETRY"
    return branch.get(strategy, "RETRY")


def looks_hallucinated(text):
    """Whisper 幻覺偵測。前端閘門攔不住的漏網之魚（例如極輕的環境聲）走這道。"""
    t = (text or "").strip()
    bare = "".join(ch for ch in t if ch.isalnum())
    return bool(t) and (any(h in t for h in HALLUCINATION) or bare in HALLUCINATION_EXACT)


def looks_silent(segments):
    """Whisper verbose_json 的 segments 全都判定「沒有人聲」。

    前端閘門在喇叭外放時常被尾音或按停止鍵的點擊聲觸發（2026-09-26 真麥克風測 C02–C05，
    四個靜默輪全走 RETRY），這道把它們接回 WAIT。
    """
    probs = [s.get("no_speech_prob") for s in (segments or [])]
    probs = [p for p in probs if p is not None]
    return bool(probs) and min(probs) >= NO_SPEECH_MIN


def evidence_ok(span, source):
    """`evidence_span` 必須是老師原話中一字不差的連續片段。

    最便宜的抗幻覺檢查：模型若編造依據，這裡就會被擋下。規格 §4.2 要求不符時重試一次。
    """
    return bool(span) and span in (source or "")
