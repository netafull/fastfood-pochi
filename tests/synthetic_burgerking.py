"""バーガーキング用の縮めたフィクスチャ。

構造(電文の応答JSONのキー)は2026-11-08に調べた実物と同じだが、項目は必要なものだけに絞り、
画像URL・長い見出しは入れていない(公式の応答そのものはリポジトリに入れない方針)。商品名・価格・日付は
実物に基づく事実情報の一部だが、テストのために創作・変更した値もある。
"""

from __future__ import annotations

import json


def ok(body: dict, trcode: str) -> str:
    return json.dumps({"header": {"result": True, "trcode": trcode, "error_code": ""}, "body": body}, ensure_ascii=False)


def mi(cd: str, name: str, price: int, range_yn: str = "N") -> dict:
    return {"menuCd": cd, "menuNm": name, "dineInPrc": str(price), "prcRangeYn": range_yn, "menuFlagList": []}


# メニュー一覧(BKJ0508、期間限定で絞り込み後)。同じ商品が「おすすめ」と別カテゴリに重複して出る
GUILTY = mi("8000391", "ギルティ ザ・ワンパウンダー", 2490, "Y")
AUTUMN = mi("8000388", "オータムSpecial マッシュルームワッパー", 990, "Y")
MUSH = mi("8000376", "マッシュルームワッパー", 990, "Y")
FRIES = mi("2000341", "ギルティ・フレンチフライ", 400)
KING = mi("4000131", "キングフュージョン~made with KITKAT®~", 490)
CHOCO = mi("4000133", "My Premiumショコラ チョコ＆キャラメル", 250)
AVOCADO_SET = mi("7001726", "アボカドワッパーJr.セット", 700)
ONBEEF = mi("8000399", "オン ザ ビーフ", 890, "Y")


def menu(items_by_cat: dict[str, list[dict]] | None = None) -> str:
    cats = {
        ("X000000001", "朝メニュー"): [AVOCADO_SET],
        ("X000000002", "おすすめ"): [GUILTY, AUTUMN, MUSH, FRIES, KING, CHOCO],
        ("X000000004", "ワッパー"): [AUTUMN, MUSH],
        ("X000000006", "バーガー他"): [GUILTY],
        ("X000000007", "サイド"): [FRIES],
        ("X000000008", "デザート"): [CHOCO, KING],
    }
    if items_by_cat is not None:
        cats = {(f"X00000000{i}", n): v for i, (n, v) in enumerate(items_by_cat.items(), 1)}
    return ok({"allMenuList": [{"menuCategoryCd": cd, "menuCategoryNm": nm, "menuCategorySeq": "1", "menuInfo": v}
                               for (cd, nm), v in cats.items()]}, "BKJ0508")


def detail(menu_cd: str, name: str, price: int, sets: list[tuple[str, int]], limited: bool = True) -> str:
    info = [{"menuCd": f"s{i}", "menuNm": f"{name}{lb}", "dineInPrc": str(p)} for i, (lb, p) in enumerate(sets)]
    info.append({"menuCd": menu_cd, "menuNm": name, "dineInPrc": str(price)})
    return ok({"menuCd": menu_cd, "menuNm": name, "dineInPrc": str(price), "menuInfoList": info, "menuDesc": None,
               "menuCalorie": None,
               "menuKeywordList": [{"menuKeywordNm": "期間限定", "menuKeywordPk": "期間限定"}] if limited else []}, "BKJ0510")


def news(items: list[tuple[str, str, str, str]] | None = None) -> str:
    """(id, 公開日yyyymmdd, 見出し, 種別 P/N)"""
    items = items if items is not None else NEWS
    return ok({"dataCount": str(len(items)), "data": [
        {"idNotice": i, "dtStart": d, "dtEnd": "99991231", "dsTitle": t, "categoryType": c,
         "categoryName": "プレスリリース" if c == "P" else "お知らせ", "pdfUrl": "https://example.invalid/x.pdf"}
        for i, d, t, c in items]}, "BKJ0506")


NEWS = [
    ("208", "20261106", "全国400店を突破！福島県に初上陸！11月から12月中旬に一挙16店を新規出店！", "P"),
    ("207", "20261105", "直火焼きのビーフが旨い超大型バーガー『ギルティ ザ・ワンパウンダー』新発売！「特製ガーリックソース」が極限の旨さ！", "P"),
    ("206", "20261104", "「わたくしの」デザートシリーズから新登場！『わたくしのプレミアムショコラ チョコレート&キャラメル』", "P"),
    ("205", "20261030", "公式アプリ 会員登録情報の変更に関するお知らせ", "N"),
    ("204", "20261029", "本場のハロウィーンを楽しもう！店頭で「トリック オア トリート」と唱えた18歳以下のお客様に「ソフトクリーム」を無料プレゼント！", "P"),
    ("202", "20261022", "大人気「マッシュルームワッパー」に新作！「オリジナルハーブソース」を使用！「オータムスペシャル マッシュルームワッパー」新発売", "P"),
    ("201", "20261021", "新作サンデーが登場！『キングフュージョン ～made with KITKAT®～』新発売！", "P"),
]


def events(items: list[tuple[str, str, str, str, str]]) -> str:
    """(eventId, 開始, 終了, 状態 C/E, 見出し)"""
    return ok({"eventList": [
        {"eventId": i, "dtStart": s, "dtEnd": e, "tpStatusEvent": st, "eventTitle": t, "cdTypeEvent": "01",
         "imageUrlWeb": "https://example.invalid/x.jpg"} for i, s, e, st, t in items],
        "onGoingEventCount": "1", "endEventCount": "1"}, "BKJ0501")


EV_OPEN = [
    ("115", "20261106", "99991231", "C", "超大型バーガー『ギルティ ザ・ワンパウンダー』新発売！4枚肉の圧倒的ボリューム"),
    ("105", "20260918", "99991231", "C", "秋の風物詩バーガー「マッシュルームワッパー」が今年も復活発売！"),
    ("111", "20261023", "99991231", "C", "新作サンデーが登場！『キングフュージョン ～made with KITKAT®～』新発売！"),
]
EV_ENDED = [
    ("110", "20261016", "20261022", "E", "直火焼きのビーフを味わえる「オン ザ ビーフ」が1週間限定で復活！"),
    ("106", "20260918", "20261001", "E", "公式アプリで『マッシュルームワッパー』をお得に楽しめます！300円引きクーポンを2週間配信！"),
    ("113", "20261023", "20261029", "E", "秋は、やっぱりきのこ。新商品の「オータムスペシャル マッシュルームワッパー」を含むセットが最大23％オフ"),
]
