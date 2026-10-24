// 開発用：実際のブラウザー（Playwright/Chromium）で画面操作を確認し、スクリーンショットを保存する。
// 使い方（アプリ起動中）: node scripts/screen_check.js http://127.0.0.1:8765 出力フォルダー
// 確認内容: 一覧・絞り込み・CSV出力・詳細（根拠・出典リンク）・手動編集の保存・再調査の開始。
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

(async () => {
  const [base, out] = process.argv.slice(2);
  fs.mkdirSync(out, { recursive: true });
  const r = [];
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, acceptDownloads: true, locale: "ja-JP" });
  page.on("dialog", (d) => d.accept());
  await page.goto(base + "/");
  r.push(`一覧: ${await page.locator("table.list tbody tr input[name=dealer_ids]").count()} 行`);
  await page.screenshot({ path: path.join(out, "01_list.png") });

  await page.check("input[name=has_email]");
  await page.check("input[name=has_form]");
  await page.click("form.filters button");
  r.push(`絞り込み（メールあり・フォームあり）: ${await page.locator("table.list tbody tr input[name=dealer_ids]").count()} 行`);
  await page.screenshot({ path: path.join(out, "02_filtered.png") });

  await page.uncheck("input[name=has_email]");
  await page.uncheck("input[name=has_form]");
  await page.selectOption("select[name=category]", "クラシックカー");
  await page.click("form.filters button");
  r.push(`絞り込み（クラシックカー）: ${await page.locator("table.list tbody tr input[name=dealer_ids]").count()} 行`);
  const [dl] = await Promise.all([page.waitForEvent("download"), page.click("text=この一覧をCSV出力")]);
  const csvPath = path.join(out, "export_classic.csv");
  await dl.saveAs(csvPath);
  const buf = fs.readFileSync(csvPath);
  r.push(`CSV出力: ${buf.length} バイト・BOM ${buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf}・行数 ${buf.toString("utf8").trim().split("\r\n").length - 1}`);

  await page.goto(base + "/");
  await page.locator("table.list tbody tr a").first().click();
  await page.waitForLoadState();
  r.push("詳細: " + (await page.locator("h1").innerText()));
  r.push(`詳細: 出典リンク ${await page.locator("text=出典を開く").count()} 件`);
  await page.screenshot({ path: path.join(out, "03_detail.png"), fullPage: true });

  await page.fill("textarea[name=notes]", "画面確認テストのメモ");
  await page.click("form[action$='/edit'] button");
  await page.waitForLoadState();
  r.push("手動編集: " + ((await page.content()).includes("画面確認テストのメモ") ? "保存を確認" : "保存されていない"));
  await page.click("form[action$='/research'] button");
  await page.waitForLoadState();
  r.push("再調査: " + ((await page.content()).includes("再調査を開始しました") ? "開始を確認" : "開始メッセージなし"));

  for (const [p, f] of [["/discover", "04_discover.png"], ["/candidates?status=", "05_candidates.png"], ["/jobs", "06_jobs.png"], ["/settings", "07_settings.png"]]) {
    await page.goto(base + p);
    await page.screenshot({ path: path.join(out, f), fullPage: true });
  }
  await browser.close();
  console.log(r.join("\n"));
})();
