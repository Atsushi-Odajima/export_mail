// 確認ダイアログ・全選択・ジョブ進捗の自動更新（インラインスクリプトは使わない）
document.addEventListener("submit", function (ev) {
  var msg = ev.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) { ev.preventDefault(); }
});
document.addEventListener("change", function (ev) {
  var name = ev.target.getAttribute && ev.target.getAttribute("data-toggle-all");
  if (!name) return;
  document.querySelectorAll('input[name="' + name + '"]').forEach(function (cb) { cb.checked = ev.target.checked; });
});
(function () {
  var el = document.getElementById("jobstatus");
  if (!el || el.getAttribute("data-active") !== "1") return;
  var id = el.getAttribute("data-job");
  var last = null;
  setInterval(function () {
    fetch("/api/jobs/" + encodeURIComponent(id), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var key = j.status + ":" + j.done;
        if (last !== null && key !== last) { window.location.reload(); }
        last = key;
      }).catch(function () {});
  }, 3000);
})();
