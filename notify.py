"""暴落警戒モニターの結果をメールで知らせる（GitHub Actions の最後のステップから実行）。

  python notify.py success  … 全指標の判定一覧を送る
  python notify.py failure  … 更新失敗の連絡を送る

環境変数 SMTP_USER / SMTP_PASSWORD（Gmailアプリパスワード）、MAIL_TO（送信先。公開リポジトリのため Secret で渡す）。
"""
import json
import os
import smtplib
import sys
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PAGE = "https://sin88123451.github.io/crash-warning-monitor/"
LABEL = {"bad": "危険", "warn": "注意", "ok": "平常", "na": "取得失敗"}


def success_mail():
    d = json.loads((ROOT / "data.json").read_text(encoding="utf-8"))
    items = d["items"]
    cnt = {k: sum(1 for i in items if i["status"] == k) for k in LABEL}
    head = "危険サインが点灯中" if cnt["bad"] >= 2 else "一部で注意サイン" if cnt["bad"] + cnt["warn"] else "目立った危険サインなし"
    lines = []
    for st in ("bad", "warn", "na", "ok"):
        for i in items:
            if i["status"] == st:
                lines.append(f"【{LABEL[st]}】{i['name']}  {i['value'] or '—'}\n    {i['note']}" + (f"（{i['asof']}時点）" if i.get("asof") else ""))
    body = (
        f"暴落警戒モニターの週次更新が完了しました（{d['updated']}）。\n\n"
        f"■ 総合: {head}\n   危険 {cnt['bad']} ／ 注意 {cnt['warn']} ／ 平常 {cnt['ok']} ／ 取得失敗 {cnt['na']}\n\n"
        + "\n".join(lines)
        + f"\n\n■ 手動確認: 米国信用買い残（FINRA Margin Statistics。月次、毎月第3週ごろ更新）\n\n"
        f"各指標の見方・リンク・グラフ: {PAGE}\n\n"
        "※判定は目安であり、投資助言ではありません。\n"
    )
    subject = f"暴落警戒モニター 週次更新（危険{cnt['bad']}・注意{cnt['warn']}）"
    return subject, body


def failure_mail():
    run_url = os.environ.get("RUN_URL", "")
    body = (
        "暴落警戒モニターの週次自動更新が失敗しました。\n公開ページは前回成功時のまま更新されていません。\n\n"
        f"公開ページ: {PAGE}\n\n失敗の詳細（sin88123451 でログインしたブラウザでのみ開けます）:\n{run_url}\n\n"
        "Claude に「暴落警戒モニターの更新が失敗したので直して」と伝えてください。\n"
    )
    return "【要確認】暴落警戒モニターの週次更新が失敗しました", body


def main():
    status = sys.argv[1] if len(sys.argv) > 1 else "failure"
    user, password, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD"), os.environ.get("MAIL_TO")
    if not (user and password and to):
        print("SMTP_USER / SMTP_PASSWORD / MAIL_TO のいずれかが未設定のため通知できません", file=sys.stderr)
        sys.exit(1)
    rcpt = [x.strip() for x in to.replace(";", ",").split(",") if x.strip()]
    subject, body = success_mail() if status == "success" else failure_mail()
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = ", ".join(rcpt)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, password)
        s.sendmail(user, rcpt, msg.as_string())
    print(f"{status} の通知メールを送信しました: {len(rcpt)}件")


if __name__ == "__main__":
    main()
