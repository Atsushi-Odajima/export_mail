// CSV の入出力（CSV は入出力用。正本はデータベース）。
// 出力：UTF-8（BOM 付き）・CRLF。= + - @ タブ 改行 で始まる値は先頭に ' を付け、Excel で数式として実行させない。

type Row = Record<string, any>;

const FORMULA_PREFIX = /^[=+\-@\t\r\n＝＋－＠]/;

export function safeCell(v: unknown): string {
  if (v === null || v === undefined) return "";
  const t = String(v);
  return FORMULA_PREFIX.test(t) ? "'" + t : t;
}

export function toCsv(header: string[], rows: unknown[][]): string {
  const q = (v: unknown) => '"' + safeCell(v).replace(/"/g, '""') + '"';
  return "﻿" + [header.map(q).join(","), ...rows.map((r) => r.map(q).join(","))].join("\r\n") + "\r\n";
}

export function dealersCsv(dealers: Row[]): string {
  const header = ["販売店ID", "店名", "公式サイト", "地域", "所在地", "分類", "分類根拠", "分類で確認した範囲", "日本語概要", "公開メール（確認済み）",
    "要確認のメール", "問い合わせフォーム", "問い合わせページ（フォーム未確認等）", "電話", "連絡方法の状態", "中古車販売の確認", "英国内の確認",
    "調査状態", "取得失敗の理由", "最終調査日時(UTC)", "人の確認状態", "人の確認日時(UTC)", "重複確認", "本体の販売店ID", "データ区分", "メモ"];
  const rows = dealers.map((d) => [
    d.id, d.name, d.website_url, d.region, d.address, d.categories.join("・"), d.category_basis, d.category_scope_auto, d.summary,
    d.emails_ok.map((c: Row) => c.value).join(" "), d.emails_review.map((c: Row) => c.value).join(" "), d.forms_ok.map((c: Row) => c.value).join(" "),
    d.contact_pages.map((c: Row) => `${c.value}（${c.status}）`).join(" "), d.phones.map((c: Row) => c.value).join(" / "), d.contact_summary,
    d.used_sales, d.uk_check_auto, d.research_status, d.failure_reason, d.last_researched_at, d.review_status, d.reviewed_at,
    d.duplicate_status, d.parent_dealer_id, d.data_origin, d.notes,
  ]);
  return toCsv(header, rows);
}

export function contactsCsv(dealers: Row[]): string {
  const header = ["連絡方法ID", "販売店ID", "店名", "種類", "値", "用途", "状態", "状態は人が確定", "除外・確認理由", "担当者名", "役職", "取得元URL",
    "根拠の抜粋", "取得経路", "初回観測(UTC)", "最終観測(UTC)", "人の確認日時(UTC)", "最新の調査で確認"];
  const rows: unknown[][] = [];
  for (const d of dealers) {
    for (const c of d.contacts) {
      rows.push([c.id, d.id, d.name, c.kind_label, c.value, c.purpose, c.status, c.status_is_manual ? "はい" : "", c.exclusion_reason, c.person_name,
        c.person_role, c.source_url, c.excerpt, c.origin, c.first_observed_at, c.last_observed_at, c.human_verified_at,
        c.is_current ? "はい" : "いいえ（古い値・履歴として保持）"]);
    }
  }
  return toCsv(header, rows);
}

export function evidenceCsv(rows: Row[], names: Map<string, string>): string {
  return toCsv(["販売店ID", "店名", "項目", "出典URL", "根拠の抜粋", "観測日時(UTC)", "取得経路", "調査ID"],
    rows.map((e) => [e.dealer_id, names.get(e.dealer_id) || "", e.topic, e.source_url, e.excerpt, e.observed_at, e.origin, e.research_run_id]));
}

// ---------------------------------------------------------------- 取込
const URL_HEADERS = new Set(["url", "website", "website_url", "公式サイト", "公式サイトurl", "サイト", "ホームページ"]);
const NAME_HEADERS = new Set(["name", "店名", "販売店名", "会社名"]);
const REGION_HEADERS = new Set(["region", "地域", "所在地", "town", "city"]);
const NOTE_HEADERS = new Set(["note", "notes", "メモ", "備考"]);
export const MAX_IMPORT_ROWS = 500;

export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let inQ = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQ) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          cell += '"';
          i++;
        } else inQ = false;
      } else cell += ch;
    } else if (ch === '"') inQ = true;
    else if (ch === ",") {
      row.push(cell);
      cell = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
    } else cell += ch;
  }
  if (cell || row.length) {
    row.push(cell);
    rows.push(row);
  }
  return rows;
}

export function parseImport(text: string): { rows: { line: number; url: string; name: string; region: string; note: string }[]; errors: string[] } {
  const all = parseCsv(text.replace(/^﻿/, ""));
  if (!all.length) return { rows: [], errors: ["空のファイルです"] };
  const header = all[0].map((h) => h.trim().replace(/^'/, "").toLowerCase());
  const col = (set: Set<string>) => {
    const i = header.findIndex((h) => set.has(h));
    return i >= 0 ? i : null;
  };
  let iu = col(URL_HEADERS);
  const [iname, ireg, inote] = [col(NAME_HEADERS), col(REGION_HEADERS), col(NOTE_HEADERS)];
  let body = all.slice(1);
  let firstLine = 2;
  if (iu === null) {
    iu = 0;
    body = all;
    firstLine = 1;
  }
  const errors: string[] = [];
  const rows = [];
  for (let n = 0; n < Math.min(body.length, MAX_IMPORT_ROWS); n++) {
    const r = body[n];
    const get = (i: number | null) => (i !== null && i < r.length ? r[i].trim().replace(/^'/, "") : "");
    if (!get(iu)) continue;
    rows.push({ line: n + firstLine, url: get(iu), name: get(iname), region: get(ireg), note: get(inote) });
  }
  if (body.length > MAX_IMPORT_ROWS) errors.push(`${MAX_IMPORT_ROWS} 行を超えた分は読み込みませんでした`);
  return { rows, errors };
}
