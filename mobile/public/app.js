// Samurai Dealer Research（iPhone 向け画面）。
// - 取得したサイトの文字列はすべて textContent で表示し、HTML として解釈しない。
// - 外部リンクは http/https のみ、新しいタブ（noopener noreferrer）で開く。
// - 変更操作は x-csrf-token を付けて送る。

const view = document.getElementById("view");
const titleEl = document.getElementById("title");
const backBtn = document.getElementById("back");
const tabs = document.getElementById("tabs");
const busyEl = document.getElementById("busy");
const toastEl = document.getElementById("toast");

const state = { user: null, server: null, listQuery: { q: "", filter: "", category: "" }, pollTimer: null, renderSeq: 0 };

// ---------------------------------------------------------------- DOM 補助
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "value") el.value = v;
      else if (k === "checked" || k === "disabled" || k === "selected" || k === "hidden" || k === "required" || k === "multiple") el[k] = !!v;
      else el.setAttribute(k, v === true ? "" : String(v));
    }
  }
  append(el, children);
  return el;
}
function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
  return el;
}
function safeHttp(url) {
  try {
    const u = new URL(String(url));
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}
function extLink(url, label) {
  const href = safeHttp(url);
  if (!href) return h("span", { class: "muted" }, label || String(url || ""));
  return h("a", { href, target: "_blank", rel: "noopener noreferrer", class: "ext" }, label || href);
}
function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function chip(text, kind) {
  return h("span", { class: "chip" + (kind ? " " + kind : "") }, text);
}
function statusKind(s) {
  if (!s) return "";
  if (/確認済み|完了|対象（/.test(s)) return "ok";
  if (/失敗|除外|英国外|対象外/.test(s)) return "ng";
  if (/要|未確認|待ち|停止|一部/.test(s)) return "warn";
  return "";
}
function section(title, ...children) {
  return h("section", { class: "card" }, title ? h("h2", null, title) : null, ...children);
}
function field(label, input, hint) {
  return h("label", { class: "field" }, h("span", { class: "label" }, label), input, hint ? h("span", { class: "hint" }, hint) : null);
}
function select(name, options, current) {
  return h("select", { name }, options.map((o) => {
    const [v, l] = Array.isArray(o) ? o : [o, o];
    return h("option", { value: v, selected: v === current }, l);
  }));
}
function button(label, onclick, cls) {
  return h("button", { type: "button", class: "btn " + (cls || ""), onclick }, label);
}

let toastTimer = null;
function toast(msg, isError) {
  toastEl.textContent = msg;
  toastEl.className = "toast" + (isError ? " error" : "");
  toastEl.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (toastEl.hidden = true), isError ? 6000 : 3500);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("コピーしました");
  } catch {
    // クリップボードが使えない環境では選択して手動コピーできるようにする
    const ta = h("textarea", { class: "copybox", readonly: true }, text);
    document.body.appendChild(ta);
    ta.select();
    toast("コピーできませんでした。表示された文字を長押ししてコピーしてください", true);
    setTimeout(() => ta.remove(), 15000);
  }
}
function copyBtn(text, label) {
  return h("button", { type: "button", class: "btn small", onclick: () => copyText(text) }, label || "コピー");
}

// ---------------------------------------------------------------- 通信
class ApiError extends Error {
  constructor(msg, status, data) {
    super(msg);
    this.status = status;
    this.data = data;
  }
}
async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body !== undefined ? "POST" : "GET"), headers: {}, credentials: "same-origin", cache: "no-store" };
  if (init.method === "POST") {
    init.headers["content-type"] = "application/json";
    if (state.user?.csrf) init.headers["x-csrf-token"] = state.user.csrf;
    init.body = JSON.stringify(opts.body ?? {});
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError("通信できませんでした。電波の状態を確認してください", 0, null);
  }
  const type = res.headers.get("content-type") || "";
  const data = type.includes("application/json") ? await res.json().catch(() => null) : null;
  if (!res.ok) {
    const msg = data?.error || `エラーが発生しました（${res.status}）`;
    if (res.status === 401 && !opts.allow401) {
      state.user = null;
      location.hash = "#/login";
    }
    if (res.status === 409 && data?.migration_required) {
      state.server = { ...(state.server || {}), schema: data.schema };
      if (!location.hash.startsWith("#/migrate")) location.hash = "#/migrate";
    }
    throw new ApiError(msg, res.status, data);
  }
  return opts.raw ? res : data;
}
async function act(path, body, after) {
  try {
    const r = await api(path, { body });
    if (r?.message) toast(r.message);
    if (after) await after(r);
    return r;
  } catch (e) {
    toast(e.message, true);
    return null;
  }
}

// ---------------------------------------------------------------- 画面の枠
function setFrame(title, { back = null, tab = null } = {}) {
  titleEl.textContent = title;
  backBtn.hidden = !back;
  backBtn.onclick = back ? () => (history.length > 1 ? history.back() : (location.hash = back)) : null;
  tabs.hidden = !state.user;
  for (const a of tabs.querySelectorAll("a")) a.classList.toggle("active", a.dataset.tab === tab);
}
function mount(...children) {
  view.replaceChildren(...children.flat().filter(Boolean));
  view.scrollTop = 0;
  window.scrollTo(0, 0);
}
function errorView(e) {
  return section("表示できませんでした", h("p", { class: "error-text" }, e.message), button("再読み込み", () => route()));
}

// ---------------------------------------------------------------- 調査の進行（画面を開いている間は速く進める）
async function tickOnce() {
  if (!state.user || document.hidden) return;
  try {
    const r = await api("/api/tick", { body: {} });
    busyEl.hidden = !r.active_jobs;
    if (r.active_jobs) schedulePoll(2500);
    else stopPoll();
    if (r.steps?.length && typeof state.onTick === "function") state.onTick(r);
  } catch {
    schedulePoll(10000);
  }
}
function schedulePoll(ms) {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(tickOnce, ms);
}
function stopPoll() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.user) schedulePoll(300);
});

// ---------------------------------------------------------------- ルーター
const routes = [
  [/^#\/login$/, loginView],
  [/^#\/setup$/, setupView],
  [/^#\/migrate$/, migrateView],
  [/^#\/?$/, listView],
  [/^#\/d\/([A-Z]{3}-[0-9A-Z]{26})$/, detailView],
  [/^#\/candidates$/, candidatesView],
  [/^#\/add$/, addView],
  [/^#\/jobs$/, jobsView],
  [/^#\/jobs\/([A-Z]{3}-[0-9A-Z]{26})$/, jobDetailView],
  [/^#\/settings$/, settingsView],
];

async function route() {
  const seq = ++state.renderSeq;
  state.onTick = null;
  const hash = location.hash || "#/";
  if (!state.server) await loadState();
  if (state.server?.setup_required && hash !== "#/setup") return (location.hash = "#/setup");
  if (!state.server?.setup_required && !state.user && hash !== "#/login") return (location.hash = "#/login");
  if (state.server?.schema?.migrationRequired && state.user && hash !== "#/migrate" && hash !== "#/settings") return (location.hash = "#/migrate");
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) {
      try {
        await fn(...m.slice(1), seq);
      } catch (e) {
        if (seq === state.renderSeq && !(e instanceof ApiError && (e.status === 401 || e.status === 409))) mount(errorView(e));
      }
      return;
    }
  }
  location.hash = "#/";
}
const stale = (seq) => seq !== state.renderSeq;

async function loadState() {
  try {
    state.server = await api("/api/state", { allow401: true });
    state.user = state.server.user;
  } catch (e) {
    state.server = null;
    setFrame("販売店調査");
    mount(errorView(e));
    throw e;
  }
}

// ---------------------------------------------------------------- 初回設定・ログイン
function setupView() {
  setFrame("はじめての設定");
  const tokenIn = h("input", { type: "password", name: "setup_token", autocomplete: "off", required: true });
  const userIn = h("input", { type: "text", name: "username", autocomplete: "username", autocapitalize: "none", spellcheck: "false", required: true, value: "admin" });
  const form = h("form", { class: "stack", onsubmit: async (ev) => {
    ev.preventDefault();
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      const r = await api("/api/setup", { body: { setup_token: tokenIn.value, username: userIn.value } });
      state.user = r.user;
      state.server.setup_required = false;
      state.server.user = r.user;
      showKeyThenSeed(r.key, userIn.value);
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false;
    }
  } },
  field("初期設定用の合言葉（SETUP_TOKEN）", tokenIn, "Cloudflare で設定した 20 文字以上の値です"),
  field("管理者の名前（英数字）", userIn),
  h("button", { type: "submit", class: "btn primary" }, "管理者を作成"));
  const warn = state.server?.setup_token_configured ? null
    : h("p", { class: "error-text" }, "SETUP_TOKEN がまだ設定されていません。Cloudflare の Worker の設定で SETUP_TOKEN（20 文字以上）を登録してから開き直してください。");
  mount(section("管理者の作成", h("p", null, "最初に管理者を 1 人作ります。作成後に表示されるログイン用キーはパスワードの代わりです。"), warn, form));
}

function showKeyThenSeed(key, username) {
  setFrame("ログイン用キー");
  mount(section("ログイン用キーを保存してください",
    h("p", null, "このキーは今しか表示されません。iPhone の「パスワード」アプリやメモなど安全な場所に保存してください。"),
    h("p", { class: "keybox" }, key),
    h("div", { class: "row" }, copyBtn(key, "キーをコピー")),
    savePasswordForm(username, key),
    h("p", { class: "hint" }, "他の人に教えないでください。忘れた場合は管理者が設定画面で発行し直せます。"),
    button("保存しました。初期データを読み込む", () => seedLoop(), "primary")));
}

// iOS の「パスワードを保存」を促すための見えないログインフォーム（送信はしない）
function savePasswordForm(username, key) {
  return h("form", { class: "pwsave", "aria-hidden": "true", onsubmit: (e) => e.preventDefault() },
    h("input", { type: "text", autocomplete: "username", value: username, tabindex: "-1" }),
    h("input", { type: "password", autocomplete: "new-password", value: key, tabindex: "-1" }));
}

async function seedLoop() {
  setFrame("初期データの読み込み");
  const bar = h("progress", { max: 30, value: 0 });
  const msg = h("p", null, "確認済みの英国販売店 30 社のデータを読み込んでいます…");
  mount(section("初期データ", msg, bar, h("p", { class: "hint" }, "画面を閉じても、後で設定画面から続きを読み込めます。")));
  try {
    for (let i = 0; i < 100; i++) {
      const r = await api("/api/seed/step", { body: {} });
      bar.max = r.total;
      bar.value = r.done;
      msg.textContent = `読み込み中… ${r.done} / ${r.total}`;
      if (r.done >= r.total) break;
    }
    toast("初期データを読み込みました");
    location.hash = "#/";
    route();
  } catch (e) {
    msg.textContent = "読み込みが中断しました：" + e.message;
    view.appendChild(button("続きを読み込む", () => seedLoop(), "primary"));
  }
}

function loginView() {
  setFrame("ログイン");
  const userIn = h("input", { type: "text", name: "username", autocomplete: "username", autocapitalize: "none", spellcheck: "false", required: true });
  const keyIn = h("input", { type: "password", name: "key", autocomplete: "current-password", required: true });
  const form = h("form", { class: "stack", onsubmit: async (ev) => {
    ev.preventDefault();
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    try {
      const r = await api("/api/login", { body: { username: userIn.value, key: keyIn.value }, allow401: true });
      state.user = r.user;
      state.server = null;
      await loadState();
      location.hash = state.server?.schema?.migrationRequired ? "#/migrate" : "#/";
      schedulePoll(500);
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false;
    }
  } },
  field("名前", userIn),
  field("ログイン用キー", keyIn, "例：ABCD-EFGH-…（ハイフンは省略可）"),
  h("button", { type: "submit", class: "btn primary" }, "ログイン"));
  mount(section("ログイン", form), h("p", { class: "hint center" }, "英国の中古車販売店の公開情報を調べるアプリです。メールの送信機能はありません。"));
}

// ---------------------------------------------------------------- DB 更新
function migrateView() {
  setFrame("データベースの更新");
  const sc = state.server?.schema || {};
  const isAdmin = state.user?.role === "admin";
  const saved = h("input", { type: "checkbox" });
  mount(section("更新が必要です",
    h("p", null, `アプリが新しくなり、データベースの版を ${sc.version} から ${sc.latest} に更新する必要があります。`),
    h("p", null, "更新の前に、必ずバックアップを保存してください。"),
    button("バックアップを作成して保存", () => makeBackup(), "primary"),
    isAdmin ? h("div", { class: "stack" },
      h("label", { class: "check" }, saved, "バックアップを保存しました"),
      button("データベースを更新", async () => {
        if (!saved.checked) return toast("バックアップを保存してからチェックを入れてください", true);
        const r = await act("/api/migrate", { backup_saved: true });
        if (r) {
          toast("更新しました");
          state.server = null;
          location.hash = "#/";
        }
      })) : h("p", { class: "hint" }, "更新は管理者だけが実行できます。")));
}

// ---------------------------------------------------------------- 一覧
const FILTERS = [
  ["", "すべて"], ["has_email", "メールあり"], ["has_form", "フォームあり"], ["needs_review", "要確認"],
  ["status:未調査", "未調査"], ["status:取得失敗", "取得失敗"], ["include_archived", "非表示も含む"],
];

async function listView(seq) {
  setFrame("販売店一覧", { tab: "list" });
  const q = state.listQuery;
  const params = new URLSearchParams();
  if (q.q) params.set("q", q.q);
  if (q.category) params.set("category", q.category);
  if (q.filter.startsWith("status:")) params.set("status", q.filter.slice(7));
  else if (q.filter) params.set(q.filter, "1");
  const data = await api("/api/dealers?" + params);
  if (stale(seq)) return;
  const st = data.stats;
  const search = h("input", { type: "search", placeholder: "店名・URL・メモで検索", value: q.q, enterkeyhint: "search", autocapitalize: "none" });
  const searchForm = h("form", { class: "search", role: "search", onsubmit: (e) => {
    e.preventDefault();
    q.q = search.value.trim();
    search.blur();
    route();
  } }, search);
  const chips = h("div", { class: "chips", role: "group", "aria-label": "絞り込み" }, FILTERS.map(([v, l]) =>
    h("button", { type: "button", class: "fchip" + (q.filter === v ? " on" : ""), "aria-pressed": String(q.filter === v), onclick: () => {
      q.filter = v;
      route();
    } }, l)));
  const catSel = select("category", [["", "分類：すべて"], ...data.categories], q.category);
  catSel.addEventListener("change", () => {
    q.category = catSel.value;
    route();
  });
  const stats = h("div", { class: "stats" },
    stat("販売店", st.total), stat("メール", st.with_email), stat("フォーム", st.with_form), stat("連絡先なし", st.unconfirmed_contact), stat("確認済み", st.reviewed));
  const cards = data.dealers.length ? data.dealers.map(dealerCard) : [h("p", { class: "empty" }, "該当する販売店はありません")];
  const exportUrl = "/api/export/dealers.csv?" + params;
  mount(
    searchForm, chips, h("div", { class: "row" }, catSel), stats,
    h("p", { class: "count" }, `${data.dealers.length} 件を表示`),
    h("div", { class: "list" }, cards),
    section("まとめて操作",
      h("div", { class: "row wrap" },
        button("未調査をすべて調査", () => act("/api/research/start", { scope: "unresearched" }, (r) => goJob(r))),
        button("失敗分を再調査", () => act("/api/research/start", { scope: "failed" }, (r) => goJob(r)))),
      h("div", { class: "row wrap" },
        downloadBtn(exportUrl, "dealers.csv", "一覧を CSV で保存"),
        downloadBtn("/api/export/contacts.csv", "contacts.csv", "連絡方法 CSV"),
        downloadBtn("/api/export/evidence.csv", "evidence.csv", "根拠 CSV")),
      h("p", { class: "hint" }, "CSV は Excel・Numbers で開けます（UTF-8 BOM 付き）。")),
  );
}
function stat(label, n) {
  return h("div", { class: "stat" }, h("strong", null, String(n ?? 0)), h("span", null, label));
}
function goJob(r) {
  if (r?.job_id) {
    schedulePoll(300);
    location.hash = "#/jobs/" + r.job_id;
  }
}

function dealerCard(d) {
  const contactLine = [];
  if (d.emails_ok.length) contactLine.push(h("span", { class: "email" }, d.emails_ok[0] + (d.emails_ok.length > 1 ? ` 他${d.emails_ok.length - 1}件` : "")));
  else if (d.forms_ok.length) contactLine.push(h("span", null, "問い合わせフォームあり"));
  else contactLine.push(h("span", { class: "muted" }, d.contact_summary));
  return h("a", { class: "dcard", href: "#/d/" + d.id },
    h("div", { class: "dhead" }, h("strong", { class: "dname" }, d.name || d.host),
      chip(d.contact_summary, statusKind(d.contact_summary))),
    h("div", { class: "dmeta" }, d.region || "地域未確認", " ・ ", (d.categories || []).join("／") || "分類なし"),
    d.summary ? h("p", { class: "dsum" }, d.summary) : null,
    h("div", { class: "dfoot" }, contactLine,
      d.research_status !== "完了" ? chip(d.research_status, statusKind(d.research_status)) : null,
      d.review_status === "確認済み" ? chip("人が確認済み", "ok") : null,
      d.duplicate_status ? chip(d.duplicate_status, "warn") : null,
      d.archived ? chip("非表示", "ng") : null));
}

// CSV などの保存：Cookie 付きで取得し、共有シート（使えなければダウンロード）で渡す
function downloadBtn(url, filename, label) {
  return button(label, async () => {
    try {
      const res = await api(url, { raw: true });
      const blob = await res.blob();
      await saveBlob(blob, filename);
    } catch (e) {
      toast(e.message, true);
    }
  }, "small");
}
async function saveBlob(blob, filename) {
  const file = new File([blob], filename, { type: blob.type || "application/octet-stream" });
  if (navigator.canShare && navigator.canShare({ files: [file] })) {
    try {
      await navigator.share({ files: [file], title: filename });
      return;
    } catch (e) {
      if (e && e.name === "AbortError") return;
    }
  }
  const a = h("a", { href: URL.createObjectURL(blob), download: filename });
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    URL.revokeObjectURL(a.href);
    a.remove();
  }, 30000);
  toast("保存しました（「ファイル」アプリのダウンロードを確認してください）");
}

// ---------------------------------------------------------------- 詳細
async function detailView(id, seq) {
  setFrame("販売店の詳細", { back: "#/", tab: "list" });
  const data = await api("/api/dealers/" + id);
  if (stale(seq)) return;
  const d = data.dealer;
  titleEl.textContent = d.name || d.website_host;
  const current = d.contacts.filter((c) => c.is_current);
  const old = d.contacts.filter((c) => !c.is_current);
  const reload = () => route();

  const head = section(null,
    h("h2", { class: "big" }, d.name || d.website_host, d.name_is_manual ? chip("人が入力", "") : null),
    h("p", null, extLink(d.website_url)),
    h("div", { class: "chips static" },
      chip(d.contact_summary, statusKind(d.contact_summary)), chip("調査：" + d.research_status, statusKind(d.research_status)),
      chip("確認：" + d.review_status, statusKind(d.review_status)), chip(d.eligibility, statusKind(d.eligibility))),
    d.failure_reason ? h("p", { class: "error-text" }, "取得できなかった理由：" + d.failure_reason) : null,
    h("dl", { class: "kv" },
      kv("販売店ID", h("span", { class: "mono" }, d.id), copyBtn(d.id)),
      kv("地域", d.region || "未確認"),
      kv("住所", d.address || "未確認"),
      kv("分類", (d.categories || []).join("／") || "なし", d.category_basis ? h("span", { class: "hint" }, "（" + d.category_basis + "）") : null),
      kv("中古車販売", d.used_sales_auto || "未確認"),
      kv("英国内", d.uk_check_auto || "未確認"),
      kv("最終調査", fmtDate(d.last_researched_at)),
      kv("取得元", d.data_origin)),
    d.summary ? h("div", { class: "summary" }, h("h3", null, "日本語の概要", d.summary_is_manual ? chip("人が入力", "") : null), h("p", null, d.summary)) : null,
    h("div", { class: "row wrap" },
      button("再調査", () => act(`/api/dealers/${id}/research`, {}, (r) => goJob(r))),
      d.review_status !== "確認済み" ? button("確認済みにする", () => act(`/api/dealers/${id}/review`, { status: "確認済み" }, reload), "primary")
        : button("要確認に戻す", () => act(`/api/dealers/${id}/review`, { status: "要確認" }, reload))));

  const contacts = section("連絡方法",
    h("p", { class: "hint" }, "公開されている連絡先です。掲載されていることは送信の許可を意味しません。"),
    current.length ? current.map((c) => contactItem(c, data.contact_statuses, reload)) : h("p", { class: "empty" }, "確認できた連絡方法はありません"),
    old.length ? h("details", null, h("summary", null, `前回の調査で見つかったが今回は見つからなかったもの（${old.length}）`), old.map((c) => contactItem(c, data.contact_statuses, reload))) : null,
    addContactForm(id, reload));

  const dup = d.duplicate_status || data.dup || data.parent || data.branches?.length ? section("重複・支店",
    d.duplicate_status ? h("p", null, "状態：" + d.duplicate_status) : null,
    data.dup ? h("p", null, "似ている販売店：", h("a", { href: "#/d/" + data.dup.id }, data.dup.name || data.dup.host)) : null,
    data.parent ? h("p", null, "本店：", h("a", { href: "#/d/" + data.parent.id }, data.parent.name || data.parent.id)) : null,
    data.branches?.length ? h("p", null, "支店：", data.branches.map((b) => h("a", { href: "#/d/" + b.id, class: "sp" }, b.name || b.id))) : null,
    h("div", { class: "row wrap" },
      button("別の事業者と確認", () => act(`/api/dealers/${id}/duplicate`, { action: "distinct" }, reload), "small"),
      data.dup ? button("支店として関連付け", () => act(`/api/dealers/${id}/duplicate`, { action: "branch" }, reload), "small") : null,
      d.parent_dealer_id ? button("関連付けを外す", () => act(`/api/dealers/${id}/duplicate`, { action: "unlink" }, reload), "small") : null,
      d.archived ? button("再表示", () => act(`/api/dealers/${id}/duplicate`, { action: "unarchive" }, reload), "small")
        : button("重複として非表示", () => act(`/api/dealers/${id}/duplicate`, { action: "archive" }, reload), "small danger"))) : null;

  const evidence = section(`根拠（${data.evidence.length}）`,
    h("p", { class: "hint" }, "各情報をどのページのどの記載から判断したかの記録です。"),
    data.evidence.length ? h("ul", { class: "evlist" }, data.evidence.slice(0, 100).map((e) => h("li", null,
      h("div", { class: "evtopic" }, e.topic, e.contact_value ? h("span", { class: "muted" }, "：" + e.contact_value) : null),
      e.excerpt ? h("blockquote", null, e.excerpt) : null,
      h("div", { class: "evsrc" }, e.source_url ? extLink(e.source_url, shortUrl(e.source_url)) : null, " ", h("span", { class: "muted" }, fmtDate(e.observed_at) + "・" + e.origin))))) : h("p", { class: "empty" }, "根拠はまだありません"),
    data.evidence.length > 100 ? h("p", { class: "hint" }, "先頭 100 件を表示しています。すべては根拠 CSV で確認できます。") : null);

  const run = data.runs[0];
  const pages = run ? section("調べたページ（最新の調査）",
    h("p", { class: "hint" }, `${fmtDate(run.started_at)}・${run.status}・取得 ${run.pages_ok} / ${run.pages_attempted} ページ`),
    h("ul", { class: "plist" }, run.pages.map((p) => h("li", null, chip(p.status, statusKind(p.status === "取得" ? "完了" : p.status)), " ", extLink(p.url, shortUrl(p.url)), p.error ? h("div", { class: "muted small" }, p.error) : null)))) : null;

  const history = section(`変更履歴（${data.history.length}）`,
    h("details", null, h("summary", null, "表示する"),
      h("ul", { class: "hlist" }, data.history.map((x) => h("li", null,
        h("span", { class: "muted" }, fmtDate(x.changed_at) + "・" + x.origin + (x.changed_by ? "・" + x.changed_by : "")), h("br"),
        h("strong", null, x.field), "：", shorten(x.old_value), " → ", shorten(x.new_value))))));

  mount(head, contacts, editForm(d, data, reload), dup, evidence, pages, history);
}
function kv(k, ...v) {
  return [h("dt", null, k), h("dd", null, ...v)];
}
function shortUrl(u) {
  try {
    const x = new URL(u);
    return x.host + (x.pathname.length > 1 ? x.pathname : "");
  } catch {
    return String(u);
  }
}
function shorten(v) {
  if (v === null || v === undefined || v === "") return "（なし）";
  const s = String(v);
  return s.length > 120 ? s.slice(0, 120) + "…" : s;
}

function contactItem(c, statuses, reload) {
  const sel = select("status", [["", "自動判定に戻す"], ...statuses], c.status_is_manual ? c.status : "");
  sel.addEventListener("change", () => act(`/api/contacts/${c.id}/status`, { status: sel.value }, reload));
  const value = c.kind === "email" ? h("span", { class: "email big" }, c.value)
    : c.kind === "phone" ? h("span", { class: "mono" }, c.value) : extLink(c.value, shortUrl(c.value));
  return h("div", { class: "contact" + (c.is_current ? "" : " old") },
    h("div", { class: "chead" }, h("span", { class: "kind" }, c.kind_label), chip(c.status, statusKind(c.status)), c.status_is_manual ? chip("人が判断", "") : null),
    h("div", { class: "cval" }, value, c.kind === "email" || c.kind === "phone" ? copyBtn(c.value) : null),
    c.purpose ? h("p", { class: "muted small" }, "用途：" + c.purpose) : null,
    c.exclusion_reason ? h("p", { class: "muted small" }, "除外理由：" + c.exclusion_reason) : null,
    c.person_name ? h("p", { class: "muted small" }, "担当：" + c.person_name + (c.person_role ? "（" + c.person_role + "）" : "")) : null,
    c.excerpt ? h("blockquote", null, c.excerpt) : null,
    c.source_url ? h("p", { class: "small" }, "出典：", extLink(c.source_url, shortUrl(c.source_url))) : null,
    h("p", { class: "muted small" }, `連絡方法ID ${c.id}・初回 ${fmtDate(c.first_observed_at)}・最終 ${fmtDate(c.last_observed_at)}`),
    h("label", { class: "field inline" }, h("span", { class: "label" }, "状態を人が判断"), sel));
}

function addContactForm(id, reload) {
  const kind = select("kind", [["email", "メール"], ["form", "問い合わせフォーム"], ["contact_page", "問い合わせページ"], ["phone", "電話"]], "email");
  const value = h("input", { type: "text", name: "value", autocapitalize: "none", spellcheck: "false", required: true });
  const src = h("input", { type: "url", name: "source_url", placeholder: "https://…", autocapitalize: "none" });
  const note = h("input", { type: "text", name: "note" });
  const form = h("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault();
    await act(`/api/dealers/${id}/contacts`, { kind: kind.value, value: value.value, source_url: src.value, note: note.value }, reload);
  } }, field("種類", kind), field("メールアドレス・URL・電話番号", value), field("確認したページの URL", src, "公式サイトで自分の目で確認したものだけを登録してください"), field("メモ", note),
  h("button", { type: "submit", class: "btn" }, "登録"));
  return h("details", { class: "addc" }, h("summary", null, "連絡方法を手動で追加"), form);
}

function editForm(d, data, reload) {
  const name = h("input", { type: "text", value: d.name_manual || "", placeholder: d.name_auto || "" });
  const region = h("input", { type: "text", value: d.region_manual || "", placeholder: d.region_auto || "" });
  const address = h("input", { type: "text", value: d.address_manual || "", placeholder: d.address_auto || "" });
  const summary = h("textarea", { rows: 4, placeholder: d.summary_auto || "" }, d.summary_manual || "");
  const notes = h("textarea", { rows: 3 }, d.notes || "");
  const manualCats = d.categories_manual_list;
  const catBoxes = data.categories.map((c) => {
    const cb = h("input", { type: "checkbox", value: c, checked: manualCats ? manualCats.includes(c) : false });
    return { c, cb, el: h("label", { class: "check" }, cb, c) };
  });
  const basis = select("category_basis", [["", "自動判定のまま"], ...data.bases], d.category_basis_manual || "");
  const elig = select("eligibility", [["", "自動判定のまま"], "対象", "対象外", "要確認"], d.eligibility_manual || "");
  const form = h("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault();
    const cats = catBoxes.filter((x) => x.cb.checked).map((x) => x.c);
    const body = { name: name.value, region: region.value, address: address.value, summary: summary.value, notes: notes.value, category_basis: basis.value, eligibility_manual: elig.value };
    body.categories = cats;
    await act(`/api/dealers/${d.id}/edit`, body, reload);
  } },
  h("p", { class: "hint" }, "空欄の項目は自動で調べた値を使います。入力した値は再調査で上書きされません。"),
  field("店名", name), field("地域", region), field("住所", address),
  h("fieldset", null, h("legend", null, "分類（何も選ばなければ自動判定）"), catBoxes.map((x) => x.el)),
  field("分類の根拠", basis), field("対象かどうか", elig), field("日本語の概要", summary), field("メモ", notes),
  h("button", { type: "submit", class: "btn primary" }, "保存"));
  return h("details", { class: "card editcard" }, h("summary", null, "人の確定値を編集"), form);
}

// ---------------------------------------------------------------- 候補
async function candidatesView(seq) {
  setFrame("候補を探す", { tab: "candidates" });
  const status = state.candStatus ?? "未処理";
  const regionSel = select("region", [], "");
  const catSel = select("category", [], "");
  const meta = await api("/api/discover/meta");
  const list = await api("/api/candidates" + (status ? "?status=" + encodeURIComponent(status) : ""));
  if (stale(seq)) return;
  const curRegion = state.discRegion || meta.regions[0];
  const curCat = state.discCat || meta.categories[0];
  regionSel.replaceChildren(...meta.regions.map((r) => h("option", { value: r, selected: r === curRegion }, r)));
  catSel.replaceChildren(...meta.categories.map((c) => h("option", { value: c, selected: c === curCat }, c)));
  regionSel.addEventListener("change", () => ((state.discRegion = regionSel.value), route()));
  catSel.addEventListener("change", () => ((state.discCat = catSel.value), route()));
  const limit = h("input", { type: "number", min: 1, max: 50, value: 20, inputmode: "numeric" });
  const run = async (provider, btn) => {
    btn.disabled = true;
    btn.textContent = "探しています…";
    await act("/api/discover/run", { provider, region: regionSel.value, category: catSel.value, limit: Number(limit.value) }, () => route());
    btn.disabled = false;
  };
  const osmBtn = h("button", { type: "button", class: "btn primary", onclick: (e) => run("osm", e.currentTarget) }, "OpenStreetMap で探す（無料）");
  const fc = meta.firecrawl;
  const fcBtn = fc.has_key ? h("button", { type: "button", class: "btn", onclick: (e) => run("firecrawl", e.currentTarget) }, `Firecrawl で探す（今月 ${fc.used}/${fc.cap}）`) : null;
  const links = meta.links;
  const selected = new Set();
  const items = list.candidates.map((c) => {
    const cb = h("input", { type: "checkbox", disabled: c.status !== "未処理", onchange: () => (cb.checked ? selected.add(c.id) : selected.delete(c.id)) });
    return h("label", { class: "cand" + (c.status !== "未処理" ? " done" : "") }, cb,
      h("div", null, h("strong", null, c.name || c.url_host), h("div", { class: "small" }, extLink(c.url, c.url_host)),
        h("div", { class: "muted small" }, [c.region, c.source, c.hint].filter(Boolean).join("・")),
        c.status !== "未処理" ? h("div", null, chip(c.status, statusKind(c.status)), c.dealer_id ? h("a", { href: "#/d/" + c.dealer_id, class: "sp" }, "販売店を見る") : null) : null));
  });
  const doAction = (action) => act("/api/candidates/action", { ids: [...selected], action }, (r) => (r.job_id ? goJob(r) : route()));
  const statusSel = select("cstatus", [["未処理", "未処理"], ["登録済み", "登録済み"], ["重複", "重複"], ["除外", "除外"], ["", "すべて"]], status);
  statusSel.addEventListener("change", () => ((state.candStatus = statusSel.value), route()));
  mount(
    section("探す",
      h("p", { class: "hint" }, "見つけた候補は、内容を見てから販売店として登録します（自動では登録しません）。"),
      field("地域", regionSel), field("分類", catSel), field("最大件数", limit),
      h("div", { class: "stack" }, osmBtn, fcBtn),
      fc.has_key ? null : h("p", { class: "hint" }, "Firecrawl（任意・無料枠あり）は設定画面で API キーを登録すると使えます。"),
      h("h3", null, "自分で検索する"),
      h("p", { class: "hint" }, "検索結果から公式サイトを見つけたら、「追加」タブに URL を貼り付けてください。"),
      h("div", { class: "row wrap" }, links.map((l) => extLink(l.url, l.label)))),
    section(`候補一覧（${list.candidates.length}）`, field("表示", statusSel),
      items.length ? h("div", { class: "cands" }, items) : h("p", { class: "empty" }, "候補はありません"),
      status === "未処理" && items.length ? h("div", { class: "row wrap sticky-actions" },
        button("登録して調査", () => doAction("register_research"), "primary"),
        button("登録だけ", () => doAction("register")),
        button("除外", () => doAction("exclude"), "danger")) : null));
}

// ---------------------------------------------------------------- 追加
function addView() {
  setFrame("販売店を追加", { tab: "add" });
  const urls = h("textarea", { rows: 6, placeholder: "https://www.example-motors.co.uk/\n（1 行に 1 つ）", autocapitalize: "none", spellcheck: "false" });
  const start = h("input", { type: "checkbox", checked: true });
  const out = h("div");
  const showResult = (r) => {
    out.replaceChildren(section("結果",
      h("p", null, `新しく登録：${r.new_count} 件`),
      r.errors?.length ? h("ul", { class: "error-text" }, r.errors.map((e) => h("li", null, e))) : null,
      h("ul", { class: "rlist" }, r.results.map((x) => h("li", null, h("span", { class: "mono small" }, x.input), h("br"),
        x.id ? h("a", { href: "#/d/" + x.id }, x.note) : h("span", { class: "error-text" }, x.note)))),
      r.job_id ? button("調査の進捗を見る", () => goJob(r), "primary") : null));
    if (r.job_id) schedulePoll(300);
  };
  const urlForm = h("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault();
    await act("/api/add/urls", { urls: urls.value, start: start.checked }, showResult);
  } }, field("公式サイトの URL", urls, "車両掲載サイト（AutoTrader 等）や SNS は登録できません"),
  h("label", { class: "check" }, start, "登録後すぐに調査する"),
  h("button", { type: "submit", class: "btn primary" }, "登録"));
  const fileIn = h("input", { type: "file", accept: ".csv,text/csv,text/plain" });
  const csvStart = h("input", { type: "checkbox", checked: true });
  const csvForm = h("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault();
    const f = fileIn.files?.[0];
    if (!f) return toast("CSV ファイルを選んでください", true);
    if (f.size > 1_000_000) return toast("1MB 以下のファイルにしてください", true);
    const text = await f.text();
    await act("/api/add/csv", { text, start: csvStart.checked }, showResult);
  } }, field("CSV ファイル", fileIn, "列：url（必須）, name, region, note。1 回 500 行まで"),
  h("label", { class: "check" }, csvStart, "登録後すぐに調査する"),
  h("button", { type: "submit", class: "btn" }, "CSV を読み込む"));
  mount(section("URL を貼り付けて追加", urlForm), section("CSV から追加", csvForm), out);
}

// ---------------------------------------------------------------- 進捗
async function jobsView(seq) {
  setFrame("調査の進捗", { tab: "jobs" });
  const data = await api("/api/jobs");
  if (stale(seq)) return;
  state.onTick = () => route();
  if (data.jobs.some((j) => j.status === "実行中")) schedulePoll(1000);
  mount(
    h("p", { class: "hint pad" }, "この画面を開いている間は速く進みます。閉じている間も 1 分ごとに少しずつ進みます。"),
    data.jobs.length ? h("div", { class: "list" }, data.jobs.map((j) => h("a", { class: "dcard", href: "#/jobs/" + j.id },
      h("div", { class: "dhead" }, h("strong", null, j.params?.label || "調査"), chip(j.status, statusKind(j.status))),
      h("progress", { max: j.total || 1, value: j.done }),
      h("div", { class: "dmeta" }, `${j.done} / ${j.total} 社・${fmtDate(j.created_at)}`, j.message ? "・" + j.message : "")))) : h("p", { class: "empty" }, "まだ調査はありません"));
}

async function jobDetailView(id, seq) {
  setFrame("調査の詳細", { back: "#/jobs", tab: "jobs" });
  const data = await api("/api/jobs/" + id);
  if (stale(seq)) return;
  const j = data.job;
  state.onTick = () => route();
  if (j.status === "実行中") schedulePoll(1000);
  const c = j.counts || {};
  mount(
    section(j.params?.label || "調査",
      h("div", { class: "chips static" }, chip(j.status, statusKind(j.status))),
      h("progress", { max: j.total || 1, value: j.done }),
      h("p", null, `完了 ${c["完了"] || 0}・失敗 ${c["失敗"] || 0}・実行中 ${c["実行中"] || 0}・待機 ${c["待機"] || 0}・停止 ${c["停止"] || 0}`),
      j.message ? h("p", { class: "muted" }, j.message) : null,
      h("div", { class: "row wrap" },
        j.status === "実行中" ? button("停止", () => act(`/api/jobs/${id}/stop`, {}, () => route()), "danger") : null,
        (c["停止"] || 0) > 0 && j.status !== "実行中" ? button("再開", () => act(`/api/jobs/${id}/resume`, {}, () => (schedulePoll(300), route())), "primary") : null,
        (c["失敗"] || 0) + (c["停止"] || 0) > 0 ? button("失敗・停止分を再実行", () => act(`/api/jobs/${id}/retry`, {}, (r) => goJob(r))) : null)),
    h("div", { class: "list" }, data.items.map((it) => h("a", { class: "dcard", href: "#/d/" + it.dealer_id },
      h("div", { class: "dhead" }, h("strong", null, it.name || it.dealer_id), chip(it.status, statusKind(it.status))),
      it.status === "実行中" ? h("div", { class: "dmeta" }, `ページ ${it.pages_done} / ${it.pages_total}`) : null,
      it.error ? h("div", { class: "muted small" }, it.error) : null))));
}

// ---------------------------------------------------------------- 設定
async function settingsView(seq) {
  setFrame("設定", { tab: "settings" });
  if (state.server?.schema?.migrationRequired) {
    mount(section("データベースの更新が必要です", button("更新画面へ", () => (location.hash = "#/migrate"), "primary")), backupSection(false));
    return;
  }
  const data = await api("/api/settings");
  if (stale(seq)) return;
  const admin = data.is_admin;
  const s = data.settings;
  const labels = {
    max_pages_per_site: ["1 社あたりの最大ページ数", "1〜15"], min_interval_sec: ["同じサイトへのアクセス間隔（秒）", "2 秒以上"],
    global_concurrency: ["全体の同時アクセス数", "1〜3"], timeout_sec: ["1 ページの待ち時間（秒）", "5〜30"],
    max_bytes: ["1 ページの最大サイズ（バイト）", "10 万〜200 万"], cache_hours: ["再調査しない期間（時間）", "0〜720"],
    firecrawl_monthly_credit_cap: ["Firecrawl の月間上限（クレジット）", "0〜1000"], firecrawl_max_results_per_run: ["Firecrawl の 1 回の最大件数", "1〜50"],
  };
  const inputs = {};
  const crawlForm = h("form", { class: "stack", onsubmit: async (e) => {
    e.preventDefault();
    const body = {};
    for (const [k, el] of Object.entries(inputs)) body[k] = el.value;
    await act("/api/settings", body, () => route());
  } }, Object.entries(labels).map(([k, [l, hint]]) => {
    inputs[k] = h("input", { type: "number", inputmode: "numeric", value: s[k], disabled: !admin });
    return field(l, inputs[k], hint);
  }), admin ? h("button", { type: "submit", class: "btn primary" }, "保存") : h("p", { class: "hint" }, "変更は管理者だけができます。"));

  const fcKey = h("input", { type: "password", autocomplete: "off", placeholder: data.firecrawl_key_set ? "登録済み（変更する場合だけ入力）" : "fc-…" });
  const fcSection = section("Firecrawl（任意）",
    h("p", { class: "hint" }, "無料枠のある外部の検索サービスです。カード登録が必要なプランは使わないでください。キーはバックアップ・CSV に含めません。"),
    h("p", null, `状態：${data.firecrawl_key_set ? "登録済み" : "未登録"}${data.firecrawl_key_in_env ? "（Cloudflare 側で設定）" : ""}・今月の使用 ${data.firecrawl_used} クレジット`),
    admin && !data.firecrawl_key_in_env ? h("div", { class: "stack" }, field("API キー", fcKey),
      h("div", { class: "row wrap" },
        button("登録", () => act("/api/settings/firecrawl-key", { api_key: fcKey.value }, () => route()), "primary"),
        data.firecrawl_key_set ? button("削除", () => act("/api/settings/firecrawl-key", { clear: true }, () => route()), "danger") : null)) : null);

  const keyOut = h("div");
  const showKey = (r, username) => {
    if (!r?.key) return;
    keyOut.replaceChildren(h("div", { class: "keyshow" }, h("p", null, `${username} さんのログイン用キー（この画面でしか表示されません）`),
      h("p", { class: "keybox" }, r.key), copyBtn(r.key, "キーをコピー")));
  };
  const newUser = h("input", { type: "text", autocapitalize: "none", spellcheck: "false", placeholder: "英数字" });
  const newRole = select("role", [["member", "一般"], ["admin", "管理者"]], "member");
  const usersSection = admin ? section("利用者",
    h("p", { class: "hint" }, "友人と共有する場合は、ここで利用者を作り、表示されたキーを本人だけに伝えてください。"),
    h("ul", { class: "ulist" }, data.users.map((u) => h("li", null,
      h("div", null, h("strong", null, u.username), " ", chip(u.role === "admin" ? "管理者" : "一般"), u.disabled ? chip("無効", "ng") : null,
        h("div", { class: "muted small" }, "最終ログイン " + fmtDate(u.last_login_at))),
      h("div", { class: "row wrap" },
        button("キーを再発行", () => {
          if (confirm(`${u.username} さんのキーを再発行します。以前のキーは使えなくなります。`)) act(`/api/users/${u.id}/reset`, {}, (r) => showKey(r, u.username));
        }, "small"),
        u.username !== state.user.username ? button(u.disabled ? "有効にする" : "無効にする", () => act(`/api/users/${u.id}/${u.disabled ? "enable" : "disable"}`, {}, () => route()), "small") : null)))),
    keyOut,
    h("div", { class: "stack" }, field("新しい利用者の名前", newUser), field("権限", newRole),
      button("利用者を作成", () => act("/api/users", { username: newUser.value, role: newRole.value }, (r) => showKey(r, newUser.value)), "primary"))) : null;

  const seed = data.seed;
  const seedSection = section("初期データ（確認済みの英国販売店 30 社）",
    h("p", null, `読み込み済み ${seed.done} / ${seed.total}${data.seed_imported_at ? "（" + fmtDate(data.seed_imported_at) + "）" : ""}`),
    seed.done < seed.total ? button("続きを読み込む", () => seedLoop(), "primary")
      : button("不足分を読み込み直す", () => act("/api/seed/restart", {}, () => seedLoop())),
    h("p", { class: "hint" }, "既にある販売店（編集したものを含む）は変更しません。"));

  mount(
    section("ログイン中", h("p", null, `${state.user.username}（${admin ? "管理者" : "一般"}）`),
      button("ログアウト", async () => {
        await act("/api/logout", {});
        state.user = null;
        state.server = null;
        stopPoll();
        location.hash = "#/login";
      })),
    backupSection(admin),
    section("調査の設定", h("p", { class: "hint" }, "相手のサイトに負担をかけないための上限です。robots.txt は常に守ります。"), crawlForm),
    fcSection, usersSection, seedSection,
    section("このアプリについて", h("p", { class: "small" }, `データベースの版 ${data.schema_version}（最新 ${data.latest_version}）`),
      h("p", { class: "small" }, "メールの作成・送信機能はありません。問い合わせフォームへの自動入力・送信もしません。")),
  );
}

// ---------------------------------------------------------------- バックアップ・復元
function backupSection(admin) {
  const fileIn = h("input", { type: "file", accept: ".json,application/json" });
  const saved = h("input", { type: "checkbox" });
  const progress = h("p", { class: "muted small" });
  return section("バックアップ",
    h("p", { class: "hint" }, "販売店・連絡方法・根拠・変更履歴・候補を 1 つのファイルに保存します。ログイン情報と API キーは含みません。"),
    button("バックアップを作成して保存", () => makeBackup(progress), "primary"),
    progress,
    admin ? h("details", null, h("summary", null, "バックアップから復元"),
      h("div", { class: "stack" },
        h("p", { class: "error-text" }, "現在のデータはすべてバックアップの内容に置き換わります。"),
        h("label", { class: "check" }, saved, "現在のデータのバックアップを保存しました"),
        field("バックアップファイル（.json）", fileIn),
        button("復元する", () => restore(fileIn, saved, progress), "danger"))) : null);
}

async function makeBackup(progressEl) {
  const say = (t) => (progressEl ? (progressEl.textContent = t) : toast(t));
  try {
    const info = await api("/api/backup/info");
    const tables = {};
    for (const t of Object.keys(info.counts)) {
      tables[t] = [];
      let after = 0;
      for (;;) {
        say(`${t} を取り出しています… ${tables[t].length} / ${info.counts[t]}`);
        const r = await api(`/api/backup/rows?table=${encodeURIComponent(t)}&after=${after}`);
        tables[t].push(...r.rows);
        if (r.next === null) break;
        after = r.next;
      }
    }
    const manifest = { format: info.format, app: info.app, app_version: info.app_version, schema_version: info.schema_version, created_at: info.created_at, counts: info.counts, note: info.note };
    const blob = new Blob([JSON.stringify({ manifest, tables })], { type: "application/json" });
    const stamp = info.created_at.replace(/[-:]/g, "").slice(0, 15);
    say(`作成しました（${Math.round(blob.size / 1024)} KB）`);
    await saveBlob(blob, `sdr-backup-${stamp}.json`);
  } catch (e) {
    say("バックアップを作成できませんでした：" + e.message);
    toast(e.message, true);
  }
}

async function restore(fileIn, savedCb, progressEl) {
  if (!savedCb.checked) return toast("先に現在のデータのバックアップを保存してください", true);
  const f = fileIn.files?.[0];
  if (!f) return toast("バックアップファイルを選んでください", true);
  if (!confirm("現在のデータを置き換えます。よろしいですか？")) return;
  try {
    const data = JSON.parse(await f.text());
    if (!data?.manifest || !data?.tables) throw new Error("バックアップファイルの形式ではありません");
    await api("/api/restore/begin", { body: { current_backup_saved: true, manifest: data.manifest } });
    for (const [t, rows] of Object.entries(data.tables)) {
      for (let i = 0; i < rows.length; i += 200) {
        progressEl.textContent = `${t} を読み込んでいます… ${Math.min(i + 200, rows.length)} / ${rows.length}`;
        await api("/api/restore/rows", { body: { table: t, rows: rows.slice(i, i + 200) } });
      }
    }
    const r = await api("/api/restore/commit", { body: { counts: data.manifest.counts } });
    progressEl.textContent = r.message;
    toast(r.message);
  } catch (e) {
    progressEl.textContent = "復元できませんでした（データは変更していません）：" + e.message;
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------- 起動
window.addEventListener("hashchange", () => route());
route().then(() => {
  if (state.user) schedulePoll(800);
}).catch(() => {});
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}
