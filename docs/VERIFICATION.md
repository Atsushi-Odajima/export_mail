# 検証結果と残る制限（2026-10-24）

区分の意味：**実装済み**＝コードがある／**実機確認済み**＝実在サイト・実際の画面・実行ファイルで動かして確認／**試験で確認**＝自動試験（`python -m pytest tests`、架空データ）で確認／**未検証**＝動かして確認していない。

開発環境：Linux コンテナ（Python 3.12）。Windows 実機はなく、Windows の確認は GitHub Actions の Windows ランナー（windows-latest）で行います（第 4 節）。

## 1. 自動試験（72 件・Linux と Windows の両方ですべて成功）

| 仕様の検証項目 | 試験 |
|---|---|
| 店舗の公開メールと制作会社・求人・個人情報窓口・自動送信の区別、別ドメインは要確認 | `test_shop_email_vs_agency_recruitment_privacy`、`test_research_full_site` |
| 画像名（`logo@2x.png`）等をメールと誤認しない、保護表示（Cloudflare）の復号 | `test_image_names_and_placeholders_are_not_emails`、`test_decode_cfemail` |
| 問い合わせフォームと検索欄・ログイン・ニュースレター・在庫検索の区別、外部埋め込みの検出、ページのみはフォーム未確認 | `test_contact_form_distinguished_from_search_login_newsletter`、`test_embedded_form_provider_detected`、`test_contact_page_without_form_is_not_confirmed` |
| 専門店表記と在庫傾向の区別、SUV が数台あるだけで SUV 専門にしない、不明は要確認 | `test_official_specialist_claim`、`test_few_suvs_do_not_make_suv_specialist`、`test_stock_trend_without_specialist_claim`、`test_unknown_stays_review`、`test_marque_specialist_is_sports_official`、`test_car_and_van_dealer_is_not_van_specialist`、`test_match_does_not_cross_sentences` |
| 概要は確認した事実だけ、全在庫未確認を明記 | `test_summary_only_states_found_facts` |
| アイルランド・英国外の住所を英国扱いしない（北アイルランドは英国） | `test_ireland_not_treated_as_uk`、`test_foreign_structured_address_flagged`、`test_outward_postcode_and_times_sign` |
| 重複登録の防止、似た社名は自動統合せず確認待ち | `test_duplicate_domain_not_registered_twice`、`test_similar_name_goes_to_review_not_merged` |
| 取得失敗の理由の記録、失敗時に既存データを消さない | `test_research_failure_reason_recorded`、`test_failed_research_keeps_previous_data` |
| 途中停止・再開・再起動時の中断復旧 | `test_job_runs_stops_and_resumes`（実行中の 1 件の停止を含む）、`test_restart_recovers_interrupted`、`test_stop_during_research_keeps_partial` |
| 手動修正の保持、店名・URL 変更や再調査で販売店 ID・連絡方法 ID を維持、複数の連絡方法と根拠が正しい販売店に関連付く、古い連絡方法を消さない | `test_id_kept_and_manual_values_survive_reresearch`、`test_multiple_contacts_per_dealer_and_manual_person` |
| バックアップ・復元で ID・手動確定値・根拠・履歴を保持、APIキーを含めない、他形式の ZIP を拒否 | `test_backup_and_restore_keep_ids_manual_evidence_history`、`test_restore_rejects_foreign_zip` |
| DB 更新：試験用の版 2（列追加）で既存データ保持・更新前バックアップ、失敗時は巻き戻し、新しすぎる DB は開かない | `test_migration_adds_column_and_keeps_data`、`test_failed_migration_rolls_back`、`test_newer_database_refused` |
| 内部 URL の拒否（localhost・内部 IP・169.254.169.254・file:・認証情報付き・非標準ポート・.local 等）、名前解決後の内部アドレス、内部への転送 | `test_internal_urls_rejected`（14 パターン）、`test_dns_resolution_to_private_address_rejected`、`test_redirect_to_internal_address_rejected` |
| robots.txt の遵守、CAPTCHA を回避しない、応答サイズ上限、同一サイトの取得間隔 | `test_robots_disallow_respected_and_captcha_not_bypassed`、`test_size_limit_truncates`、`test_interval_between_requests_to_same_host` |
| 取得文字列の安全な表示（script・属性注入・javascript: リンク）、CSP | `test_external_text_is_escaped` |
| CSRF・別サイトからの POST 拒否・Host ヘッダー確認 | `test_post_requires_csrf_and_manual_edit_saved`、`test_host_header_checked` |
| 共有モード：未ログインで一覧・CSV・API・調査実行に到達できない、ログイン・ログアウト、Cookie 属性、連続失敗の制限、パスワードのハッシュ保存 | `test_shared_requires_login`、`test_shared_login_logout_and_cookie_flags`、`test_login_lockout`、`test_password_storage_and_policy` |
| CSV：BOM・CRLF・数式の無効化、取込（UTF-8/Shift_JIS/見出しなし） | `test_csv_export_is_excel_safe`、`test_csv_import_parsing` |
| 初期データの読込（空の DB へ追加、既存の利用者データを上書きしない） | `test_seed_import_into_empty_db`、`test_seed_import_does_not_overwrite` |
| 候補発見：OSM 応答の読み取り・新車専売の除外・予備接続先、Firecrawl の応答・残高不足で停止・月間上限で実行前に停止、候補の重複除外と登録 | `tests/test_discovery.py` |

## 2. 実機確認済み（この開発環境で実際に動かした）

| 確認内容 | 結果 |
|---|---|
| 実在の英国販売店サイトの調査（初期データ） | 62 サイト（重複除く）を試し、30 社を初期データとして登録・照合（docs/INITIAL_DATA_REPORT.md）。公開メールまたはフォームを確認できたのは 28 社（メール 23・フォーム 21）。取得不可・除外 32 サイトは理由付きで `data/unreachable_or_excluded.csv` に記録 |
| アプリ自身による新規候補の発見（初期データとは別に検証） | 画面の「候補発見」から OpenStreetMap（`overpass.atownsend.org.uk`）で Leeds 地域を実行 → 13 件の候補を取得 → 候補一覧から 4 件を「登録して調査開始」→ 4 件とも調査完了（3 件でフォーム、2 件で公開メールを確認） |
| 実際の画面（Chromium、`scripts/screen_check.js`） | 初期データ 30 件の一覧表示、絞り込み（メールあり＋フォームあり 16 件、クラシックカー 3 件）、絞り込み結果の CSV 出力（BOM 付き）、詳細画面（出典リンク 16 件）、手動編集の保存、再調査の開始、候補発見・進捗・設定画面の表示 |
| 新しいデータフォルダーでの初回起動 | 初期データ 30 件が自動で読み込まれることを確認 |
| ボット対策への対応 | HTTP 403・Cloudflare の確認ページ・JavaScript のボット確認ページを回避せず「取得失敗（理由）」として記録 |
| 実行ファイル（PyInstaller、Linux 版で設定を確認） | Python のパスを外した環境で起動 → 画面表示 → 「アプリを終了」で停止し、プロセスが残らないこと |

## 3. 未検証

| 項目 | 理由・補足 |
|---|---|
| Firecrawl 検索の実通信 | APIキーがないため。応答の読み取り・上限・残高不足の扱いは試験で確認 |
| Overpass の主インスタンス `overpass-api.de` | この環境の回線から接続できない（英国向けインスタンスで実機確認済み） |
| 共有モードを HTTPS 経路（Cloudflare Tunnel 等）越しに 2 人で使う運用 | 外部公開・サービス契約を行わないため。認証・CSRF・Cookie 属性は試験で確認 |
| 利用者の家庭・会社回線での取得結果 | 本環境（クラウドの回線）で 403 になったサイトが、利用者の回線では取得できる可能性がある（回避はしない） |
| 将来のメール機能（作成・送信・予約・返信管理）と外部メールサービス・外部AI | 今回は実装していない（FUTURE_EMAIL_PLAN.md） |

## 4. Windows 配布版

GitHub Actions の Windows ランナー（windows-latest、Windows Server・64bit、PowerShell 7）で実行：
https://github.com/Atsushi-Odajima/export_mail/actions/runs/40080941012 （2026-10-24、成功）

| 手順 | 結果 |
|---|---|
| 固定版の依存関係の導入・試験 72 件 | 成功 |
| PyInstaller でフォルダー一式（`SamuraiDealerResearch.exe`）を作成 | 成功 |
| PATH から Python を外して exe を起動 → `/healthz` 応答 → 一覧画面に「販売店一覧」 | 成功 |
| 初期データの読込（CSV 出力の行数） | 30 件 |
| データフォルダー（`--data-dir`）に `dealers.sqlite3` が作られる | 成功 |
| 画面の「アプリを終了」（CSRF トークン付き POST）→ プロセス終了・サーバー応答なし | 成功（終了を確認） |
| 再起動後もデータが残る（件数一致） | 成功 |
| 配布 ZIP の作成 | `SamuraiDealerResearch-windows.zip`（19.9MB、成果物として 30 日保存。上記ページの Artifacts から取得） |

未確認：Windows 10/11 のデスクトップ PC での実際の操作（ダブルクリック起動・既定ブラウザーが開くこと・SmartScreen の表示）。ランナーはサーバー版 Windows で、ブラウザーの自動起動は無効（`--no-browser`）にして確認しています。

## 5. 残る制限

- 分類・概要はルールによる判断で、確認したページ（既定 1 サイト最大 10 ページ）の範囲に限られます。JavaScript で表示されるサイトは内容を読めないことがあります（Playwright 等のブラウザー取得は未搭載）。
- 「確認した在庫からの傾向」は在庫ページの車名・年式・価格表示の数から判断する簡易なもので、全在庫は調べていません。
- 店名の自動取得は構造化データ・サイト名・タイトルから選ぶため、支店名や宣伝文になることがあります（照合で 3 件修正）。所在地も周辺の文字が混ざることがあり、照合で 8 件整形しました。
- 所在地は英国の郵便番号が見つかった場合だけ地域を出します。郵便番号エリアの代表都市名で表示するため、実際の町名とは異なることがあります（概要には郵便番号を記載）。
- 共有モードは小規模（2 人程度）を想定。PC 停止・スリープ・回線切断中は使えません。
- 実行ファイルには署名がないため、Windows で初回に警告が出ることがあります。

---

## 6. iPhone 版（`mobile/`：Cloudflare Workers ＋ D1）

### 自動試験（`cd mobile && npm test`：52 件すべて成功、型チェック成功）

本番と同じ手順でバンドルした Worker を workerd（Miniflare 4）で動かし、D1 もローカルの実物を使います。外部通信は架空サイト（`.example`）に差し替えています。

- 読み取り・判定（HTMLRewriter）：mailto・本文・保護表示メール、フォームの種類、郵便番号、構造化データ。PC 版と同じ架空サイトで分析結果が一致
- 安全な取得：内部・予約済み IP（DNS 解決後とリダイレクト後を含む）の拒否、robots.txt、サイトごとの間隔
- API：初回設定（SETUP_TOKEN なし・短いトークンでは不可）、ログイン失敗のロック、CSRF・別サイトからの操作拒否、調査ジョブの開始・停止・再開、手動値が再調査で上書きされないこと、CSV（BOM 付き・式の無害化）、バックアップ→復元、初期データ 30 社の読み込み
- データベースの版：版 2 を足しても既存データが残ること、失敗した版は巻き戻ること

### 実機確認済み（この開発環境で実際に動かした）

| 確認 | 結果 |
|---|---|
| 実在の英国販売店 8 社を Worker の調査処理（workerd 上）で調査（`LIVE=1 npx vitest run test/live.test.ts`） | 8 社とも完了。公開メールは PC 版で確認済みの値と 8 社すべて一致（woodhallcars・ascarsleedsltd・burnsidecarstore・gmpcars・ng9motors・heritageclassic・911virgin・mikehardinglandrovers）。分類も PC 版と同じ。初回は 911virgin.com のみ接続タイムアウトで「取得失敗（理由付き）」になり、再実行で完了 |
| 8 社の所要時間（画面を開いている状態に相当する連続実行） | 約 72 秒 |
| `wrangler dev` で画面を iPhone 13 の画面サイズ・タッチ操作で表示（Chromium の端末エミュレーション） | 初回設定→ログイン用キー表示→初期データ 30 社の読み込み→一覧（絞り込み「メールあり」23 件）→詳細→候補→追加→進捗→設定→バックアップ保存→ログアウト→キーで再ログイン、詳細での編集→バックアップから復元（編集前に戻る）、内部アドレス・掲載サイトの登録拒否。画面のエラー 0 件 |
| 定期実行（Cron）の処理を `wrangler dev` で呼び出し | 成功 |

### 本番環境での確認（2026-10-26、Cloudflare に代行して公開）

| 確認 | 結果 |
|---|---|
| 公開（`wrangler deploy`）と D1 の作成 | 成功。https://samurai-dealer-research.sdr-l2h8d6.workers.dev 。API トークンに D1 作成の権限がなかったため、D1 は Cloudflare の接続プラグインで作成（地域ヒント APAC） |
| SETUP_TOKEN の登録→管理者の作成→初期データ 30 社の読み込み | 成功（メール 23 社・フォーム 21 社・人が確認済み 29 社） |
| 画面を開いた状態に相当する調査（woodhallcars.co.uk、7 ページ） | 約 45 秒で完了、公開メールは PC 版と一致 |
| 画面を閉じた状態（1 分ごとの定期実行のみ）の調査（gmpcars.co.uk、8 ページ） | 約 11 分で完了（1 分に約 1 ページ）、公開メールは PC 版と一致。定期実行のログはすべて成功（CPU 上限超過なし） |
| 名前ごとの失敗制限を追加して公開し直し、admin のキーを利用者指定の 4 桁に変更（2026-10-26） | 旧キーは 401、新しいキーでログインでき 30 社を表示。自動試験 53 件成功 |
| 本番の画面に iPhone 13 の画面サイズでログイン（Chromium の端末エミュレーション） | 一覧 30 社を表示、画面のエラー 0 件 |

### 未検証

- 実際の iPhone の Safari（WebKit）での表示・操作、ホーム画面への追加、共有シートでのファイル保存（この環境には WebKit・実機がないため、Chromium の iPhone エミュレーションで代替）
- ダッシュボードの「リポジトリをインポート」による公開と、GitHub への push での自動更新（今回はコマンドで公開したため未使用）
- 多数の販売店をまとめて調べたときの本番での所要時間・無料枠の消費量
- OpenStreetMap・Firecrawl による候補発見の Worker からの実通信（処理は PC 版と同じ方式で、PC 版では OpenStreetMap を実通信で確認済み。Worker 版は架空の応答で試験）

### 残る制限（iPhone 版）

- 調査は「1 回 1 ページ」に分けて進めるため、アプリを閉じている間は 1 分ごとの定期実行でゆっくり進みます。
- Cloudflare の通信元から 403・チャレンジ画面を返すサイトは、PC 版と同様に回避せず「取得失敗」として理由を残します（通信元が異なるため、PC 版と結果が異なるサイトがありえます）。
- 一覧は全件を読み込んで絞り込むため、販売店が数千件規模になると D1 の読み取り量が増えます（無料枠 1 日 500 万行）。
