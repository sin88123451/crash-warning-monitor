# 暴落警戒モニター

無料の公開データから暴落の先行サインを集め、dashboard.html に平常/注意/危険で表示する。

- 実行: `python monitor.py`（標準ライブラリのみ。data.json と dashboard.html を更新）
- 指標: ハイイールド債スプレッド(FRED)、VIX・SKEW・RSP/SPY(Yahoo)、シラーPER(multpl.com)、10年-2年(米財務省)、日米10年債利回り差(FRED。日本側は月次)
- しきい値は monitor.py 内の判定部分で調整する
- 取得に失敗した指標は「取得失敗」と表示（他の指標は動く）
- 注意: 2026-11-14時点、この PC からは FRED に接続できず(タイムアウト)、HYスプレッドと日米金利差は取得失敗。GitHub Actions 等からは取得できる見込み
- バフェット指標 = ウィルシャー5000(Yahoo ^W5000) ÷ 名目GDP(FRED)。公表値とは多少ずれる近似（2026-11-14追加）
- 日本株 騰落レシオ(25日) = nikkei225jp.com の daily2year.json（同サイト内部のデータファイル）を読む。サイト仕様が変わると取得失敗になる（2026-11-14追加）
- 未実装: FINRA信用買い残、日本株の信用評価損益率
- FREDはCSV直取得がタイムアウトするため、環境変数 FRED_API_KEY（無料APIキー）があればAPI経由で取得する。GitHubではリポジトリのSecret `FRED_API_KEY` に登録（2026-11-14時点は未登録）
- 公開ページ: https://sin88123451.github.io/crash-warning-monitor/ （公開リポジトリ sin88123451/crash-warning-monitor、毎週日曜 7:00 日本時間に Actions で更新し、結果をメール通知）
- 裁定買い残 = nikkei225jp.com の saitei ページ用データ(週次・金額)。取得にはReferer指定が必要（2026-11-14追加）
- メール通知: notify.py。Secret は SMTP_USER / SMTP_PASSWORD（一括更新ツール set_mail_password.ps1 で登録）と MAIL_TO（送信先。公開リポジトリのためSecretで渡す）
- 価格の動き欄(S&P500・ナスダック・日経・NVIDIA・ゴールド・東証REIT・不動産3社の高値比)、日経の日次変動、日本の長期金利(FRED月次)、見立ての検証(HYPOTHESES)を追加（2026-11-14）。売り出し在庫・成約価格は手動確認（東日本レインズ）
