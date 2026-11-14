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


def get(url, timeout=25, retries=2, referer=None):
    last = None
    for _ in range(retries):
        try:
            return ur.urlopen(ur.Request(url, headers={**UA, **({'Referer': referer} if referer else {})}), timeout=timeout).read().decode("utf-8", "replace")
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


def arbitrage_buy():
    """裁定買い残（金額、週次）。戻り値: [(日付, 兆円)]。nikkei225jp.com の saitei ページ用データを読む"""
    txt = get("https://nikkei225jp.com/_data/_nfsDATA/json_DAY/daily_saitei.json?498512", timeout=30, referer="https://nikkei225jp.com/data/saitei.php")
    out = []
    for line in txt.splitlines():
        line = line.strip().rstrip(",")
        if not (line.startswith("[") and line.endswith("]")) or line.startswith("[["):
            continue
        c = [x.strip().strip('"') for x in line[1:-1].split(",")]
        try:
            ts, v = float(c[0]), float(c[8])
        except (ValueError, IndexError):
            continue
        out.append((dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d"), v / 1_000_000))
    if len(out) < 20:
        raise ValueError("裁定買い残のデータが不足")
    return out


def touraku25():
    """日経225JPの日次データから東証の25日騰落レシオを取得。戻り値: [(日付, 値)]"""
    txt = get("https://nikkei225jp.com/_data/_nfsDATA/data_DAY/daily2year.json", timeout=30)
    out = []
    for line in txt.splitlines():
        line = line.strip().rstrip(",")
        if not (line.startswith("[") and line.endswith("]")) or line.startswith("[["):
            continue
        c = [x.strip().strip('"') for x in line[1:-1].split(",")]
        try:
            ts, v = float(c[0]), float(c[7])
        except (ValueError, IndexError):
            continue
        if v > 0:
            out.append((dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d"), v))
    if len(out) < 30:
        raise ValueError("騰落レシオのデータが不足")
    return out


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
    ty = safe(lambda: treasury(range(today.year - 4, today.year + 1)))
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
        # 直近の「逆イールドが解消した日」（マイナス→最初のプラス）を探す
        resolved = None
        for (d0, v0), (d1, v1) in zip(spread, spread[1:]):
            if v0 < 0 <= v1:
                resolved = dt.date.fromisoformat(d1)
        if v < 0:
            st, note = WARN, "現在も逆イールド中（まだ解消前）"
        elif resolved is None:
            st, note = OK, "取得期間内に逆イールドなし"
        else:
            days = (last - resolved).days
            ago = f"{days // 30}か月前" if days >= 60 else f"{days}日前"
            if days <= 180:
                st, note = BAD, f"逆イールド解消の直後（{resolved}・{ago}）。最も警戒する局面"
            elif days <= 730:
                st, note = WARN, f"解消から2年以内（{resolved}・{ago}）。景気後退・暴落の警戒期間"
            else:
                st, note = OK, f"直近の解消は {resolved}（{ago}）。2年超が経過"
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

    # 7. 日経 騰落レシオ（25日）
    tr = safe(touraku25)
    how = "日経平均が上がっているのに騰落レシオが120%超→100%割れへ急落＝内部の買いエネルギー切れ"
    if isinstance(tr, Exception):
        items.append(item("日本株 騰落レシオ（25日）", None, NA, f"取得失敗（nikkei225jp.com）: {tr}", howto=how))
    else:
        v = tr[-1][1]; mx = max(x for _, x in tr[-30:])
        if mx >= 120 and v < 100:
            st, note = BAD, f"直近30営業日で{mx:.0f}%まで過熱→100%割れ。買いエネルギー切れの兆し"
        elif v >= 120:
            st, note = WARN, "120%超の過熱圏（ここから急落に転じるかを注視）"
        else:
            st, note = OK, f"過熱(120%超)なし。直近30営業日の最高 {mx:.0f}%"
        items.append(item("日本株 騰落レシオ（25日）", v, st, note, tr, "%", fmt="{:.1f}", asof=tr[-1][0], howto=how))

    # 8. 裁定買い残
    ab = safe(arbitrage_buy)
    how = "ピークアウトして減少に転じると、海外勢の日本株売りが始まった合図"
    if isinstance(ab, Exception):
        items.append(item("裁定買い残（日本株）", None, NA, f"取得失敗（nikkei225jp.com）: {ab}", howto=how))
    else:
        w = ab[-52:]; v = w[-1][1]
        peak_i = max(range(len(w)), key=lambda k: w[k][1]); peak = w[peak_i][1]
        drop = (1 - v / peak) * 100; peak_recent = peak_i >= len(w) - 13
        if peak_recent and drop >= 15:
            st, note = BAD, f"直近13週のピーク{peak:.2f}兆円（{w[peak_i][0]}）から{drop:.0f}%減。ピークアウトの兆し"
        elif drop >= 7:
            st, note = WARN, f"52週ピーク{peak:.2f}兆円（{w[peak_i][0]}）から{drop:.0f}%減"
        else:
            st, note = OK, f"52週ピーク{peak:.2f}兆円（{w[peak_i][0]}）から{drop:.0f}%減。ピーク圏を維持"
        items.append(item("裁定買い残（日本株）", v, st, note, ab, "兆円", asof=ab[-1][0], howto=how))

    out = dict(updated=dt.datetime.now().strftime("%Y-%m-%d %H:%M"), items=items)
    (HERE / "data.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (HERE / "dashboard.html").write_text(render(out), encoding="utf-8")
    for i in items:
        print(f"[{i['status']:4}] {i['name']}: {i['value']}  {i['note']}")


# ---------- 各指標の見方（何のために・どこを見るか） ----------
FRED = "https://fred.stlouisfed.org/series/"
TV = "https://www.tradingview.com/symbols/"
GUIDE = {
    "ハイイールド債スプレッド": dict(
        why="信用リスクの先行指標。低格付け企業の債券が国債よりどれだけ高い利回りを求められているか＝「お金を貸す側の不安」を表す。株より先に悪化しやすい。",
        where=[("FRED: BAMLH0A0HYM2", FRED + "BAMLH0A0HYM2"), ("TradingView: FRED:BAMLH0A0HYM2", TV + "FRED-BAMLH0A0HYM2/")],
        sign="平時は3%前後。3.5〜4.0%超へ急上昇し始めたら警戒。株が最高値圏なのにこれが上がり始めたら、プロが逃げ出しているサイン。（FREDは直近3年分の表示）"),
    "VIX（恐怖指数）": dict(
        why="S&P500オプションから計算される、市場の「今後の揺れの予想」。低いほど市場が油断している。SKEWとセットで見る。",
        where=[("TradingView: VIX", TV + "CBOE-VIX/"), ("Yahoo Finance: ^VIX", "https://finance.yahoo.com/quote/%5EVIX")],
        sign="12〜15の低水準は油断のサイン。この状態でSKEWが跳ね上がっていないかを必ず確認する。"),
    "SKEW（ブラックスワン指数）": dict(
        why="大暴落に備える保険（プット）がどれだけ買われているか。機関投資家の「暴落への備え」を映す。",
        where=[("TradingView: SKEW", TV + "CBOE-SKEW/"), ("Yahoo Finance: ^SKEW", "https://finance.yahoo.com/quote/%5ESKEW")],
        sign="VIXが12〜15の低水準なのに、SKEWが145〜150超に跳ね上がっていたら「裏で機関投資家が暴落保険を買い集めている」合図。"),
    "市場の広がり（RSP/SPY）": dict(
        why="等金額S&P500(RSP)÷通常のS&P500(SPY)。値上がりが一部の超大型株だけか、500社に広がっているかを見る。",
        where=[("TradingView: 検索窓に RSP/SPY と入力してEnter", TV + "AMEX-RSP/")],
        sign="チャートが右肩下がりなら危険。「一部の超大型AI株だけが指数を押し上げ、残りの490社は置いてけぼり（相場末期）」のサイン。"),
    "シラーPER（CAPE）": dict(
        why="過去10年の平均利益で割った株価収益率。景気変動をならした割高・割安の判定で、暴落時の下落幅の目安になる（タイミングは測れない）。",
        where=[("Multpl.com（トップに表示。検索不要）", "https://www.multpl.com/shiller-pe")],
        sign="40倍前後は歴史的な超割高圏（1999年ITバブルのピークは約44倍）。割高なほど、暴落したときの下落幅が大きくなりやすい。"),
    "バフェット指標": dict(
        why="株式市場全体の時価総額÷名目GDP。経済の規模に対して株がどれだけ膨らんでいるかを見る。",
        where=[("検索キーワード: Current Market Valuation（公表値の確認用）", "https://www.google.com/search?q=Current+Market+Valuation+Buffett+Indicator")],
        sign="200%超は歴史的な超割高圏。暴落時の下落幅が大きくなる目安。※本ツールの値は ウィルシャー5000÷GDP の近似で、公表値とは数十%ずれることがある。"),
    "逆イールド解消（10年−2年）": dict(
        why="長期金利−短期金利。短期のほうが高い（マイナス）のは景気後退の予兆。大事なのは「逆転した後に戻る」局面。",
        where=[("FRED: T10Y2Y", FRED + "T10Y2Y"), ("TradingView: US10Y-US02Y", TV + "FRED-T10Y2Y/")],
        sign="マイナス圏（逆イールド）からプラス圏（0%以上）へ急激に戻った直後が、過去幾度となく暴落・景気後退が始まったタイミング。"),
    "日本株 騰落レシオ（25日）": dict(
        why="東証の値上がり銘柄数÷値下がり銘柄数（25日合計）。日経平均の動きの裏で、個別株の買いの勢いが保たれているかを見る。",
        where=[("世界の株価（nikkei225jp.com）騰落レシオ", "https://nikkei225jp.com/data/touraku.php")],
        sign="日経平均が上がっているのに、騰落レシオが120%超の過熱から100%割れへ急落し始めたら、内部の買いエネルギー切れのサイン。※本ツールの値は同サイトのデータを読み取っており、サイト側の仕様変更で取得できなくなることがある。"),
    "裁定買い残（日本株）": dict(
        why="裁定取引（先物売り＋現物買い）に伴う現物株の買い残高。海外勢の先物買いに連動して積み上がるため、海外勢の日本株への姿勢を映す。",
        where=[("世界の株価（nikkei225jp.com）裁定買い残", "https://nikkei225jp.com/data/saitei.php"), ("トレーダーズ・ウェブ／株探の市場ニュース（毎週公表）", "https://kabutan.jp/")],
        sign="裁定買い残がピークアウトして減少に転じると、海外勢の日本株売りが始まった合図。※本ツールは週次の金額（兆円）を同サイトから読み取っており、サイト仕様が変わると取得できなくなることがある。"),
    "日米10年債利回り差": dict(
        why="ドル円を動かす日米の金利差。差が縮むと急速な円高になりやすく、日本の輸出企業の株に逆風になる。",
        where=[("TradingView: US10Y-JP10Y と入力", TV + "TVC-US10Y/")],
        sign="この差が急低下し始めたら「急速な円高（＝日本株の輸出企業への逆風）」の警戒サイン。※本ツールは日本側が月次データのため、日次のチャートはTradingViewで確認する。"),
}
MANUAL = [dict(
    name="米国信用買い残（マージン・デット）", where=[("検索: FINRA Margin Statistics", "https://www.google.com/search?q=FINRA+Margin+Statistics")],
    why="投資家が借金して買っている株の残高。レバレッジ（借金の膨らみ）の度合いを表す。",
    sign="毎月下旬に月次データを無料公開。過去最高水準まで膨れ上がったあと、前月比で減少に転じた瞬間が「レバレッジの巻き戻し（強制売却）」開始のサイン。", note="自動取得は未対応のため、手動で確認する。")]


def guide_html(name):
    g = GUIDE.get(name)
    if not g:
        return ""
    links = " ／ ".join(f'<a href="{u}" target="_blank" rel="noopener">{t}</a>' for t, u in g["where"])
    return (f'<details class="g" open><summary>何のために・どこを見るか</summary>'
            f'<p><b>何のために</b><br>{g["why"]}</p><p><b>どこを見るか</b><br>{links}</p>'
            f'<p><b>見るべきポイント（危険サイン）</b><br>{g["sign"]}</p></details>')


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
{guide_html(i['name'])}<div class="asof">{('データ日付 ' + i['asof']) if i['asof'] else ''}</div></section>"""
    manual = "".join(
        f'''<section class="card na"><div class="top"><h2>{m['name']}</h2><span class="badge">手動確認</span></div><div class="note">{m['note']}</div>
<details class="g" open><summary>何のために・どこを見るか</summary><p><b>何のために</b><br>{m['why']}</p><p><b>どこを見るか</b><br>{" ／ ".join(f'<a href="{u}" target="_blank" rel="noopener">{t}</a>' for t, u in m['where'])}</p>
<p><b>見るべきポイント（危険サイン）</b><br>{m['sign']}</p></details></section>''' for m in MANUAL)
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
.how,.asof{{font-size:12px;color:var(--mute);margin-top:6px}}details.g{{margin-top:8px;font-size:13px}}details.g summary{{cursor:pointer;color:var(--mute)}}details.g p{{margin:6px 0}}a{{color:inherit;text-decoration:underline}}.sec{{font-size:16px;margin:22px 0 8px}}footer{{color:var(--mute);font-size:12px;margin-top:18px}}
</style></head><body><main><h1>暴落警戒モニター</h1>
<div class="sum">{head}（危険 {cnt[BAD]} ／ 注意 {cnt[WARN]} ／ 平常 {cnt[OK]} ／ 取得失敗 {cnt[NA]}）　更新 {d['updated']}</div>
<div class="grid">{cards}</div>
<h2 class="sec">自動取得していない指標（手動で確認）</h2><div class="grid">{manual}</div>
<footer>データ: FRED・米財務省・Yahoo Finance・multpl.com。判定は目安であり、投資助言ではありません。個々のしきい値は monitor.py で調整できます。</footer></main></body></html>"""


if __name__ == "__main__":
    main()
