# 初期データ（実在の英国中古車販売店）調査報告

作成：2026-10-24 08:57 UTC。アプリの調査処理で公式サイトを取得し、全件の根拠を人が照合した結果です（`scripts/curate_initial_data.py` に照合内容を記録）。
アプリ初回起動時に `sdr/seed/initial_dealers.json` から読み込まれます（データ区分「初期調査データ」）。

## 集計

| 項目 | 件数 |
|---|---|
| 登録した販売事業者（実在・英国・中古車販売を公式サイトで確認） | 30 |
| 公開メールまたは問い合わせフォームを確認できた販売店 | **28** |
| 公開メールを確認（公式サイトで公開表示） | 23 |
| 問い合わせフォームを確認 | 21 |
| 連絡方法が未確認（電話・問い合わせページのみ等） | 2 |
| 人の確認状態「確認済み」 | 29（残りは要確認） |
| 調査したが取得できなかった・照合で除外したサイト | 32（`data/unreachable_or_excluded.csv`） |

分類内訳（1社で複数あり）：一般中古車 18／スポーツカー 4／ミニバン・SUV 5／クラシックカー 3／その他 1／要確認 1

分類根拠の内訳：確認した在庫からの傾向 14／要手動確認 4／公式に専門店と明記 12

注意：公開メールは「公式サイトで公開表示を確認した」ことだけを示します。到達可能性・営業送信の許可は確認していません。
「確認した在庫からの傾向」は確認した在庫ページの範囲での判断で、全在庫は調べていません。

## 一覧

| # | 店名 | 地域 | 分類（根拠） | 公開メール | フォーム | 確認 |
|---|---|---|---|---|---|---|
| 1 | [Woodhall Cars](https://www.woodhallcars.co.uk/) | ヨークシャー・Leedsエリア（郵便番号 LS） | 一般中古車（確認した在庫からの傾向） | woodhallcars@gmail.com | 確認済み | 確認済み |
| 2 | [AS Cars Leeds LTD](https://www.ascarsleedsltd.co.uk/) | ヨークシャー・Leedsエリア（郵便番号 LS） | 一般中古車（確認した在庫からの傾向） | sales@ascarsleedsltd.co.uk | 確認済み | 確認済み |
| 3 | [White Rose Cars](https://www.whiterosecarsales.co.uk/) | ヨークシャー・Leedsエリア（郵便番号 LS） | 一般中古車（確認した在庫からの傾向） | rafa11@hotmail.co.uk | 確認済み | 確認済み |
| 4 | [BK General & Prestige Cars](https://www.bkgeneralandprestigecars.co.uk/) | ヨークシャー・Leedsエリア（郵便番号 LS） | 一般中古車（要手動確認） | 未確認 | 確認済み | 確認済み |
| 5 | [Angel Motor Company](https://www.angelmotorcompany.com/) | ヨークシャー・Leedsエリア（郵便番号 LS） | 一般中古車（確認した在庫からの傾向） | newfamilyautos@hotmail.com | 確認済み | 確認済み |
| 6 | [Burnside Car Store](https://burnsidecarstore.co.uk/) | スコットランド・Glasgowエリア（郵便番号 G） | 一般中古車（確認した在庫からの傾向） | info@burnsidecarstore.co.uk | 確認済み | 確認済み |
| 7 | [Crawford Automobiles](https://www.crawfordautomobiles.co.uk/) | スコットランド・Glasgowエリア（郵便番号 G） | 一般中古車（確認した在庫からの傾向） | sales@crawfordautomobiles.co.uk | 確認済み | 確認済み |
| 8 | [Garrowhill Car Sales](https://www.garrowhillcarsales.co.uk/) | スコットランド・Glasgowエリア（郵便番号 G） | 一般中古車（確認した在庫からの傾向） | garrowhillcarsales@outlook.com | 確認済み | 確認済み |
| 9 | [GMP Cars](https://www.gmpcars.co.uk/) | イングランド北西部・Prestonエリア（郵便番号 PR） | 一般中古車（確認した在庫からの傾向） | sales@gmpcars.co.uk | 確認済み | 確認済み |
| 10 | [NG9 Motors](https://www.ng9motors.co.uk/) | イースト・ミッドランズ・Nottinghamエリア（郵便番号 NG） | 一般中古車（確認した在庫からの傾向） | sales@ng9motors.co.uk | ページのみ | 確認済み |
| 11 | [York Car & Commercial](https://www.yorkcarandcommercial.co.uk/) | ヨークシャー・Yorkエリア（郵便番号 YO） | 一般中古車（確認した在庫からの傾向） | 未確認 | ページのみ | 確認済み |
| 12 | [Ultima 4x4 Ltd](https://www.ultima4x4.co.uk/) | イングランド東部・Chelmsfordエリア（郵便番号 CM） | 一般中古車（確認した在庫からの傾向） | sales@ultima4x4.co.uk | 確認済み | 確認済み |
| 13 | [Prestige Motor Co](https://www.prestigemotorco.com/) | スコットランド・Edinburghエリア（郵便番号 EH） | 一般中古車（要手動確認） | sales@prestigemotorco.com | ページのみ | 確認済み |
| 14 | [JDM Autos](https://jdmautos.co.uk/) | イングランド南東部・Southampton（会社紹介ページの記載。郵便番号は未確認） | 一般中古車（確認した在庫からの傾向） | info@jdmautos.co.uk | 確認済み | 確認済み |
| 15 | [1st Choice Cars](http://www.1stchoicecarspreston.co.uk/) | イングランド北西部・Prestonエリア（郵便番号 PR） | 一般中古車（確認した在庫からの傾向） | sales@1stchoicecarspreston.co.uk | 確認済み | 確認済み |
| 16 | [Cardiff Motors](http://www.cardiffmotors.com/) | ウェールズ・Cardiffエリア（郵便番号 CF） | 一般中古車（確認した在庫からの傾向） | cardiffmotorssales@gmail.com | 確認済み | 確認済み |
| 17 | [Mount Vernon Motors](http://www.mount-vernon.co.uk/) | スコットランド・Glasgowエリア（郵便番号 G） | 一般中古車（要手動確認） | 未確認 | 確認済み | 確認済み |
| 18 | [Heritage Classic](https://www.heritageclassic.com/) | ロンドン・London SWエリア（郵便番号 SW） | クラシックカー（公式に専門店と明記） | sales@heritageclassic.com | 確認済み | 確認済み |
| 19 | [The Classic Motor Hub](https://classicmotorhub.com/) | イングランド南西部・Gloucesterエリア（郵便番号 GL） | クラシックカー（公式に専門店と明記） | info@classicmotorhub.com | ページのみ | 確認済み |
| 20 | [RH Classics](https://www.rhclassics.co.uk/) | イースト・ミッドランズ・Leicesterエリア（郵便番号 LE） | クラシックカー（公式に専門店と明記） | 未確認 | 確認済み | 確認済み |
| 21 | [Cameron Sports Cars Ltd](https://www.cameronsportscars.com/) | イングランド南西部・Swindonエリア（郵便番号 SN） | スポーツカー（公式に専門店と明記） | sales@cameronsc.co.uk | ページのみ | 確認済み |
| 22 | [Harbour Cars](https://www.harbourcars.com/) | イングランド南東部・Portsmouthエリア（郵便番号 PO） | 要確認（要手動確認） | sales@harbourcars.com | ページのみ | 要確認 |
| 23 | [Stirlings](https://www.stirlings.co.uk/) | 不明 | スポーツカー（公式に専門店と明記） | mark@stirlings.co.uk | 確認済み | 確認済み |
| 24 | [JR Motor Company](https://www.jrmotorcompany.co.uk/) | ウェスト・ミッドランズ・Coventryエリア（郵便番号 CV） | スポーツカー（公式に専門店と明記） | jrmotorcompany@yahoo.co.uk | 確認済み | 確認済み |
| 25 | [911virgin](https://www.911virgin.com/) | イングランド南東部・Sloughエリア（郵便番号 SL） | スポーツカー（公式に専門店と明記） | office@911virgin.com | ページのみ | 確認済み |
| 26 | [John Kemp 4x4](https://www.kemp4x4s.co.uk/) | イングランド東部・Norwichエリア（郵便番号 NR） | ミニバン・SUV（公式に専門店と明記） | sales@kemp4x4s.co.uk<br>service@kemp4x4s.co.uk | ページのみ | 確認済み |
| 27 | [Barry Ashworth 4x4](https://barryashworth4x4.co.uk/) | イングランド北西部・Oldhamエリア（郵便番号 OL） | ミニバン・SUV・その他（公式に専門店と明記） | 未確認 | 確認済み | 確認済み |
| 28 | [Beckside 4X4 Centre](https://www.beckside4x4.co.uk/) | ヨークシャー・Bradfordエリア（郵便番号 BD） | ミニバン・SUV・一般中古車（公式に専門店と明記） | 未確認 | 確認済み | 確認済み |
| 29 | [Mike Harding Landrovers](https://mikehardinglandrovers.co.uk/) | イングランド南西部・Torquayエリア（郵便番号 TQ） | ミニバン・SUV（公式に専門店と明記） | mike@mikehardinglandrovers.co.uk | 確認済み | 確認済み |
| 30 | [Land Rover Centre](https://www.landrovercentre.com/) | ヨークシャー・Huddersfieldエリア（郵便番号 HD） | ミニバン・SUV（公式に専門店と明記） | 未確認 | ページのみ | 確認済み |

## 取得できなかった・除外したサイト（隠さず記録）

アクセス制限（HTTP 403・ボット確認ページ）は回避していません。接続できないサイトは robots.txt を確認できないため取得を見送っています。これらはブラウザーで人が確認すれば手動で登録できます。

| ドメイン | 区分 | 理由 |
|---|---|---|
| affordablepremiumcars.co.uk | 取得失敗 | トップページを取得できませんでした: HTTP 403 |
| birchwoodmotors.co.uk | 取得失敗 | トップページを取得できませんでした: HTTP 403 |
| botleycarcentre.com | 取得失敗 | トップページを取得できませんでした: HTTP 403 |
| carsrusnotts.co.uk | 取得失敗 | トップページを取得できませんでした: robots.txt を取得できないため取得を見送りました（接続エラー: ConnectError） |
| cjautosbristol.co.uk | 取得失敗 | トップページを取得できませんでした: HTTP 403（アクセス拒否。ボット対策の可能性があるため回避せず、ブラウザーでの手動確認が必要） |
| copycars.co.uk | 取得失敗 | トップページを取得できませんでした: HTTP 403（アクセス拒否。ボット対策の可能性があるため回避せず、ブラウザーでの手動確認が必要） |
| edinburghcarsales.co.uk | 取得失敗 | トップページを取得できませんでした: HTTP 403 |
| fishpondsautos.co.uk | 取得失敗 | トップページを取得できませんでした: robots.txt を取得できないため取得を見送りました（接続エラー: ConnectError） |
| harriscars.co.uk | 取得失敗 | トップページを取得できませんでした: HTTP 403（アクセス拒否。ボット対策の可能性があるため回避せず、ブラウザーでの手動確認が必要） |
| hazelmotorcompany.co.uk | 取得失敗 | トップページを取得できませんでした: 接続エラー: ConnectError |
| inspirecarsalessouthampton.co.uk | 取得失敗 | トップページを取得できませんでした: robots.txt を取得できないため取得を見送りました（接続がタイムアウトしました） |
| johnsons4x4.co.uk | 取得失敗 | トップページを取得できませんでした: HTTP 403（アクセス拒否。ボット対策の可能性があるため回避せず、ブラウザーでの手動確認が必要） |
| kentqualitycarcentre.co.uk | 取得失敗 | トップページを取得できませんでした: robots.txt を取得できないため取得を見送りました（接続がタイムアウトしました） |
| maltmillmotorsplumtree.co.uk | 取得失敗 | トップページを取得できませんでした: HTTP 403（アクセス拒否。ボット対策の可能性があるため回避せず、ブラウザーでの手動確認が必要） |
| mandacarsleeds.co.uk | 取得失敗 | トップページを取得できませんでした: アクセス制限（CAPTCHA・ボット確認等）が表示されたため取得しませんでした |
| moto-vation-leeds.co.uk | 取得失敗 | トップページを取得できませんでした: HTTP 403 |
| oldlandmotorcompany.co.uk | 取得失敗 | トップページを取得できませんでした: robots.txt を取得できないため取得を見送りました（接続がタイムアウトしました） |
| palmersmotorco.co.uk | 取得失敗 | トップページを取得できませんでした: HTTP 403 |
| relianceauto.co.uk | 取得失敗 | トップページを取得できませんでした: HTTP 403 |
| rsjsportscars.co.uk | 取得失敗 | トップページを取得できませんでした: HTTP 403（アクセス拒否。ボット対策の可能性があるため回避せず、ブラウザーでの手動確認が必要） |
| sms-motors.co.uk | 取得失敗 | トップページを取得できませんでした: robots.txt を取得できないため取得を見送りました（接続がタイムアウトしました） |
| stationspecialistcarsltd.co.uk | 取得失敗 | トップページを取得できませんでした: HTTP 403 |
| storry4x4.co.uk | 取得失敗 | トップページを取得できませんでした: HTTP 403（アクセス拒否。ボット対策の可能性があるため回避せず、ブラウザーでの手動確認が必要） |
| swanzmotors.co.uk | 取得失敗 | トップページを取得できませんでした: robots.txt を取得できないため取得を見送りました（接続がタイムアウトしました） |
| tmautosltd.co.uk | 取得失敗 | トップページを取得できませんでした: HTTP 403 |
| usedcarmanchester.co.uk | 取得失敗 | トップページを取得できませんでした: アクセス制限（CAPTCHA・ボット確認等）が表示されたため取得しませんでした |
| walton-motors.co.uk | 取得失敗 | トップページを取得できませんでした: HTTP 403（アクセス拒否。ボット対策の可能性があるため回避せず、ブラウザーでの手動確認が必要） |
| leedsmotorcarsltd.co.uk | 照合で除外 | ボット確認ページ（One moment, please...）が表示され取得できない（予備調査ではこのページを誤って読み込んだため、検出処理を追加した） |
| motovia.co.uk | 照合で除外 | 公式サイトが作成途中（Content Coming Soon）で、中古車販売を確認できない |
| nobukojapan.com | 照合で除外 | 所在地が東京（構造化データ）の日本の輸出業者で、英国の販売店ではない |
| pearcebros.com | 照合で除外 | 主な事業がバンのレンタル・整備で、問い合わせ・在庫ページは取得タイムアウト。中古車販売店か要確認 |
| sgpetch.co.uk | 照合で除外 | 複数ブランドの正規ディーラー（新車・中古車）。独立系の候補を優先し今回は含めない |

## 照合の手順

1. 候補の出典（AA Cars の販売店ページの公式サイトリンク、Web 検索結果、911uk.com の販売店一覧）から公式サイト URL を得た（URL の推測なし）。
2. アプリの調査処理で公式サイトを取得（robots.txt 遵守・取得間隔 2 秒以上・1 サイト最大 10 ページ）。
3. 根拠の抜粋と出典 URL を読み、名称・公式サイト・英国所在（郵便番号）・中古車販売の記載・分類根拠・連絡方法を照合。
4. 誤りは人の確定値で修正し、判断をメモに記録（店名の修正 3 件、地域の補記 1 件、分類の追加 1 件、連絡方法の確定 3 件）。
5. 英国外の事業者・作成途中のサイト・主業が異なるものは除外。

初期データの登録（この報告）と、アプリ自身による新規候補の発見・調査の検証は別に行いました（docs/VERIFICATION.md）。
