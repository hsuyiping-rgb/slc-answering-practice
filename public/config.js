// 後端代理的端點。這裡只有網址，沒有任何金鑰（金鑰在 Secret Manager，見 functions/main.py）。
// 本機開發（localhost）自動改打 emulator；網址加 ?mock=1 則完全不打後端，用手動面板模擬判定結果。
(function () {
  var PROJECT = "slc-die-mode", REGION = "us-central1";
  var local = /^(localhost|127\.0\.0\.1)$/.test(location.hostname);
  var base = local
    ? "http://127.0.0.1:5001/" + PROJECT + "/" + REGION + "/"
    : "https://" + REGION + "-" + PROJECT + ".cloudfunctions.net/";
  window.SLC_CONFIG = {
    transcribe: base + "answering_transcribe",
    judge: base + "answering_judge",
    mock: /[?&]mock=1/.test(location.search),
    storageKey: "slc_answering_log"
  };
})();
