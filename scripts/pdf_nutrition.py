#!/usr/bin/env python3
"""吉野家・すき家の公式PDF(栄養成分・アレルゲン)を、文字と座標から表として読み取る。

このリポジトリは基本的に Python 標準ライブラリだけで動くが、ここだけは例外で
`pdfminer.six`(純Python。バージョンは requirements-pdf.txt で固定)を使う。
未インストールのときは import 時に落とさず、`extract_geometry` を呼んだときだけ
ImportError にする(呼び出し側 pdf_update.py が警告を出してPDFの読み取りだけを飛ばす)。

構成:
  - extract_geometry(): PDF -> ページごとの「文字行のbbox・罫線のbbox」(JSONにできる素の辞書)。pdfminerを使う唯一の場所
  - parse_*(): ページの辞書 -> 構造化データ。純粋関数で、pdfminerにも通信にも依存しない(テストはここを合成データで行う)
  - match_*() / build_*(): 商品名の照合と、商品データに入れる形への変換。これも純粋関数

PDFの表は、メニュー名のセルが複数サイズの行をまたぐ(セル結合)・縦に1文字ずつ分割されている、
という作りで、素のテキスト順では名前と数値が対応しない。そこで座標を使う:
  - すき家(栄養): セルの罫線(細い矩形)でメニュー名のセルの上下端を取り、その中に入る数値行を割り当てる
  - すき家(アレルゲン): 見出しの番号1〜28の列位置(x)で、行ごとの記号 ●/△/- を列に対応づける
  - 吉野家: 縦罫線で列を、名前セルの横罫線で行のまとまりを取る。数値の右の ○▲ は文字単位のxで列に対応づける

規約上、PDFの表そのものを保存・公開してはならない(複製・転載の禁止)。このモジュールは表を
返すが、呼び出し側は「サイトで記録している商品に一致した行の数値」だけを商品データに入れ、
PDF全体のダンプを保存しない。

images.zensho.co.jp(すき家のPDFの置き場)の robots.txt は403を返す。PDF本体は200で取れ、
公式ページ(メニューのページ)がこのURLへ直接リンクしている。そのため、公式ページが示す
URLを、更新があったときだけ取得する運用にしている(pdf_update.py)。
"""

from __future__ import annotations

import io
import logging
import re
import unicodedata

# ---------------------------------------------------------------------------
# 共通
# ---------------------------------------------------------------------------

ENERGY_REL_TOL = 0.15  # kcal ≈ 4×たんぱく質 + 9×脂質 + 4×炭水化物 の許容誤差(±15%)
ENERGY_ABS_TOL = 6.0   # 数kcalの小さい値は、丸めで15%を超えるので絶対誤差も許す


def cx(t) -> float:
    return (t[0] + t[2]) / 2


def cy(t) -> float:
    return (t[1] + t[3]) / 2


def normalize_name(s: str | None) -> str:
    """商品名の照合用: NFKC正規化して空白(全角含む)を除く。"""
    # 波ダッシュ(U+301C)は全角チルダ(U+FF5E)と見た目が同じで、NFKCでも別の文字のまま残る。公式サイトとPDFで使い分けがあるので同じにする
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "").replace("\u301c", "~"))


def to_number(s: str | None) -> float | None:
    if s is None:
        return None
    s = unicodedata.normalize("NFKC", s).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def energy_ok(kcal, protein, fat, carbs) -> bool:
    """エネルギー検算。数値が欠けていれば False。"""
    if None in (kcal, protein, fat, carbs) or kcal < 0:
        return False
    calc = 4 * protein + 9 * fat + 4 * carbs
    return abs(calc - kcal) <= max(ENERGY_REL_TOL * kcal, ENERGY_ABS_TOL)


def _cluster(vals, tol=3.0):
    out: list[list[float]] = []
    for v in sorted(vals):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


def _assign_centered(names: list, kc: list) -> list[str] | None:
    """複数のメニュー名を1つのセルに縦に並べたブロック(サイド・ドリンク等)で、各名前を行に割り当てる。
    メニュー名のセルは、そのメニューのサイズ行(1〜3行)の中央に名前が置かれる。そこで各行を最も近い名前に
    割り当て、(1)どの名前にも行が付き、(2)名前の高さがその行たちの中央と合う、ときだけ採用する。合わなければ None。"""
    groups: dict = {}
    for j, k in enumerate(kc):
        i = min(range(len(names)), key=lambda i: abs(cy(names[i]) - cy(k)))
        groups.setdefault(i, []).append(j)
    if len(groups) != len(names):
        return None
    out: list = [None] * len(kc)
    for i, idxs in groups.items():
        center = sum(cy(kc[j]) for j in idxs) / len(idxs)
        if abs(cy(names[i]) - center) > 7:
            return None
        for j in idxs:
            out[j] = names[i][4]
    return out


def _row_from_numbers(name, size, vals, extra=None) -> dict:
    kcal, pro, fat, carb, salt = (to_number(v) for v in vals)
    row = {
        "name": name, "size": size,
        "kcal": kcal, "protein_g": pro, "fat_g": fat, "carbs_g": carb, "salt_g": salt,
    }
    row["ok"] = energy_ok(kcal, pro, fat, carb) and salt is not None
    if extra:
        row.update(extra)
    return row


# ---------------------------------------------------------------------------
# pdfminer による座標つきの抽出(これだけが pdfminer に依存する)
# ---------------------------------------------------------------------------

def extract_geometry(data: bytes, with_chars: bool = False) -> list[dict]:
    """PDFのバイト列 -> ページごとの {"texts","rules","chars"?}。

    texts: [x0,y0,x1,y1,文字列](文字行ごと。PDFの座標系で、yは下が小さい)
    rules: 罫線として使われている細い矩形・線 [x0,y0,x1,y1]
    chars: 1文字ずつ [x0,y0,x1,y1,文字](with_chars=True のときだけ。縦書き風の見出しや、1つの文字行に
           複数の列の記号が詰まって出るセルを、文字のxで列に割り当てるために使う)
    """
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LAParams, LTChar, LTLine, LTRect, LTTextLine
    except ImportError as e:  # 呼び出し側が警告を出してPDFの読み取りを飛ばす
        raise ImportError("pdfminer.six が見つかりません(pip install -r requirements-pdf.txt)") from e
    # フォントの色指定への警告がページ数ぶん出て、ログが埋まるため
    logging.getLogger("pdfminer").setLevel(logging.ERROR)

    pages = []
    for page in extract_pages(io.BytesIO(data), laparams=LAParams(char_margin=1.0)):
        texts: list = []
        rules: list = []
        chars: list = []

        def walk(o) -> None:
            if isinstance(o, LTTextLine):
                t = o.get_text().strip()
                if with_chars:
                    for c in o:
                        if isinstance(c, LTChar) and c.get_text().strip():
                            chars.append([round(c.x0, 1), round(c.y0, 1), round(c.x1, 1), round(c.y1, 1), c.get_text()])
                if t:
                    texts.append([round(o.x0, 1), round(o.y0, 1), round(o.x1, 1), round(o.y1, 1), t])
            elif isinstance(o, (LTRect, LTLine)):
                w, h = o.x1 - o.x0, o.y1 - o.y0
                if min(w, h) < 4 and max(w, h) > 10:
                    rules.append([round(o.x0, 1), round(o.y0, 1), round(o.x1, 1), round(o.y1, 1)])
            elif hasattr(o, "__iter__"):
                for c in o:
                    walk(c)

        walk(page)
        d = {"texts": texts, "rules": rules}
        if with_chars:
            d["chars"] = chars
        pages.append(d)
    return pages


# ---------------------------------------------------------------------------
# すき家: 栄養成分PDF
# ---------------------------------------------------------------------------

SUKIYA_HEADS = ["メニュー", "サイズ", "カロリー", "たんぱく質", "脂質", "炭水化物", "食塩相当量"]
_DATE_JP = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_DATE_SLASH = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")


def _iso(m) -> str:
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def parse_sukiya_nutrition(pages: list[dict]) -> dict:
    """-> {"updated": "YYYY-MM-DD"|None, "rows": [...], "pages_total", "pages_table", "problems": [...]}

    行 = {"name","size","kcal","protein_g","fat_g","carbs_g","salt_g","ok","block","ambiguous"}
    ambiguous: 1つのセル結合の中に名前が複数並び、名前と数値の対応が座標では決められない行(採用しない)。
    """
    updated = None
    problems: list[str] = []
    rows: list[dict] = []
    pages_table = 0
    for pi, p in enumerate(pages):
        T, R = p["texts"], p["rules"]
        if updated is None:
            for t in T:
                m = re.search(r"更新日\s*" + _DATE_JP.pattern, t[4])
                if m:
                    updated = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
                    break
        hd = {t[4]: t for t in T if t[4] in SUKIYA_HEADS}
        if len(hd) < len(SUKIYA_HEADS):
            problems.append(f"p{pi + 1}: 見出し行が見つかりません")
            continue
        top = max(t[3] for t in hd.values()) + 2
        vx = sorted({round((r[0] + r[2]) / 2, 1) for r in R
                     if r[2] - r[0] < 4 and r[3] - r[1] > 20 and r[3] > top - 25})
        cols = list(zip(vx, vx[1:]))

        def colidx(t, cols=cols):
            for i, (a, b) in enumerate(cols):
                if a <= cx(t) <= b:
                    return i
            return None

        idx = [colidx(hd[h]) for h in SUKIYA_HEADS]
        if None in idx or idx != [idx[0] + k for k in range(len(SUKIYA_HEADS))]:
            problems.append(f"p{pi + 1}: 列の罫線と見出しの位置が合いません")
            continue
        pages_table += 1
        # メニュー名のセルの上下端 = メニュー列から始まる(または表の左端から続く)横罫線
        ys = sorted({round((r[1] + r[3]) / 2, 1) for r in R
                     if r[2] - r[0] > 150 and r[3] - r[1] < 4 and r[0] <= cols[idx[0]][0] + 2
                     and (r[1] + r[3]) / 2 < top})
        for bi, (lo, hi) in enumerate(zip(ys, ys[1:])):
            inb = [t for t in T if lo < cy(t) < hi]
            kc = sorted((t for t in inb if colidx(t) == idx[2]), key=lambda t: -cy(t))
            names = sorted((t for t in inb if colidx(t) == idx[0]), key=lambda t: -cy(t))
            if not kc:
                continue
            # 名前が1行ごとに数値行と同じ高さに並ぶブロック(サイド・ドリンクの一覧)
            per_row = len(names) == len(kc) > 1 and all(abs(cy(n) - cy(k)) < 4 for n, k in zip(names, kc))
            joined = "".join(t[4] for t in names)  # 縦書き風に1文字ずつ分割された名前も、上から順につなぐ
            # 長めの名前が複数行ならんでいて、1行ごとに数値行とも対応しない: 複数のメニューをまとめたセル(つなぐと別の名前になる)
            multi_menu = len(names) > 1 and not per_row and any(len(n[4]) > 2 for n in names)
            centered = _assign_centered(names, kc) if multi_menu else None
            for j, k in enumerate(kc):
                sz = [t for t in inb if colidx(t) == idx[1] and abs(cy(t) - cy(k)) < 3]
                vals = []
                for q in range(2, 7):
                    c = [t for t in inb if colidx(t) == idx[q] and abs(cy(t) - cy(k)) < 3]
                    vals.append(c[0][4] if len(c) == 1 else None)
                name = names[j][4] if per_row else (centered[j] if centered else joined)
                size = sz[0][4] if len(sz) == 1 else None
                # サイズの無い行が複数あるのに名前が1行ごとに並んでいない: 対応が決められない
                ambiguous = (not name) or (multi_menu and not centered) \
                    or (len(kc) > 1 and size is None and not per_row and not centered)
                rows.append(_row_from_numbers(name, size, vals, {"block": f"{pi + 1}-{bi}", "ambiguous": ambiguous}))
    return {"updated": updated, "rows": rows, "pages_total": len(pages), "pages_table": pages_table,
            "problems": problems}


# ---------------------------------------------------------------------------
# すき家: アレルゲンPDF
# ---------------------------------------------------------------------------

SUKIYA_MARKS = "●△-"


def parse_sukiya_allergen(pages: list[dict]) -> dict:
    """-> {"updated","allergen_names":[28],"rows":[{"id","name","marks":[28],"complete"}],"legend": {...},"pages_table"}

    28品目の列見出しは縦書きで1文字ずつ分割されているので、見出し番号(1〜28)の列位置xに
    見出しの文字を集め、同じ列位置の記号(● 使用 / △ 同じラインで製造 / - 不使用)を対応づける。
    """
    updated = None
    names28: list[str] | None = None
    rows: list[dict] = []
    legend: dict = {}
    pages_table = 0
    problems: list[str] = []
    for pi, p in enumerate(pages):
        T = p["texts"]
        for t in T:
            if updated is None:
                m = re.search(_DATE_SLASH.pattern + "更新", t[4])
                if m:
                    updated = _iso(m)
            if "△・・・" in t[4] and not legend:
                m = re.search(r"△・・・(.*?)(?:　|ー・・・|$)", t[4])
                legend = {"●": "原材料として使用している", "△": (m.group(1).strip() if m else None)}
        by_y: dict = {}
        for t in T:
            if re.fullmatch(r"\d{1,2}", t[4]) and cx(t) > 200:
                by_y.setdefault(round(cy(t)), []).append(t)
        hdr = None
        for _, lst in by_y.items():
            lst = sorted(lst, key=lambda t: t[0])
            if [int(t[4]) for t in lst] == list(range(1, 29)):
                hdr = lst
        if not hdr:
            continue
        colx = [cx(t) for t in hdr]
        hy = cy(hdr[0])

        def col(t, colx=colx):
            k = min(range(28), key=lambda i: abs(colx[i] - cx(t)))
            return k if abs(colx[k] - cx(t)) < 9 else None

        # 商品名の直後に最初の列の記号がくっついて1つの文字行になることがある: 分けておく
        T2 = []
        for t in T:
            if len(t[4]) > 1 and t[4][-1] in SUKIYA_MARKS and t[2] > colx[0] - 6 and t[0] < colx[0] and cy(t) < hy - 5:
                mid = t[2] - 9.6
                T2.append([t[0], t[1], mid, t[3], t[4][:-1].rstrip()])
                T2.append([mid, t[1], t[2], t[3], t[4][-1]])
            else:
                T2.append(t)
        marks = [t for t in T2 if t[4] in tuple(SUKIYA_MARKS) and cy(t) < hy - 5 and col(t) is not None]
        groups: list[list] = []
        for t in sorted(marks, key=lambda t: -cy(t)):
            if groups and abs(cy(groups[-1][0]) - cy(t)) < 3:
                groups[-1].append(t)
            else:
                groups.append([t])
        if not groups:
            continue
        pages_table += 1
        first_top = max(t[3] for t in groups[0])
        band = [t for t in T2 if len(t[4]) == 1 and t[4] not in SUKIYA_MARKS and hy > cy(t) > first_top + 1
                and col(t) is not None and cx(t) > 200]
        page_names = ["".join(t[4] for t in sorted((t for t in band if col(t) == k), key=lambda t: -cy(t)))
                      for k in range(28)]
        if names28 is None:
            names28 = page_names
        elif page_names != names28:
            problems.append(f"p{pi + 1}: 品目の見出しがページ間で食い違います")
        for g in groups:
            y = cy(g[0])
            left = sorted((t for t in T2 if t[0] >= 80 and cx(t) < 215 and abs(cy(t) - y) < 4 and cy(t) < hy - 5),
                          key=lambda t: t[0])
            txt = "".join(t[4] for t in left)
            m = re.match(r"^(\d+)\s*(.*)$", txt)
            mk: list = [None] * 28
            for t in g:
                mk[col(t)] = t[4]
            rows.append({"id": int(m.group(1)) if m else None, "name": (m.group(2).strip() if m else txt),
                         "marks": mk, "complete": bool(m and m.group(2).strip()) and None not in mk})
    return {"updated": updated, "allergen_names": names28 or [], "rows": rows, "legend": legend,
            "pages_table": pages_table, "pages_total": len(pages), "problems": problems}


# ---------------------------------------------------------------------------
# 吉野家: 栄養成分・アレルゲンPDF(日本語ページだけ)
# ---------------------------------------------------------------------------

_NUM = re.compile(r"^[\d,]+(?:\.\d+)?$")
_NUM_WITH_MARKS = re.compile(r"^([\d,]+(?:\.\d+)?)(?:\s*[○▲])*$")


def _is_yoshinoya_ja_table(p: dict) -> bool:
    """日本語の表のページ: 「栄 養 成 分」の見出しと「(kcal)」の単位がある。
    英語・中国語・韓国語のページ(同じ品目が重複する)は見出しの文言が違うので除く。ページ番号は決め打ちしない。"""
    T = p["texts"]
    has_head = any(re.match(r"^栄\s*養\s*成\s*分", unicodedata.normalize("NFKC", t[4])) for t in T)
    has_unit = any(unicodedata.normalize("NFKC", t[4]).startswith("(kca") for t in T)
    return has_head and has_unit


def parse_yoshinoya(pages: list[dict]) -> dict:
    """-> {"issue","date","legend","allergen_names","rows","pages_total","pages_table","problems"}

    行 = {"name","size","kcal",...,"salt_g","ok","marks":[28個。○ ▲ None]}
    """
    issue = date = None
    legend = {"○": None, "▲": None}
    problems: list[str] = []
    rows: list[dict] = []
    names28: list[str] | None = None
    pages_table = 0
    for pi, p in enumerate(pages):
        T, R = p["texts"], p["rules"]
        for t in T:
            if issue is None:
                m = re.search(r"吉野家メニュー情報\s*(\d+)号", t[4])
                if m:
                    issue = int(m.group(1))
            if date is None:
                m = re.search(_DATE_JP.pattern + "現在", t[4])
                if m:
                    date = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        # 凡例(○ ▲ の意味)は、表の前の説明ページにある
        if legend["○"] is None:
            lines = sorted(T, key=lambda t: -t[1])
            for i, t in enumerate(lines):
                if "○：" in t[4] or "〇：" in t[4]:
                    txt = t[4]
                    for nxt in lines[i + 1:i + 6]:
                        # 注記が次の行に続くとき。「4.」だけの番号の部分は飛ばし、次の注記(「5. …」)や「※」で止める
                        if nxt[1] < t[1] - 120 or nxt[4].startswith("※") or re.match(r"^\d+\.\s*\S", nxt[4]):
                            break
                        if not re.fullmatch(r"\d+\.", nxt[4]):
                            txt += nxt[4]
                    m1 = re.search(r"[○〇]：(.*?)(?:\s*▲|$)", txt)
                    m2 = re.search(r"▲：(.*)$", txt)
                    legend["○"] = m1.group(1).strip() if m1 else None
                    legend["▲"] = re.sub(r"\s+", " ", m2.group(1)).strip().rstrip("）)") if m2 else None
                    break
        if not _is_yoshinoya_ja_table(p):
            continue
        if "chars" not in p:
            problems.append(f"p{pi + 1}: 文字単位の座標がありません")
            continue
        V = [r for r in R if r[3] - r[1] > 300 and r[2] - r[0] < 6]
        ex = _cluster([(r[0] + r[2]) / 2 for r in V])
        unit = [t for t in T if unicodedata.normalize("NFKC", t[4]).startswith("(kca")][0]
        kc = next((i for i in range(len(ex) - 1) if ex[i] <= cx(unit) <= ex[i + 1]), None)
        if kc is None or kc < 2:
            problems.append(f"p{pi + 1}: 縦罫線から列を決められません")
            continue
        size_col = (ex[kc - 1], ex[kc])
        val_cols = [(ex[kc + j], ex[kc + j + 1]) for j in range(5) if kc + j + 1 < len(ex)]
        al_cols = [(ex[i], ex[i + 1]) for i in range(kc + 5, len(ex) - 1)]
        if len(val_cols) != 5 or len(al_cols) != 28:
            problems.append(f"p{pi + 1}: 列の数が合いません(数値{len(val_cols)}・品目{len(al_cols)})")
            continue
        lefts = [x for x in ex[:kc - 1] if x > 150]
        if not lefts:
            problems.append(f"p{pi + 1}: メニュー名の列が見つかりません")
            continue
        name_col = (min(lefts), ex[kc - 1])
        H = [r for r in R if r[2] - r[0] > 150 and r[3] - r[1] < 4]
        long_ys = [r[1] for r in H if r[2] - r[0] > 1500 and r[1] < unit[1]]
        if not long_ys:
            problems.append(f"p{pi + 1}: 見出しの下の罫線がありません")
            continue
        hb = max(long_ys)  # 見出し行の下端 = データ行の上端
        bounds = sorted(_cluster([(r[1] + r[3]) / 2 for r in H
                                  if r[0] <= name_col[0] + 50 and r[2] >= name_col[1] - 4 and r[2] - r[0] > 250
                                  and (r[1] + r[3]) / 2 < hb + 1], tol=2), reverse=True)
        if len(bounds) < 2:
            problems.append(f"p{pi + 1}: 行の区切りの罫線が足りません")
            continue
        pages_table += 1
        chars = p["chars"]
        page_names = ["".join(c[4] for c in sorted((c for c in chars if a <= cx(c) <= b and hb < cy(c) < hb + 218),
                                                   key=lambda c: -cy(c)))
                      for a, b in al_cols]
        if names28 is None:
            names28 = page_names
        elif names28 != page_names:
            problems.append(f"p{pi + 1}: 品目の見出しがページ間で食い違います")

        def in_col(t, c):
            return c[0] <= cx(t) <= c[1]

        for bi, (lo, hi) in enumerate(zip(bounds[1:], bounds[:-1])):
            inb = [t for t in T if lo < cy(t) < hi]
            lines = sorted((t for t in inb if in_col(t, name_col)), key=lambda t: -cy(t))
            head: list[str] = []
            for t in lines:  # 名前の下には「※…」の注記が続くので、注記より前の行だけを名前とする
                if t[4].startswith("※"):
                    break
                head.append(t[4])
            name = "".join(head)
            kcs = sorted((t for t in inb if val_cols[0][0] <= t[0] < val_cols[0][1] and _NUM.match(t[4])),
                         key=lambda t: -cy(t))
            for k in kcs:
                sz = [t for t in inb if in_col(t, size_col) and abs(cy(t) - cy(k)) < 8]
                vals = []
                for q in range(5):
                    c = [t for t in inb if val_cols[q][0] <= t[0] < val_cols[q][1] and abs(cy(t) - cy(k)) < 8]
                    v = None
                    for t in c:
                        m = _NUM_WITH_MARKS.match(t[4])
                        if m:
                            v = m.group(1)  # 食塩の数値には、最初の品目の記号がくっついて出ることがある
                    vals.append(v)
                marks: list = [None] * 28
                for c in chars:
                    if abs(cy(c) - cy(k)) < 8 and c[4] in ("○", "▲"):
                        for j, a in enumerate(al_cols):
                            if a[0] <= cx(c) <= a[1]:
                                marks[j] = c[4]
                rows.append(_row_from_numbers(
                    name, sz[0][4] if len(sz) == 1 else None, vals,
                    {"marks": marks, "block": f"{pi + 1}-{bi}", "ambiguous": not name}))
    return {"issue": issue, "date": date, "legend": legend, "allergen_names": names28 or [], "rows": rows,
            "pages_total": len(pages), "pages_table": pages_table, "problems": problems}


# ---------------------------------------------------------------------------
# 検証(誤った栄養値を出さないためのゲート)
# ---------------------------------------------------------------------------

def pass_rate(rows: list[dict]) -> float:
    """曖昧でない行のうち、数値がそろいエネルギー検算に通った行の割合。"""
    usable = [r for r in rows if not r.get("ambiguous")]
    return (sum(1 for r in usable if r["ok"]) / len(usable)) if usable else 0.0


def validate_source(kind: str, parsed: dict, min_rows: int = 80, min_rate: float = 0.8) -> str | None:
    """PDF全体の読み取り結果を確かめる。問題があれば理由(文字列)、なければ None。
    見出しが見つからない・行が少なすぎる・検算の通過率が低い、はいずれもPDFの構造変更の疑い。"""
    if parsed.get("pages_table", 0) == 0:
        return "表の見出し行が1ページも見つかりません(PDFの構造が変わった疑い)"
    rows = parsed["rows"]
    if len(rows) < min_rows:
        return f"読み取れた行が少なすぎます({len(rows)}行 < {min_rows})"
    if kind in ("sukiya_nutrition", "yoshinoya"):
        rate = pass_rate(rows)
        if rate < min_rate:
            return f"エネルギー検算に通った行が少なすぎます({rate:.0%} < {min_rate:.0%})"
    if kind == "sukiya_allergen":
        done = sum(1 for r in rows if r["complete"]) / len(rows)
        if done < min_rate or len(parsed["allergen_names"]) != 28:
            return f"アレルゲンの表を読み取れた割合が低すぎます({done:.0%}・品目{len(parsed['allergen_names'])})"
    if kind == "sukiya_nutrition" and not parsed.get("updated"):
        return "更新日が見つかりません"
    if kind == "yoshinoya" and (parsed.get("issue") is None or not parsed.get("date")):
        return "号数・日付が見つかりません"
    return None


# ---------------------------------------------------------------------------
# 照合と、商品データに入れる形への変換
# ---------------------------------------------------------------------------

REPRESENTATIVE_SIZE = "並盛"


def rows_for_name(rows: list[dict], name: str) -> list[dict] | None:
    """商品名と完全一致(NFKC・空白除去)する行のまとまりを返す。曖昧(同名が別の値で複数ある)なら None。
    曖昧な部分一致はしない(別商品に当てないため)。"""
    key = normalize_name(name)
    hit = [r for r in rows if not r.get("ambiguous") and normalize_name(r["name"]) == key]
    if not hit:
        return None
    blocks: dict = {}
    for r in hit:
        blocks.setdefault(r.get("block"), []).append(r)
    if len(blocks) > 1:
        sigs = {tuple((r["size"], r["kcal"], r["protein_g"], r["fat_g"], r["carbs_g"], r["salt_g"]) for r in b)
                for b in blocks.values()}
        if len(sigs) > 1:
            return None
        return list(blocks.values())[0]
    return hit


def build_nutrition(rows: list[dict]) -> tuple[dict | None, list[dict] | None, int]:
    """行のまとまり -> (代表の栄養, サイズ別リスト(2件以上のときだけ), 検算で捨てた行数)。
    代表は「並盛」があれば並盛、なければ先頭行(価格の代表と同じ考え方)。リストは代表を先頭にする。"""
    good = [r for r in rows if r["ok"]]
    dropped = len(rows) - len(good)
    if not good:
        return None, None, dropped
    rep = next((r for r in good if r["size"] == REPRESENTATIVE_SIZE), good[0])
    ordered = [rep] + [r for r in good if r is not rep]

    def nut(r: dict) -> dict:
        return {"kcal": r["kcal"], "protein_g": r["protein_g"], "fat_g": r["fat_g"], "carbs_g": r["carbs_g"],
                "sugar_g": None, "fiber_g": None, "salt_g": r["salt_g"], "other": {}}

    variants = [{"label": r["size"] or "", "nutrition": nut(r)} for r in ordered] if len(ordered) > 1 else None
    return nut(rep), variants, dropped


def allergens_from_marks(names: list[str], mark_lists: list[list], contains_mark: str, partial_mark: str) -> dict:
    """記号のリスト(サイズごと)を、品目名の「含む」「条件つき」にまとめる。
    サイズで記号が違うときは、安全側に倒して「含む」を優先する(どれかのサイズで含むなら含む)。"""
    contains, partial = [], []
    for j, nm in enumerate(names):
        marks = {ml[j] for ml in mark_lists if j < len(ml)}
        if contains_mark in marks:
            contains.append(nm)
        elif partial_mark in marks:
            partial.append({"name": nm, "mark": partial_mark})
    return {"contains": contains, "partial": partial}


def match_sukiya_allergen(parsed: dict, name: str) -> dict | None:
    key = normalize_name(name)
    hit = [r for r in parsed["rows"] if r["complete"] and normalize_name(r["name"]) == key]
    if not hit:
        return None
    if len({tuple(r["marks"]) for r in hit}) > 1:
        return None  # 同名で記号が違う品目が複数ある
    return allergens_from_marks(parsed["allergen_names"], [hit[0]["marks"]], "●", "△")
