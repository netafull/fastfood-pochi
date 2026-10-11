"""PDFの読み取りテスト用の合成フィクスチャ。

公式PDFの実データ(名前・数値)はテストに入れない。ここで作るのは、実物と同じ「構造」
(表の列の罫線・セル結合したメニュー名のセル・縦書き風に分割した名前・番号と記号の列・
○▲が1つの文字行に詰まるセル など)を、創作した名前と数値で再現した、
pdf_nutrition.parse_*() への入力(ページごとの texts/rules/chars の辞書)。
"""

from __future__ import annotations

# --- 共通 -------------------------------------------------------------------


def box(x0, y0, x1, y1, t):
    return [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1), t]


def rect_v(x, y0, y1):
    return [x - 0.3, y0, x + 0.3, y1]


def rect_h(x0, x1, y):
    return [x0, y - 0.6, x1, y + 0.6]


def energy(p, f, c):
    """創作した値の kcal を、検算に通る値にする(4×たんぱく質+9×脂質+4×炭水化物)。"""
    return round(4 * p + 9 * f + 4 * c)


# --- すき家: 栄養成分 ---------------------------------------------------------

SK_EDGES = [51.5, 99.6, 212.1, 266.5, 320.9, 375.2, 429.6, 484.0, 543.1]
SK_PITCH = 11.9


def sukiya_nutrition_page(blocks, updated: str | None = "2026年9月29日", header=True):
    """blocks: [(名前の見せ方, [(サイズ|None, p, f, c, salt, kcal|None), ...])]
    名前の見せ方: 文字列=1行 / ("v", "ラ","イ","ト")=縦に1文字ずつ / ("multi", [名前,...], [各名前の行数])=複数メニューを1セルに。"""
    texts, rules = [], []
    if updated:
        texts.append(box(488.7, 772.1, 539.0, 776.8, f"更新日　{updated}"))
    top = 676.2
    if header:
        for x0, t in [(141.7, "メニュー"), (229.1, "サイズ"), (279.9, "カロリー"), (330.8, "たんぱく質"),
                      (395.7, "脂質"), (443.0, "炭水化物"), (496.3, "食塩相当量")]:
            texts.append(box(x0, 697.7, x0 + 9 * len(t), 704.8, t))
        for x in SK_EDGES:
            rules.append(rect_v(x, 677.4, 712.2))
    rules.append(rect_h(52.1, 543.7, top))
    y = top
    for name, rows in blocks:
        h = SK_PITCH * len(rows)
        mid = y - h / 2
        if isinstance(name, str):
            texts.append(box(101.3, mid - 4.5, 101.3 + 9 * len(name), mid + 4.5, name))
        elif name[0] == "v":
            chars = name[1:]
            for i, ch in enumerate(chars):
                cyi = mid + (len(chars) / 2 - i - 0.5) * 12
                texts.append(box(101.3, cyi - 4.5, 110.3, cyi + 4.5, ch))
        else:  # multi: 各名前を、その名前のサイズ行の中央に置く
            _, names, counts = name
            r = 0
            for nm, k in zip(names, counts):
                cyi = y - SK_PITCH * (r + k / 2)
                texts.append(box(101.3, cyi - 4.5, 101.3 + 9 * len(nm), cyi + 4.5, nm))
                r += k
        for i, row in enumerate(rows):
            size, p, f, c, salt, kcal = row
            ry = y - SK_PITCH * (i + 0.5)
            k = energy(p, f, c) if kcal is None else kcal
            if size:
                texts.append(box(213.7, ry - 3.5, 213.7 + 7 * len(size), ry + 3.5, size))
            for col, v in zip(range(3, 8), [str(k), f"{p:.1f}", f"{f:.1f}", f"{c:.1f}", f"{salt:.1f}"]):
                right = SK_EDGES[col + 1] - 4
                texts.append(box(right - 4.5 * len(v), ry - 3.5, right, ry + 3.5, v))
        y -= h
        rules.append(rect_h(100.1, 543.7, y))
    return {"texts": texts, "rules": rules}


# --- すき家: アレルゲン -------------------------------------------------------

AL_NAMES = ["小麦", "そば", "卵", "乳", "落花生", "えび", "かに", "くるみ", "あわび", "いか", "いくら", "オレンジ",
            "キウイ", "牛肉", "さけ", "さば", "大豆", "鶏肉", "豚肉", "もも", "やまいも", "りんご", "ゼラチン",
            "バナナ", "ごま", "アーモンド", "カシュー", "マカダミア"]
AL_COLX = [224.7 + 20.5 * k for k in range(28)]


def sukiya_allergen_page(rows, updated: str | None = "2026/9/29更新", merge_first_mark=True):
    """rows: [(id, 名前, 28個の記号文字列 '●△-'...)]。見出しの品目名は縦に1文字ずつ。"""
    texts = []
    if updated:
        texts.append(box(779.7, 576.6, 818.5, 582.5, updated))
        texts.append(box(60, 356.0, 677.7, 363.5, "●・・・原材料として使用している　△・・・原材料由来および製造工場で同じラインを使う　ー・・・使用していない※"))
    hy = 434.5
    for k in range(28):
        x = AL_COLX[k]
        texts.append(box(x - 1.6, hy, x + 1.6, hy + 5.2, str(k + 1)))
        nm = AL_NAMES[k]
        for i, ch in enumerate(nm):
            yy = hy - 8 - 6 * i
            texts.append(box(x - 2, yy - 5, x + 2, yy, ch))
    y0 = 345.7
    for n, (rid, name, marks) in enumerate(rows):
        y = y0 - 9.7 * n
        if rid is None:
            pass
        if n == 1:  # 番号と名前が1つの文字行になる行
            texts.append(box(85.3, y, 85.3 + 6 * (len(name) + 3), y + 8.5, f"{rid} {name}"))
        else:
            texts.append(box(88.0, y, 93.3, y + 8.5, str(rid)))
            if merge_first_mark and n == 2:  # 名前の直後に最初の記号が続く行
                texts.append(box(103.0, y, 229.5, y + 9.5, f"{name} {marks[0]}"))
                marks = "\0" + marks[1:]
            else:
                texts.append(box(103.1, y, 103.1 + 9 * len(name), y + 8.5, name))
        for k, m in enumerate(marks):
            if m == "\0":
                continue
            x = AL_COLX[k]
            texts.append(box(x - 4.8, y, x + 4.8, y + 9.5, m))
    return {"texts": texts, "rules": []}


# --- 吉野家 ------------------------------------------------------------------

YO_EDGES = [105.0, 144.0, 189.0, 518.0, 596.0, 694.0, 792.0, 889.0, 987.0, 1085.0] + \
           [1085.0 + 39.2 * k for k in range(1, 29)]
YO_PITCH = 27.0
YO_AL = ["小麦", "乳", "卵", "落花生", "そば", "えび", "かに", "くるみ", "あわび", "いか", "いくら", "オレンジ",
         "キウイ", "牛肉", "さけ", "さば", "大豆", "鶏肉", "豚肉", "マカダミア", "バナナ", "もも", "山芋",
         "りんご", "ごま", "カシュー", "ゼラチン", "アーモンド"]


def yoshinoya_page(blocks, lang="ja", issue=262, date="2026年10月8日現在"):
    """blocks: [(名前, [注記の行...], [(サイズ|None, p, f, c, salt, {品目の添字: '○'|'▲'}), ...])]"""
    W, H = 2289.7, 3238.2
    texts, rules, chars = [], [], []
    texts.append(box(180.5, 3161.2, 442.8, 3183.3, f"吉野家メニュー情報　{issue}号"))
    texts.append(box(1970.7, 3160.0, 2179.5, 3184.0, date))
    title = {"ja": "メニュー別・栄養成分・アレルギー物質一覧", "en": "NUTRITION ・ALLERGEN　LIST"}[lang]
    texts.append(box(1002.7, 3116.9, 1521.7, 3145.1, title))
    head = {"ja": "栄 養 成 分  （1食当たり）", "en": "NUTRITION (per serving)"}[lang]
    texts.append(box(722.0, 3056.9, 959.5, 3079.0, head))
    top, hb = 3080.0, 2811.0
    for y in (top, 3055.0, 3029.7, hb):
        rules.append([106.0, y - 0.3, 2184.2, y + 0.3])
    # 縦罫線(表の左端から右端まで)
    bottom = hb - YO_PITCH * sum(len(r) for _, _, r in blocks) - 60
    for x in YO_EDGES + [2184.0]:
        rules.append([x - 0.3, bottom, x + 0.3, top])
    texts.append(box(618.1, 2825.4, 671.8, 2845.3, "（kcaｌ）"))
    # 見出し(品目名は縦に1文字ずつ。chars に入れる)
    for k, nm in enumerate(YO_AL):
        cxk = 1085.0 + 39.2 * (k + 0.5)
        for i, ch in enumerate(nm):
            yy = 3008 - 24 * i
            chars.append(box(cxk - 10, yy - 22, cxk + 10, yy, ch))
    y = hb
    bounds = [hb]
    for name, notes, rows in blocks:
        h = max(YO_PITCH * len(rows), 60 + 22 * len(notes))
        mid = y - h / 2
        texts.append(box(192.5, y - 30, 192.5 + 22 * len(name), y - 8, name))
        for j, note in enumerate(notes):
            texts.append(box(192.5, y - 50 - 20 * j, 192.5 + 18 * len(note), y - 32 - 20 * j, note))
        for i, (size, p, f, c, salt, marks) in enumerate(rows):
            ry = y - YO_PITCH * (i + 0.5)
            if size:
                texts.append(box(537.3, ry - 10, 577.0, ry + 10, size))
            k = energy(p, f, c)
            kc = f"{k:,}"
            texts.append(box(682.7 - 12 * len(kc), ry - 12, 682.7, ry + 12, kc))
            for col, v in zip((5, 6, 7), [f"{p:.1f}", f"{f:.1f}", f"{c:.1f}"]):
                right = YO_EDGES[col + 1] - 12
                texts.append(box(right - 12 * len(v), ry - 12, right, ry + 12, v))
            salt_s = f"{salt:.1f}"
            mk_cols = sorted(marks)
            if mk_cols:  # 食塩の数値に、最初の品目の記号がくっついて1つの文字行になる
                texts.append(box(1045.1, ry - 12, 1045.1 + 12 * len(salt_s) + 25 * len(mk_cols), ry + 12,
                                 salt_s + " " + " ".join(marks[k] for k in mk_cols)))
            else:
                texts.append(box(1045.1, ry - 12, 1117.1, ry + 12, salt_s))
            for k2 in mk_cols:
                cxk = 1085.0 + 39.2 * (k2 + 0.5)
                chars.append(box(cxk - 11, ry - 12, cxk + 11, ry + 12, marks[k2]))
        y -= h
        bounds.append(y)
        rules.append([189.9, y - 0.3, 517.2, y + 0.3])
        rules.append([189.7, y - 0.5, 517.5, y + 0.5])
    return {"w": W, "h": H, "texts": texts, "rules": rules, "chars": chars}


def yoshinoya_intro_page():
    """表の前の説明ページ(○ ▲ の凡例)。"""
    return {"texts": [
        box(107.2, 810.5, 1808.7, 836.5, "アレルギー情報は商品の原材料を調べたものです。（○：原材料に含みます。　　▲：原材料「あおのり」には『かに』が共生しています。"),
        box(78.6, 807.3, 99.3, 836.8, "4."),
        box(107.2, 783.0, 579.2, 809.0, "『えび』が混ざる漁法で採取されています。）"),
        box(78.6, 734.2, 1762.8, 763.6, "5. 揚げ物は全て同じフライヤーで揚げています。"),
    ], "rules": [], "chars": []}
