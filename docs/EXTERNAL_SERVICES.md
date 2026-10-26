# 外部サービスの確認記録（2026-10-26）

営業メール機能で使う外部サービスの仕様を、**公式ページで確認した内容**と、それに合わせたアプリの作りを記録します。
確認日はすべて **2026-10-26** です。公式ページで確かめられなかったものは「**未確認**」と書いています。料金・上限・規約は変わることがあるため、使い始める前に各公式ページで確認してください。

> この開発環境には利用者のメールアカウント・API キーがないため、**実際のサービスへの接続（送信・受信・認証・AI 呼び出し）は行っていません**。下の「アプリの対応」は、公式仕様どおりの応答を模した試験（`tests/test_connectors.py`）で確認したものです。

---

## 1. Gmail API

| 項目 | 公式の内容 | 出典 |
|---|---|---|
| デスクトップアプリの認証 | `http://127.0.0.1:ポート` へのループバックのリダイレクトはデスクトップ用クライアントで引き続き使える（廃止は Android・iOS・Chrome アプリ向けのみ）。PKCE（S256 推奨）に対応 | https://developers.google.com/identity/protocols/oauth2/native-app |
| 認証・トークンの URL | `https://accounts.google.com/o/oauth2/v2/auth`、`https://oauth2.googleapis.com/token` | 同上 |
| 更新トークンの期限 | 同意画面が「テスト」状態の外部アプリでは、更新トークンは **7 日で失効**（名前・メール・プロフィールだけのスコープを除く） | https://developers.google.com/identity/protocols/oauth2 |
| 更新トークンの数 | 1 つの Google アカウント・1 つのクライアント ID あたり最大 100 個。超えると古いものから無効 | 同上 |
| テストユーザー | 「テスト」状態では最大 100 人 | https://developers.google.com/identity/protocols/oauth2/production-readiness/overview |
| スコープの区分 | `gmail.send` は「機密（Sensitive）」、`gmail.readonly`・`gmail.modify` は「制限付き（Restricted）」 | https://developers.google.com/workspace/gmail/api/auth/scopes |
| 審査 | 制限付きスコープは審査が必要。セキュリティ評価は「制限付きスコープのデータをサーバーに保存（または送信）する場合」のみ。**個人利用（100 人未満）は審査不要**で、「未確認のアプリ」の警告を通って使う（審査までは 100 人まで） | 同上、https://support.google.com/cloud/answer/13464323 、https://support.google.com/cloud/answer/7454865 |
| 送信 | `POST https://gmail.googleapis.com/gmail/v1/users/{userId}/messages/send`。`raw` は RFC 2822 形式を base64url にしたもの。返信をスレッドにするには `threadId` と `References`／`In-Reply-To`、件名の一致が必要 | https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send 、https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages |
| 差分の取得 | `users.history.list`（`startHistoryId` 必須）。古い・無効な履歴 ID は **通常 HTTP 404** で、その場合は全件の取り直しが必要。履歴 ID は通常 1 週間以上有効だが数時間のこともある | https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list 、https://developers.google.com/workspace/gmail/api/guides/sync |
| 開始点 | `users.getProfile` が現在の `historyId` を返す | https://developers.google.com/workspace/gmail/api/reference/rest/v1/users/getProfile |
| 検索 | `messages.list` の `q` は Gmail の検索構文。`rfc822msgid:` は公式の検索演算子 | https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list 、https://support.google.com/mail/answer/7190 |
| 利用枠（2026-05-01 改定） | 1 分あたり：利用者・プロジェクトごとに 6,000 単位、プロジェクト全体で 1,200,000 単位。`messages.send` 100 単位、`messages.get` 20 単位（raw 等は 60）、`messages.list` 5、`history.list` 2、`getProfile` 1。1 通の宛先は API で 500 まで。旧来の「1 秒あたり 250 単位」は**未確認** | https://developers.google.com/workspace/gmail/api/reference/quota |
| 1 日の送信上限 | Google Workspace（有料）：1 人 1 日 2,000 通（試用中 500）。**個人の Gmail の上限は未確認**（公式ページに数値の記載なし。よく見る「1 日 500 通」は第三者の情報のみ） | https://knowledge.workspace.google.com/admin/gmail/gmail-sending-limits-in-google-workspace |

**アプリの対応**
- 種類「デスクトップ アプリ」の OAuth クライアントで、PKCE（S256）付きのループバック（`http://127.0.0.1:ポート/oauth/callback`）で認証。保存するのは更新トークンだけ（OS の資格情報保管）。
- スコープは `gmail.send` と `gmail.readonly` のみ（受信の確認のため readonly が必要。メールの変更・削除はしない）。
- 「テスト」状態の 7 日失効は接続画面に表示。更新トークンが使えなくなると接続を「認証エラー」にし、その送信は「送信前に失敗」として保留（再認証後に再開）。
- 送信は `raw`、返信は `threadId`＋`In-Reply-To`／`References` を付ける。
- 受信は `history.list`。404 なら直近の期間（初期値 14 日）を取り直す。
- 結果不明の照合は `rfc822msgid:` で送信済みを検索。

## 2. Microsoft Graph（Outlook.com／Microsoft 365）

| 項目 | 公式の内容 | 出典 |
|---|---|---|
| 認証 | `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize` と `/token`。`{tenant}` は `common`・`organizations`・`consumers`・テナント ID。PKCE 推奨。パブリッククライアントはシークレットを送らない。更新トークンは `offline_access` を要求したときだけ返る。ネイティブアプリの更新トークンに固定の期限はない | https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow |
| リダイレクト URI | `http://localhost` が使え、**照合時にポート番号は無視**される。`127.0.0.1` が推奨だが、ポータルからは入力できずマニフェストで追加が必要。`[::1]` は不可。プラットフォームは「モバイル アプリケーションとデスクトップ アプリケーション」。個人アカウントを許可する場合はリダイレクト URI にクエリ文字列不可 | https://learn.microsoft.com/en-us/entra/identity-platform/reply-url |
| 個人アカウントの権限 | `Mail.Send`・`Mail.ReadWrite` は「委任（個人の Microsoft アカウント）」で使える。`User.Read` が必須かは**未確認** | 下記の各 API ページ |
| 下書き作成 | `POST /me/messages` → **201**（`id`・`internetMessageId` を含む）。`Mail.ReadWrite` が必要 | https://learn.microsoft.com/en-us/graph/api/user-post-messages?view=graph-rest-1.0 |
| 下書きの送信 | `POST /me/messages/{id}/send` → **202**（本文なし）。送信済みアイテムに入る | https://learn.microsoft.com/en-us/graph/api/message-send?view=graph-rest-1.0 |
| 一度に送信 | `POST /me/sendMail` → **202**（本文・ID なし）。202 は「受け付けた」で、配達の意味ではない | https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0 |
| ID | 既定ではフォルダー移動で `id` が変わる。`Prefer: IdType="ImmutableId"` で固定。`internetMessageId` は RFC 2822 形式。`$filter=internetMessageId eq '…'` は**未確認** | https://learn.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0 |
| 差分の取得 | `GET /me/mailFolders/{id}/messages/delta`（フォルダーごと）。`@odata.nextLink` をたどり `@odata.deltaLink` を保存。**410 Gone** は全件の取り直し。期限切れのトークンは `syncStateNotFound` などの 40X エラー | https://learn.microsoft.com/en-us/graph/api/message-delta?view=graph-rest-1.0 、https://learn.microsoft.com/en-us/graph/delta-query-overview |
| 制限 | アプリ・メールボックスごとに 10 分 10,000 要求、同時 4 要求。Exchange Online：1 日 10,000 宛先、1 分 30 通。Outlook.com（Microsoft 365 加入者）：1 日 5,000 宛先、1 通 500 宛先、関係のない宛先は 1 日 1,000。未加入・新規アカウントはさらに少ない | https://learn.microsoft.com/en-us/graph/throttling-limits 、https://learn.microsoft.com/en-us/office365/servicedescriptions/exchange-online-service-description/exchange-online-limits 、https://support.microsoft.com/en-us/outlook/sending-limits-in-outlook-com |

**アプリの対応**
- 「モバイルとデスクトップ」のパブリッククライアントとして、PKCE 付きで `http://localhost:ポート/oauth/callback` に戻す（ポートは照合されないため、アプリのポートが変わっても再登録不要）。
- スコープは `offline_access User.Read Mail.Send Mail.ReadWrite`。
- 送信は **下書き作成（201）→ 送信（202）** の 2 段階。下書きの不変 ID と `internetMessageId` を送信前に記録し、結果不明の照合では、その ID で送信済みアイテムにあるかを確認する。`internetMessageId` での `$filter` は使わない。
- 受信は受信トレイの delta。410／`syncStateNotFound` なら直近の期間を取り直す。

## 3. SMTP／IMAP（パスワード方式）

| 項目 | 公式の内容 | 出典 |
|---|---|---|
| Gmail のサーバー | `imap.gmail.com:993`（SSL）、`smtp.gmail.com` の 465（SSL）または 587（STARTTLS）。IMAP・SMTP とも XOAUTH2 に対応 | https://developers.google.com/workspace/gmail/imap/imap-smtp |
| Gmail のアプリパスワード | 2 段階認証が必要。「推奨しない」とされているが廃止ではない。仕事用アカウント・高度な保護機能・セキュリティキーのみの設定では使えない場合がある | https://support.google.com/accounts/answer/185833 |
| 安全性の低いアプリ | Google Workspace では 2025-05-01 に終了 | https://workspaceupdates.googleblog.com/2023/09/winding-down-google-sync-and-less-secure-apps-support.html |
| 個人の Gmail | ユーザー名とパスワードだけで使う外部アプリには「対応していない」 | https://support.google.com/mail/answer/7126229 |
| Outlook.com | **2024-09-16 に基本認証を廃止**。POP／IMAP は OAuth（先進認証）が必要。SMTP・EAS も OAuth を推奨 | https://support.microsoft.com/en-us/office/modern-authentication-methods-now-needed-to-continue-syncing-outlook-email-in-non-microsoft-email-apps-c5d65390-9676-4763-b41f-d7986499a90d |
| Exchange Online | POP・IMAP・EWS・EAS の基本認証は全テナントで無効。SMTP AUTH の基本認証は当面利用可。「2026 年 12 月末に既定で無効、最終廃止日は 2027 年後半に発表」という日程は二次情報のみで**一部未確認** | https://learn.microsoft.com/en-us/exchange/clients-and-mobile-in-exchange-online/deprecation-of-basic-authentication-exchange-online |

**アプリの対応**
- Gmail は「アプリパスワード」用の設定値を用意。Outlook.com はパスワード方式では接続できない旨を画面に表示し、Graph を案内。
- SMTP は「MAIL → RCPT → DATA」の順に送り、DATA の前に断られたら「送信前に失敗」、DATA の途中で切れたら「結果不明」（自動では再送しない）。
- IMAP は UID と UIDVALIDITY で続きから取り込み、既読にしない（`BODY.PEEK`）。
- SMTP は送信済みフォルダーを検索できないため、結果不明の照合は Gmail（自動で送信済みに入る）の場合だけ IMAP で確認し、それ以外は人が判断する。

## 4. Anthropic Claude API（AI による文面作成・任意）

| 項目 | 公式の内容 | 出典 |
|---|---|---|
| 呼び出し | `POST https://api.anthropic.com/v1/messages`。`x-api-key`・`anthropic-version`・`content-type: application/json` | https://platform.claude.com/docs/en/get-started |
| 版 | 最新の `anthropic-version` は `2026-10-21`（推奨）。`temperature`・`top_p`・`top_k` を送ると 400 | https://platform.claude.com/docs/en/api/versioning |
| エラー | 400・401・402（billing_error）・403・404・413・429（rate_limit_error）・500・504・529（overloaded_error） | https://platform.claude.com/docs/en/api/errors |
| モデルと料金（100 万トークンあたり 入力／出力） | 最上位 $10／$50、高性能（アプリの初期値）$4／$20、中位 $2／$10、小型 $0.10／$0.50（小型は 10 万トークン超で $0.50／$2.50）。モデル名と ID は出典ページを参照 | https://platform.claude.com/docs/en/about-claude/models/overview 、https://platform.claude.com/docs/en/about-claude/pricing |
| 無料枠 | **なし**（新規利用者には試用のための少額のクレジットがある） | https://platform.claude.com/docs/en/about-claude/pricing |
| 商用利用の条件 | 出力の権利は利用者、Anthropic は顧客データで学習しない——は Bedrock／Vertex 版の規約・要約ページでのみ確認。**API の商用規約本文は未確認** | https://www.anthropic.com/legal |

**アプリの対応**
- 公式 Python SDK（`anthropic`）を使い、構造化出力（JSON スキーマ）で件名・本文・日本語要約・使った事実を受け取る。
- 安全上の理由で断られたときは、別のモデルで自動的にやり直す設定（初期値オン。小型モデルでは使わない）。
- 月間の呼び出し回数・トークン数の上限（初期値 200 回・200 万トークン）を超えると呼び出さない。使用量を記録して画面に表示。
- 402・429・529 などは日本語のエラーにして記録し、自動で有料プランに切り替えることはない。
- API キーがなくても、テンプレートと「コピー貼り付け」で同じ流れを使える。

## 5. Firecrawl 検索（候補発見・任意）

| 項目 | 公式の内容 | 出典 |
|---|---|---|
| 呼び出し | `POST https://api.firecrawl.dev/v2/search`（`Authorization: Bearer`）。`limit` は既定 10・最大 100 | https://docs.firecrawl.dev/api-reference/endpoint/search |
| 料金 | 検索は **10 件あたり 2 クレジット**（切り上げ）。無料プランは **毎月 1,000 クレジット**（繰り越しなし） | https://docs.firecrawl.dev/billing 、https://www.firecrawl.dev/pricing |
| 商用利用 | 利用規約 §5.d.1 は「Firecrawl が明示的に認めた場合を除き商用目的の利用」を禁止。有料プランがその許可に当たるかは**未確認** | https://www.firecrawl.dev/terms-of-service |

**アプリの対応**：月間の使用上限（初期値 200 クレジット）と 1 回の最大件数で止める。API キーは OS の資格情報保管に保存。

## 6. Cloudflare Tunnel（共有モードの外部公開・任意）

| 項目 | 公式の内容 | 出典 |
|---|---|---|
| Quick Tunnel | 「試験・開発用」。アカウント・ドメイン不要、稼働保証なし、同時 200 要求まで、起動のたびに URL が変わる | https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/ |
| 名前付きトンネル | アプリを公開するには Cloudflare のアカウントと、Cloudflare に登録したドメインが必要 | https://developers.cloudflare.com/tunnel/setup/ |

## 7. Python ライブラリ

| 項目 | 内容 | 出典 |
|---|---|---|
| keyring 25.7.0 | MIT。Windows では `pywin32-ctypes` を使う。PyPI では Windows 用の保管先を「Windows Credential Locker」と記載（Windows の「資格情報マネージャー」と同じ保管先かは公式記載で**未確認**） | https://pypi.org/project/keyring/ |
| tzdata 2026.5 | Apache-2.0。Windows には IANA の時刻帯データがないため、Python の公式文書は `tzdata` への依存を推奨（ないと `ZoneInfoNotFoundError`） | https://pypi.org/project/tzdata/ 、https://docs.python.org/3.12/library/zoneinfo.html |

配布版では `SamuraiDealerResearch-cli.exe selfcheck` で、資格情報保管への保存・読み出し・削除、英国時間の変換、AI ライブラリとメール部品の読み込みを確認できます（外部へは接続しません）。GitHub Actions の Windows ランナーでもこの確認を行います。
