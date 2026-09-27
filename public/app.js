/* 理答練功前端：錄音 → 靜音閘門 → 後端代理（Whisper／判定）→ 兩層回饋 → localStorage
 *
 * 紅線（agents.md）
 * - 錄音絕不落地：音訊只存在記憶體裡的 Blob，送出（或判為靜音）後立刻丟掉。
 *   不寫 IndexedDB、不寫 localStorage、不下載。
 * - 前端沒有任何 API key；只打 config.js 裡的兩個代理端點。
 * - 選擇層（第二層）不出現分數與對錯字樣。後果文案來自 cards/<id>.json，載入時做一次字詞稽核。
 * - 錄音不因靜音自動結束（規格 §4.6）。等待秒數只顯示已經過的秒數，沒有倒數、沒有進度條。
 */
(function () {
  "use strict";
  var C = window.SLC_CONFIG;
  var $ = function (id) { return document.getElementById(id); };

  // 規格 §4.3.1／§8-6：選擇層禁語。載入卡片時掃一次，違反只在 console 警告，不擋畫面
  var BANNED = ["錯", "不好", "失敗", "應該", "蓋章"];
  // 靜音閘門（judge/run_e2e.py 移植）：幀級 RMS 的 P95／中位數。語音 >3，靜音 ~1，閾值 2.0
  var DYNAMIC_MIN = 2.0;
  var ONSET_RUN = 10;           // 連續 10 幀（約 200ms）高於門檻才算開口；3 幀（60ms）擋不住清喉嚨與滑鼠聲（2026-09-22 實測等 20 秒被記成 4 秒）

  var card = null;
  var preselect = null;         // 使用者事前自選的策略（可選，供第一層對照）
  var player = new Audio();

  // ------------------------------------------------------------ 小工具
  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function md(s) {                       // 只支援 **粗體**
    return esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  }
  function show(id, on) { $(id).hidden = !on; }
  function play(file) {
    return new Promise(function (res) {
      player.onended = player.onerror = function () { res(); };
      // mp3 是一年 immutable 快取、重合成時檔名不變 → 換語音就改卡片的 audio_rev（卡片 JSON 是 no-cache）
      player.src = card.audio_base + file + (card.audio_rev ? "?v=" + card.audio_rev : "");
      player.play().catch(res);
    });
  }
  function stopPlayer() { player.pause(); player.onended = null; }

  // ------------------------------------------------------------ 載入卡片
  // ?card=c04 選卡。id 只接受 cards/index.json 列出的，不拿網址參數去拼任意路徑
  var want = (location.search.match(/[?&]card=([a-z0-9]+)/) || [])[1] || "c01";
  fetch("cards/index.json", { cache: "no-cache" }).then(function (r) { return r.json(); })
    .then(function (idx) {
      var ok = idx.cards.some(function (x) { return x.id === want; });
      buildPicker(idx.cards, ok ? want : "c01");
      return fetch("cards/" + (ok ? want : "c01") + ".json", { cache: "no-cache" });
    })
    .then(function (r) { return r.json(); }).then(init)
    .catch(function (e) { $("introText").textContent = "卡片載入失敗：" + e; });

  function buildPicker(list, cur) {
    var nav = $("cardPicker");
    list.forEach(function (x) {
      var a = document.createElement("a");
      a.className = "chip" + (x.id === cur ? " on" : "");
      // 保留 ?mock=1 之類的其他參數，只換 card
      var q = location.search.replace(/[?&]card=[a-z0-9]+/, "").replace(/^&/, "?");
      a.href = (q ? q + "&" : "?") + "card=" + x.id;
      a.textContent = x.title + "｜" + x.focus;
      if (x.id === cur) a.setAttribute("aria-current", "page");
      nav.appendChild(a);
    });
  }

  function init(c) {
    card = c;
    document.title = "理答練功｜" + c.title;
    $("cardSubject").textContent = c.subject;
    $("cardTitle").textContent = c.title;
    $("cardSource").textContent = c.source;
    $("introText").innerHTML = c.intro_text.map(function (p) { return "<p>" + md(p) + "</p>"; }).join("");
    $("openingSpeaker").textContent = c.opening.speaker;
    $("startHint").textContent = "按下後" + c.opening.speaker + "會開口，說完之後就換你了。";
    $("openingText").textContent = c.opening.text;
    $("redline").textContent = c.redline;
    $("closing").textContent = c.closing || "";
    audit(c);
    buildPreselect();
    buildMock();
    renderLog();

    $("btnStart").onclick = startRound;
    $("btnStop").onclick = stopRecording;
    $("btnAgain").onclick = reset;
    $("btnExport").onclick = exportLog;
    $("btnClear").onclick = function () {
      if (confirm("清除這台裝置上的練習紀錄？")) { localStorage.removeItem(C.storageKey); renderLog(); }
    };
    show("secMock", false);
  }

  function audit(c) {
    var texts = c.branches.map(function (b) { return [b.id, b.consequence]; });
    texts.push(["WAIT", c.wait.consequence]);
    texts.forEach(function (t) {
      BANNED.forEach(function (w) {
        if (t[1].indexOf(w) >= 0) console.warn("選擇層文案含禁語「" + w + "」：" + t[0]);
      });
    });
  }

  // 可選的事前自選策略：只影響第一層的對照文字，不影響判定
  function buildPreselect() {
    var box = document.createElement("div");
    box.innerHTML = '<p class="hint">（可選）這一回合你打算用哪個策略？選了之後，第一層會拿你的預想和實際說出口的比一比。</p>';
    var chips = document.createElement("div");
    chips.className = "chips";
    Object.keys(card.strategies).forEach(function (s) {
      var b = document.createElement("button");
      b.className = "chip"; b.type = "button"; b.textContent = s;
      b.onclick = function () {
        preselect = (preselect === s) ? null : s;
        Array.prototype.forEach.call(chips.children, function (x) { x.classList.toggle("on", x.textContent === preselect); });
      };
      chips.appendChild(b);
    });
    box.appendChild(chips);
    $("secIntro").insertBefore(box, $("secIntro").querySelector(".actions"));
  }

  // ------------------------------------------------------------ 一回合
  var stream, ctx, proc, rec, chunks, frames, frameDur, mime, t0, timer;

  function startRound() {
    $("micErr").hidden = true;
    // 先要麥克風權限，讓權限對話框不要吃掉等待時間
    navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: false, autoGainControl: false }
    }).then(function (s) {
      stream = s;
      // AudioContext 要在使用者手勢的呼叫鏈裡建立
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      $("openingPlaying").textContent = card.opening.speaker + "正在說話……";   // 每輪重設，播完會被清空
      show("secIntro", false); show("secResult", false); show("secOpening", true);
      return play(card.opening.audio);
    }).then(function () {
      if (!stream) return;
      $("openingPlaying").textContent = "";
      startRecording();
    }).catch(function (e) {
      $("micErr").textContent = "拿不到麥克風：" + (e && e.message || e) + "。請允許瀏覽器使用麥克風後再試一次。";
      $("micErr").hidden = false;
    });
  }

  function startRecording() {
    frames = []; chunks = [];
    var src = ctx.createMediaStreamSource(stream);
    proc = ctx.createScriptProcessor(1024, 1, 1);
    frameDur = 1024 / ctx.sampleRate;              // 48k → 約 21ms 一幀
    proc.onaudioprocess = function (e) {
      if (!frames) return;                         // 停止後 ctx.close() 是非同步的，可能還排著一次回呼
      var d = e.inputBuffer.getChannelData(0), s = 0;
      for (var i = 0; i < d.length; i++) s += d[i] * d[i];
      frames.push(Math.sqrt(s / d.length));
    };
    var mute = ctx.createGain(); mute.gain.value = 0;   // ScriptProcessor 要接到 destination 才會跑，但不要回放
    src.connect(proc); proc.connect(mute); mute.connect(ctx.destination);
    if (ctx.state === "suspended") ctx.resume();

    mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]
      .filter(function (m) { return window.MediaRecorder && MediaRecorder.isTypeSupported(m); })[0] || "";
    rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    rec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
    rec.start(250);

    t0 = performance.now();
    $("elapsed").textContent = "0";
    timer = setInterval(function () {
      $("elapsed").textContent = String(Math.floor((performance.now() - t0) / 1000));
    }, 200);
    show("secOpening", false); show("secTeacher", true);
  }

  function stopRecording() {
    clearInterval(timer);
    $("btnStop").disabled = true;
    var totalSec = (performance.now() - t0) / 1000;
    new Promise(function (res) { rec.onstop = res; rec.stop(); }).then(function () {
      var blob = new Blob(chunks, { type: mime || "audio/webm" });
      chunks = null;
      proc.disconnect(); ctx.close(); ctx = null;
      stream.getTracks().forEach(function (t) { t.stop(); }); stream = null;
      $("btnStop").disabled = false;
      show("secTeacher", false);

      var gate = analyze(frames, frameDur, totalSec);
      frames = null;
      if (gate.silent) {
        blob = null;                                 // 全靜音：錄音根本不離開裝置
        return finish({ strategy: "等待", branch_id: "WAIT", flags: {} }, gate);
      }
      if (C.mock) return mockJudge(gate).then(function (r) { blob = null; return finish(r, gate); });

      show("secBusy", true); $("busyText").textContent = "正在轉成文字";
      return fetch(C.transcribe, { method: "POST", headers: { "Content-Type": blob.type }, body: blob })
        .then(function (r) { blob = null; return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || r.status); return j; }); })
        .then(function (t) {
          // 後端判定沒講話時，閘門記的開口點其實是尾音或點擊聲，等待秒數改用整段長度
          if (t.silent || !t.text) return finish({ strategy: "等待", branch_id: "WAIT", flags: {} },
                                                 { ratio: gate.ratio, wait_sec: Math.round(totalSec) });
          $("busyText").textContent = "正在判定策略";
          return fetch(C.judge, { method: "POST", headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify({ text: t.text, card: card.id }) })
            .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || r.status); return j; }); })
            .then(function (j) { return finish(j, gate); });
        })
        .catch(function (e) {
          show("secBusy", false); show("secIntro", true);
          $("micErr").textContent = "這一回合沒有完成：" + (e && e.message || e) + "。錄音已丟棄，請再試一次。";
          $("micErr").hidden = false;
        });
    });
  }

  // 靜音閘門＋開口時點。全部用相對量，不用絕對音量（換支麥克風就失效）
  function analyze(fr, dur, totalSec) {
    var n = fr.length;
    if (n < 8) return { silent: true, ratio: 0, wait_sec: Math.round(totalSec) };
    var s = fr.slice().sort(function (a, b) { return a - b; });
    var med = s[Math.floor(n * 0.5)], p95 = s[Math.floor(n * 0.95)];
    var ratio = p95 / (med + 1e-9);
    if (ratio < DYNAMIC_MIN) return { silent: true, ratio: ratio, wait_sec: Math.round(totalSec) };
    // 開口門檻不能只看中位數：串流開頭常是數位零（median≈0、ratio 上千），2×0 仍是 0，
    // 第一幀底噪就會被當成開口（2026-09-22 驗收實測 4 輪 wait_sec 全記成 0）。
    // 加一道相對峰值的下限：至少要到 P95 的三成才算講話
    var thr = Math.max(med * DYNAMIC_MIN, p95 * 0.3), run = 0, onset = n;
    for (var i = 0; i < n; i++) {
      run = fr[i] > thr ? run + 1 : 0;
      if (run >= ONSET_RUN) { onset = i - ONSET_RUN + 1; break; }
    }
    // 只記統計量，不記內容。之後驗證風險 #1／#5 時要看的就是這幾個數字
    // 每秒峰值佔 P95 的百分比：驗證風險 #5 時看「開口前有沒有東西超過門檻」用的。純數字剖面，不是內容
    var perSec = Math.round(1 / dur), profile = [];
    for (var j = 0; j < n; j += perSec) profile.push(Math.round(Math.max.apply(null, fr.slice(j, j + perSec)) / p95 * 100));
    console.info("gate", { frames: n, median: med.toExponential(2), p95: p95.toExponential(2),
                           ratio: ratio.toFixed(1), onset_frame: onset, wait_sec: Math.round(onset * dur),
                           thr_pct_of_p95: Math.round(thr / p95 * 100), peak_pct_per_sec: profile.join(" ") });
    return { silent: false, ratio: ratio, wait_sec: Math.round(onset * dur) };
  }

  // ------------------------------------------------------------ 回饋
  function tierText(sec) {
    var t = card.wait.tiers.filter(function (x) { return x.max_sec === null || sec <= x.max_sec; })[0];
    return t.text.replace("{n}", sec);
  }

  function finish(j, gate) {
    show("secBusy", false); show("secMock", false);
    var branch = j.branch_id, strategy = j.strategy, flags = j.flags || {};
    var b = card.branches.filter(function (x) { return x.id === branch; })[0];

    // 練習紀錄：只有判定結果，沒有錄音、沒有逐字文字（規格 §5）
    saveLog({ card: card.id, ts: Date.now(), strategy: strategy, branch: branch,
              wait_sec: gate.wait_sec, flags: { is_evaluative: !!flags.is_evaluative,
              gives_answer: !!flags.gives_answer, returns_to_text: !!flags.returns_to_text },
              preselect: preselect, dynamic_ratio: Math.round(gate.ratio * 10) / 10 });

    $("waitFb").textContent = tierText(gate.wait_sec);
    $("l1").innerHTML = layer1(j, b);
    show("contrast", false);

    var l2 = $("layer2");
    if (branch === "RETRY") {
      l2.hidden = true;                              // 不播任何學生語音（§4.3.1）
      $("btnAgain").textContent = "再說一次";
    } else {
      l2.hidden = false;
      $("btnAgain").textContent = "再練一次";
      var seg = (branch === "WAIT") ? card.wait : b;
      $("l2Speaker").textContent = seg.speaker;
      $("l2Text").textContent = seg.text;
      $("l2Cons").textContent = seg.consequence;
      $("btnReplay").onclick = function () { stopPlayer(); play(seg.audio); };
      var cb = b && card.branches.filter(function (x) { return x.id === b.contrast; })[0];
      $("btnContrast").hidden = !cb;
      $("btnContrast").onclick = function () {
        stopPlayer();
        $("contrastHead").textContent = "另一條路（" + b.contrast_note + "）";
        $("cSpeaker").textContent = cb.speaker;
        $("cText").textContent = cb.text;
        $("cCons").textContent = cb.consequence;
        show("contrast", true);
        play(cb.audio);
      };
      play(seg.audio);
    }
    show("secResult", true);
    renderLog();
    window.scrollTo({ top: $("waitFb").offsetTop - 12, behavior: "smooth" });
  }

  // 第一層：策略辨識。可以有對錯，但不用「代答」這個內部標籤
  function layer1(j, b) {
    var h = "", s = j.strategy, r = card.retry;
    if (j.branch_id === "WAIT") {
      return '<p class="verdict">你這一回合沒有開口。</p><p class="note">等待也是一個理答動作。</p>';
    }
    if (j.branch_id === "RETRY") {
      if (r[s] && j.confidence >= r.min_confidence) {
        var x = r[s];
        h += '<p class="verdict">' + esc(x.lead) + "</p><p>" + esc(x.body) + "</p>";
        h += '<p class="note">' + esc(x.example_intro) + "</p>";
        h += '<div class="example">' + x.example.map(function (e) {
          return "<div><b>" + esc(e[0]) + "</b>" + esc(e[1]) + "</div>"; }).join("") + "</div>";
        h += "<p>" + esc(x.example_note) + "</p>";
        return h;
      }
      // 只有評價、沒有交出發言權的句子（「好，很好。」）也會落到這裡，不能只回一句判不出來
      if (j.flags && j.flags.is_evaluative && s === "其他") {
        return '<p class="verdict">這一句是對學生發言的評價，還沒有把話交給任何人。</p>' +
               '<p class="note">評價之後，教室通常會等老師的下一句。試試看這一句要把話交給誰。</p>';
      }
      return '<p class="verdict">' + esc(r.generic) + "</p>";
    }
    var isGive = b && b.strategy === "代答";
    if (isGive) {
      h += '<p class="verdict">這一句不在六個策略裡。</p><p>' + esc(b.recognition) + "</p>";
    } else {
      h += '<p class="verdict">你這一句是<b>' + esc(s) + "</b>。</p>";
      if (card.strategies[s]) h += '<p class="note">' + esc(card.strategies[s]) + "</p>";
    }
    if (j.evidence_span) h += '<p class="evidence">依據：<q>' + esc(j.evidence_span) + "</q></p>";
    if (j.recognition_note && !isGive) h += '<p class="note">' + esc(j.recognition_note) + "</p>";
    if (j.secondary && j.secondary !== s && card.strategies[j.secondary]) {
      h += '<p class="note">同時帶有一點<b>' + esc(j.secondary) + "</b>。</p>";
    }
    if (j.flags && j.flags.is_evaluative) {
      h += '<p class="note">這一句裡也出現了對學生發言的評價用語。</p>';
    }
    if (preselect && preselect !== s) {
      h += '<div class="mismatch"><p>你事前選的是<b>' + esc(preselect) + "</b>。" +
           esc(preselect) + "：" + esc(card.strategies[preselect]) + "</p>";
      if (card.strategies[s]) h += "<p>而這一句要的是" + esc(s) + "：" + esc(card.strategies[s]) + "</p>";
      h += "</div>";
    } else if (preselect && preselect === s) {
      h += '<p class="mismatch">和你事前選的一樣。</p>';
    }
    return h;
  }

  function reset() {
    stopPlayer();
    // 事前自選只屬於那一回合，換回合就清掉；否則上一輪的選擇會被拿來對照這一輪
    preselect = null;
    Array.prototype.forEach.call(document.querySelectorAll("#secIntro .chips .on"), function (x) { x.classList.remove("on"); });
    show("secResult", false); show("contrast", false); show("secIntro", true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // ------------------------------------------------------------ 練習紀錄（localStorage）
  function readLog() {
    try { return JSON.parse(localStorage.getItem(C.storageKey) || "[]"); } catch (e) { return []; }
  }
  function saveLog(rec) {
    var log = readLog(); log.push(rec);
    try { localStorage.setItem(C.storageKey, JSON.stringify(log)); } catch (e) { /* 配額滿了就算了 */ }
  }
  var LABEL = { "代答": "替學生說完", "等待": "等待", "其他": "未判定" };
  function renderLog() {
    var log = readLog();
    $("logCount").textContent = log.length ? "（" + log.length + " 回合）" : "";
    if (!log.length) { $("logStats").innerHTML = '<p class="hint">還沒有紀錄。</p>'; return; }
    var cnt = {};
    log.forEach(function (r) {
      var k = r.branch === "RETRY" && (r.strategy === "其他" || !r.strategy) ? "未判定" : (LABEL[r.strategy] || r.strategy || "未判定");
      cnt[k] = (cnt[k] || 0) + 1;
    });
    var max = Math.max.apply(null, Object.keys(cnt).map(function (k) { return cnt[k]; }));
    var waits = log.map(function (r) { return r.wait_sec || 0; });
    var avg = Math.round(waits.reduce(function (a, b) { return a + b; }, 0) / waits.length);
    $("logStats").innerHTML = '<ul class="stats">' + Object.keys(cnt).map(function (k) {
      return '<li><span class="k">' + esc(k) + '</span><span class="bar" style="width:' +
        Math.round(cnt[k] / max * 160) + 'px"></span><span class="v">' + cnt[k] + "</span></li>";
    }).join("") + '</ul><p class="hint">平均等待 ' + avg + " 秒，最長 " + Math.max.apply(null, waits) + " 秒。</p>";
  }
  function exportLog() {
    var blob = new Blob([JSON.stringify({ exported_at: new Date().toISOString(), records: readLog() }, null, 2)],
                        { type: "application/json" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "理答練習紀錄_" + new Date().toISOString().slice(0, 10) + ".json";
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
  }

  // ------------------------------------------------------------ 模擬判定（?mock=1）
  var mockPick = null;
  function buildMock() {
    var chips = $("mockChips");
    ["釐清", "深究", "轉引", "轉問", "反問", "提示", "代答", "其他"].forEach(function (s) {
      var b = document.createElement("button");
      b.className = "chip"; b.type = "button"; b.textContent = s;
      b.onclick = function () { mockPick && mockPick(s); };
      chips.appendChild(b);
    });
  }
  // 只給模擬面板用，線上以後端 functions/cards/<id>.json 為準。由卡片分支的 strategy／also 推導；
  // 卡片沒有分支的策略（例如 C01 的反問）走 RETRY，和後端一致
  function mockBranch(s) {
    var hit = card.branches.filter(function (b) {
      return b.strategy === s || (b.also || []).indexOf(s) >= 0; })[0];
    return hit ? hit.id : "RETRY";
  }
  function giveBranch() { return mockBranch("代答"); }
  function mockJudge(gate) {
    show("secMock", true);
    $("mockText").value = "";
    return new Promise(function (res) {
      mockPick = function (s) {
        mockPick = null;
        var text = $("mockText").value.trim();
        var flags = { gives_answer: $("mockGives").checked, is_evaluative: $("mockEval").checked };
        var conf = $("mockLow").checked ? 0.3 : 0.9;
        var branch = flags.gives_answer ? giveBranch() : (conf < 0.5 ? "RETRY" : mockBranch(s));
        res({ strategy: s, secondary: null, confidence: conf,
              evidence_span: text ? text.slice(0, 12) : null, flags: flags,
              recognition_note: "（模擬）這是手動選的結果，不是模型判定。", branch_id: branch });
      };
    });
  }
})();
