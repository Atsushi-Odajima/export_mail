# 検証結果と残る制限（2026-10-24）

区分の意味：**実装済み**＝コードがある／**実機確認済み**＝実在サイト・実際の画面・実行ファイルで動かして確認／**試験で確認**＝自動試験（`python -m pytest tests`、架空データ）で確認／**未検証**＝動かして確認していない。

開発環境：Linux コンテナ（Python 3.12）。Windows 実機はなく、Windows の確認は GitHub Actions の Windows ランナー（windows-latest）で行います（第 4 節）。

## 1. 自動試験（71 件・すべて成功）

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

（GitHub Actions の結果を追記）

## 5. 残る制限

- 分類・概要はルールによる判断で、確認したページ（既定 1 サイト最大 10 ページ）の範囲に限られます。JavaScript で表示されるサイトは内容を読めないことがあります（Playwright 等のブラウザー取得は未搭載）。
- 「確認した在庫からの傾向」は在庫ページの車名・年式・価格表示の数から判断する簡易なもので、全在庫は調べていません。
- 店名の自動取得は構造化データ・サイト名・タイトルから選ぶため、支店名や宣伝文になることがあります（照合で 3 件修正）。所在地も周辺の文字が混ざることがあり、照合で 8 件整形しました。
- 所在地は英国の郵便番号が見つかった場合だけ地域を出します。郵便番号エリアの代表都市名で表示するため、実際の町名とは異なることがあります（概要には郵便番号を記載）。
- 共有モードは小規模（2 人程度）を想定。PC 停止・スリープ・回線切断中は使えません。
- 実行ファイルには署名がないため、Windows で初回に警告が出ることがあります。
