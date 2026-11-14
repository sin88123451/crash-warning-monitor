"""暴落警戒モニター: 無料の公開データから危険サインを集めて dashboard.html を作る。
実行: python monitor.py   （標準ライブラリのみ。取得に失敗した指標は「取得失敗」と表示）
"""
import json, re, ssl, sys, time, datetime as dt
import urllib.request as ur
from pathlib import Path

HERE = Path(__file__).parent
UA = {"User-Agent": "Mozilla/5.0"}
OK, WARN, BAD, NA = "ok", "warn", "bad", "na"


def load_env():
    """10_調査部の.env があれば読む（ローカル実行用。GitHub上ではSecretを使う）"""
    import os
    f = HERE.parent / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def get(url, timeout=25, retries=2):
    last = None
    for _ in range(retries):
        try:
            return ur.urlopen(ur.Request(url, headers=UA), timeout=timeout).read().decode("utf-8", "replace")
        except Exception as e:  # noqa
            last = e
            time.sleep(1)
    raise last


# ---------- データ取得 ----------
def yahoo(symbol, rng="2y"):
    j = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={rng}&interval=1d"))
    r = j["chart"]["result"][0]
    out = []
    for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]):
        if c is not None:
            out.append((dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d"), float(c)))
    return out


def fred(series, start):
    import os
    key = os.environ.get("FRED_API_KEY")
    if key:  # APIキーがあればAPI経由（CSV直取得より安定）
        j = json.loads(get(f"https://api.stlouisfed.org/fred/series/observations?series_id={series}&api_key={key}&file_type=json&observation_start={start}", timeout=30))
        out = []
        for o in j["observations"]:
            try:
                out.append((o["date"], float(o["value"])))
            except ValueError:
                pass
        return out
    txt = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}", timeout=20, retries=2)
    out = []
    for line in txt.splitlines()[1:]:
        d, _, v = line.partition(",")
        try:
            out.append((d, float(v)))
        except ValueError:
            pass
    return out


def treasury(years):
    """米財務省の日次イールドカーブ。戻り値: {日付: (2年, 10年)}"""
    res = {}
    for y in years:
        x = get(f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value={y}", timeout=40)
        for e in re.findall(r"<m:properties>(.*?)</m:properties>", x, re.S):
            d = re.search(r"<d:NEW_DATE[^>]*>(\d{4}-\d{2}-\d{2})", e)
            a = re.search(r"<d:BC_2YEAR[^>]*>([\d.]+)", e)
            b = re.search(r"<d:BC_10YEAR[^>]*>([\d.]+)", e)
            if d and a and b:
                res[d.group(1)] = (float(a.group(1)), float(b.group(1)))
    return res


def shiller_pe():
    h = get("https://www.multpl.com/shiller-pe")
    m = re.search(r'id="current".*?([\d]{2}\.\d{1,2})', h, re.S)
    if not m:
        raise ValueError("シラーPERを読み取れず")
    return float(m.group(1))


# ---------- 判定 ----------
def back(series, days):
    """days日前以前で最も新しい値"""
    last = dt.date.fromisoformat(series[-1][0])
    tgt = (last - dt.timedelta(days=days)).isoformat()
    cand = [v for d, v in series if d <= tgt]
    return cand[-1] if cand else None


def item(name, value, status, note, series=None, unit="", fmt="{:.2f}", asof="", howto=""):
    return dict(name=name, value=None if value is None else fmt.format(value) + unit, status=status,
                note=note, series=[v for _, v in (series or [])][-250:], asof=asof, howto=howto)


def safe(fn):
    try:
        return fn()
    except Exception as e:  # noqa
        return e


def main():
    load_env()
    items = []
    today = dt.date.today()

    # 1. ハイイールド債スプレッド
    hy = safe(lambda: fred("BAMLH0A0HYM2", (today - dt.timedelta(days=400)).isoformat()))
    how = "FRED: BAMLH0A0HYM2 ／ 平時は約3%。3.5〜4%超へ急上昇なら警戒"
    if isinstance(hy, Exception) or not hy:
        items.append(item("ハイイールド債スプレッド", None, NA, f"取得失敗（FRED）: {hy}", howto=how))
    else:
        v = hy[-1][1]; m1 = back(hy, 30)
        rise = v - m1 if m1 is not None else 0
        st = BAD if v >= 4.0 else WARN if (v >= 3.5 or rise >= 0.5) else OK
        items.append(item("ハイイールド債スプレッド", v, st, f"1か月前比 {rise:+.2f}pt", hy, "%", asof=hy[-1][0], howto=how))

    # 2. VIX / SKEW
    vix = safe(lambda: yahoo("%5EVIX")); skew = safe(lambda: yahoo("%5ESKEW"))
    how = "SKEW≧145なのにVIXが低い＝機関投資家が暴落保険を買い集めている合図"
    vv = None if isinstance(vix, Exception) else vix[-1][1]
    if isinstance(vix, Exception):
        items.append(item("VIX（恐怖指数）", None, NA, f"取得失敗: {vix}"))
    else:
        st = BAD if vv >= 30 else WARN if vv >= 20 else OK
        items.append(item("VIX（恐怖指数）", vv, st, "12〜15は油断水準、20超で警戒、30超で恐怖", vix, asof=vix[-1][0]))
    if isinstance(skew, Exception):
        items.append(item("SKEW（ブラックスワン指数）", None, NA, f"取得失敗: {skew}", howto=how))
    else:
        s = skew[-1][1]
        st = BAD if (s >= 145 and (vv or 99) < 20) else WARN if s >= 140 else OK
        items.append(item("SKEW（ブラックスワン指数）", s, st, "VIXが低いのにSKEW高＝要警戒" if st == BAD else "145超が目安", skew, fmt="{:.1f}", asof=skew[-1][0], howto=how))

    # 3. RSP/SPY
    rsp = safe(lambda: yahoo("RSP")); spy = safe(lambda: yahoo("SPY"))
    how = "右肩下がり＝一部の超大型株だけが指数を押し上げている（相場末期）"
    if isinstance(rsp, Exception) or isinstance(spy, Exception):
        items.append(item("市場の広がり（RSP/SPY）", None, NA, f"取得失敗: {rsp if isinstance(rsp, Exception) else spy}", howto=how))
    else:
        sd = dict(spy)
        ratio = [(d, c / sd[d]) for d, c in rsp if d in sd]
        v = ratio[-1][1]; m3 = back(ratio, 90); m6 = back(ratio, 180)
        c3 = (v / m3 - 1) * 100 if m3 else 0; c6 = (v / m6 - 1) * 100 if m6 else 0
        st = BAD if (c3 < -3 and c6 < -3) else WARN if c3 < -1.5 else OK
        items.append(item("市場の広がり（RSP/SPY）", v, st, f"3か月 {c3:+.1f}% ／ 6か月 {c6:+.1f}%", ratio, fmt="{:.4f}", asof=ratio[-1][0], howto=how))

    # 4. シラーPER
    pe = safe(shiller_pe)
    how = "40倍前後は歴史的な割高圏（1999年ITバブルのピークは約44倍）"
    if isinstance(pe, Exception):
        items.append(item("シラーPER（CAPE）", None, NA, f"取得失敗（multpl.com）: {pe}", howto=how))
    else:
        st = BAD if pe >= 35 else WARN if pe >= 30 else OK
        items.append(item("シラーPER（CAPE）", pe, st, "35倍以上で危険、30倍以上で注意", fmt="{:.1f}", unit="倍", asof=str(today), howto=how))

    # 4b. バフェット指標（ウィルシャー5000 ÷ 名目GDP。市場全体の時価総額/GDPの近似）
    w5 = safe(lambda: yahoo("%5EW5000", "2y")); gdp = safe(lambda: fred("GDP", (today - dt.timedelta(days=900)).isoformat()))
    how = "200%超は歴史的な超割高圏。暴落時の下落幅が大きくなる目安"
    if isinstance(w5, Exception) or isinstance(gdp, Exception) or not gdp:
        items.append(item("バフェット指標", None, NA, f"取得失敗: {w5 if isinstance(w5, Exception) else gdp}", howto=how))
    else:
        def gdp_at(d):  # その日に公表済みの直近四半期GDP（10億ドル）
            c = [v for gd, v in gdp if gd <= d]
            return c[-1] if c else gdp[0][1]
        bi = [(d, c / gdp_at(d) * 100) for d, c in w5]
        v = bi[-1][1]
        st = BAD if v >= 200 else WARN if v >= 150 else OK
        items.append(item("バフェット指標", v, st, "200%以上で危険、150%以上で注意（ウィルシャー5000÷名目GDPの近似値）", bi, fmt="{:.0f}", unit="%", asof=bi[-1][0], howto=how))

    # 5. 逆イールド解消 / 6. 日米金利差
    ty = safe(lambda: treasury([today.year - 1, today.year]))
    how = "マイナス圏からプラス圏へ戻った直後が景気後退・暴落の始まりになりやすい"
    if isinstance(ty, Exception) or not ty:
        items.append(item("逆イールド解消（10年−2年）", None, NA, f"取得失敗（米財務省）: {ty}", howto=how))
        us10 = None
    else:
        ds = sorted(ty)
        spread = [(d, round(ty[d][1] - ty[d][0], 3)) for d in ds]
        us10 = [(d, ty[d][1]) for d in ds]
        v = spread[-1][1]
        last = dt.date.fromisoformat(spread[-1][0])
        yr = [x for d, x in spread if dt.date.fromisoformat(d) >= last - dt.timedelta(days=365)]
        neg_recent = [d for d, x in spread if x < 0 and dt.date.fromisoformat(d) >= last - dt.timedelta(days=180)]
        if v < 0:
            st, note = WARN, "現在も逆イールド中（まだ解消前）"
        elif min(yr) < 0:
            st, note = BAD, "過去1年に逆転あり、直近でプラス圏へ復帰" if neg_recent else "1年以内に逆転→解消済み（景気後退入りの警戒期間）"
        else:
            st, note = OK, "逆イールドなし"
        items.append(item("逆イールド解消（10年−2年）", v, st, note, spread, "%", asof=spread[-1][0], howto=how))

    jp = safe(lambda: fred("IRLTLT01JPM156N", (today - dt.timedelta(days=500)).isoformat()))
    how = "日米金利差の急低下＝急速な円高（輸出企業への逆風）。日本側は月次データ"
    if us10 is None or isinstance(jp, Exception) or not jp:
        items.append(item("日米10年債利回り差", None, NA, f"取得失敗: {jp if isinstance(jp, Exception) else '米国債データなし'}", howto=how))
    else:
        jpd = dict(jp)
        gap = []
        for d, u10 in us10:
            ym = d[:7] + "-01"
            if ym in jpd:
                gap.append((d, round(u10 - jpd[ym], 3)))
        if not gap:  # 当月が未公表なら直近月で代用
            lastjp = jp[-1][1]; gap = [(d, round(u - lastjp, 3)) for d, u in us10]
        v = gap[-1][1]; m3 = back(gap, 90)
        ch = v - m3 if m3 is not None else 0
        st = BAD if ch <= -0.5 else WARN if ch <= -0.25 else OK
        items.append(item("日米10年債利回り差", v, st, f"3か月前比 {ch:+.2f}pt", gap, "%", asof=gap[-1][0], howto=how))

    out = dict(updated=dt.datetime.now().strftime("%Y-%m-%d %H:%M"), items=items)
    (HERE / "data.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (HERE / "dashboard.html").write_text(render(out), encoding="utf-8")
    for i in items:
        print(f"[{i['status']:4}] {i['name']}: {i['value']}  {i['note']}")


# ---------- HTML ----------
LABEL = {OK: "平常", WARN: "注意", BAD: "危険", NA: "取得失敗"}


def spark(vals, color):
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals); rng = (hi - lo) or 1
    pts = " ".join(f"{i * 240 / (len(vals) - 1):.1f},{44 - (v - lo) / rng * 40:.1f}" for i, v in enumerate(vals))
    return f'<svg viewBox="0 0 240 48" class="sp"><polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6"/></svg>'


def render(d):
    cnt = {k: sum(1 for i in d["items"] if i["status"] == k) for k in (OK, WARN, BAD, NA)}
    head = "危険サインが点灯中" if cnt[BAD] >= 2 else "一部で注意サイン" if cnt[BAD] + cnt[WARN] else "目立った危険サインなし"
    cards = ""
    for i in d["items"]:
        color = {OK: "var(--ok)", WARN: "var(--warn)", BAD: "var(--bad)", NA: "var(--mute)"}[i["status"]]
        cards += f"""<section class="card {i['status']}"><div class="top"><h2>{i['name']}</h2><span class="badge">{LABEL[i['status']]}</span></div>
<div class="val">{i['value'] or '—'}</div><div class="note">{i['note']}</div>{spark(i['series'], color)}
<div class="how">{i['howto']}</div><div class="asof">{('データ日付 ' + i['asof']) if i['asof'] else ''}</div></section>"""
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>暴落警戒モニター</title><style>
:root{{--bg:#f6f7f9;--fg:#111;--card:#fff;--mute:#6b7280;--ok:#16a34a;--warn:#d97706;--bad:#dc2626;--line:#e5e7eb}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0f1115;--fg:#eee;--card:#181b21;--mute:#9ca3af;--line:#2a2f38}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,"Yu Gothic",sans-serif}}
main{{max-width:980px;margin:0 auto;padding:16px}}h1{{font-size:22px;margin:8px 0 2px}}
.sum{{color:var(--mute);margin-bottom:14px}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-left:6px solid var(--mute);border-radius:10px;padding:12px 14px}}
.card.ok{{border-left-color:var(--ok)}}.card.warn{{border-left-color:var(--warn)}}.card.bad{{border-left-color:var(--bad)}}
.top{{display:flex;justify-content:space-between;align-items:center;gap:8px}}h2{{font-size:15px;margin:0}}
.badge{{font-size:12px;padding:1px 9px;border-radius:99px;border:1px solid currentColor;white-space:nowrap}}
.ok .badge{{color:var(--ok)}}.warn .badge{{color:var(--warn)}}.bad .badge{{color:var(--bad)}}.na .badge{{color:var(--mute)}}
.val{{font-size:30px;font-weight:700;margin:4px 0}}.note{{font-size:13px}}.sp{{width:100%;height:48px;margin-top:6px}}
.how,.asof{{font-size:12px;color:var(--mute);margin-top:6px}}footer{{color:var(--mute);font-size:12px;margin-top:18px}}
</style></head><body><main><h1>暴落警戒モニター</h1>
<div class="sum">{head}（危険 {cnt[BAD]} ／ 注意 {cnt[WARN]} ／ 平常 {cnt[OK]} ／ 取得失敗 {cnt[NA]}）　更新 {d['updated']}</div>
<div class="grid">{cards}</div>
<footer>データ: FRED・米財務省・Yahoo Finance・multpl.com。判定は目安であり、投資助言ではありません。個々のしきい値は monitor.py で調整できます。</footer></main></body></html>"""


if __name__ == "__main__":
    main()
