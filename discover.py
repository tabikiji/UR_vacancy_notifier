"""フェーズA: 通勤条件から候補団地を洗い出し、watchlist の下書き（candidates.yaml）を作る（初回・手動）.

使い方:
    python discover.py                 # → candidates.yaml（家賃・間取りの列つき）
    python discover.py --enrich watchlist.yaml
                                       # 既存の watchlist.yaml に家賃・間取りの列だけを追記（検索はしない）
出力を見て、監視したい団地だけ残し watchlist.yaml として保存する。
"""
from __future__ import annotations

import argparse
import re
import unicodedata

import yaml

from ur_client import SITE_BASE, danchi_key, fetch_danchi_summary, search_all


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"</li>\s*<li>", " / ", s)
    return re.sub(r"<[^>]+>", "", s).strip()


def station_walk_min(traffic: str) -> int | None:
    """交通情報から「駅から直接徒歩」の最短分数を返す（バス利用ルートは除外）。

    例: 「○○」駅 徒歩11～16分 → 11 / 「○○」駅バス7分 徒歩2分 → 対象外
    """
    t = unicodedata.normalize("NFKC", traffic or "")
    mins = [int(m.group(1)) for m in re.finditer(r"駅\s*(?:から)?\s*徒歩\s*(\d+)", t)]
    return min(mins) if mins else None


def add_summary(c: dict) -> None:
    """団地ページから家賃・間取りを取得して c に追記する（失敗しても止めない）。

    家賃・間取りは「管理物件全体」の情報で、空室がなくても取れる。
    rent_range / madori_range は目視用、rent_min / rent_max は並べ替え・除外用。
    """
    try:
        s = fetch_danchi_summary(c["url"])
    except Exception as e:  # 1団地の失敗で止めない
        print(f"  詳細取得失敗 {c.get('name')}({c.get('key')}): {e}")
        s = {}
    c["rent_range"] = (f"{s['rent_min']:,}～{s['rent_max']:,}円（共益費 {s['commonfee']:,}円）"
                       if s.get("rent_min") and s.get("commonfee") is not None else None)
    c["madori_range"] = (f"{s['madori']} / {s['area_min']:g}～{s['area_max']:g}㎡"
                         if s.get("madori") else None)
    c["rent_min"] = s.get("rent_min")
    c["rent_max"] = s.get("rent_max")


def enrich_file(path: str) -> None:
    """既存の yaml（watchlist.yaml など）に、家賃・間取りが未設定の団地だけ追記する。先頭のコメントは保持。"""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    header_lines = []
    for line in text.splitlines(keepends=True):
        if line.startswith("#") or not line.strip():
            header_lines.append(line)
        else:
            break
    data = yaml.safe_load(text)["danchi"]
    todo = [c for c in data if not c.get("rent_range")]
    print(f"{path}: {len(data)} 団地中 {len(todo)} 団地の家賃・間取りを取得します")
    for i, c in enumerate(todo, 1):
        add_summary(c)
        if i % 20 == 0:
            print(f"  詳細取得 {i}/{len(todo)}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(header_lines))
        yaml.safe_dump({"danchi": data}, f, allow_unicode=True, sort_keys=False, width=200)
    print(f"{path} を更新しました")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-o", "--out", default="candidates.yaml")
    ap.add_argument("--enrich", metavar="FILE",
                    help="検索せず、既存の yaml（watchlist.yaml など）に家賃・間取りの列だけを追記する")
    args = ap.parse_args()

    if args.enrich:
        enrich_file(args.enrich)
        return

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    rows = search_all(cfg["search"])
    walk_max = cfg["search"].get("walk_max")
    # UR 側の「駅徒歩」条件はバス＋徒歩も含むため、駅から直接徒歩で walk_max 以内の団地に絞る
    if walk_max:
        before = len(rows)
        rows = [b for b in rows
                if (w := station_walk_min(strip_html(b.get("traffic")))) is not None and w <= int(walk_max)]
        print(f"駅徒歩{walk_max}分以内（バス除く）: {before} → {len(rows)} 団地")
    candidates = [
        {
            "key": danchi_key(b),
            "name": b.get("danchiNm"),
            "place": b.get("place"),
            "traffic": strip_html(b.get("traffic")),
            "station_walk_min": station_walk_min(strip_html(b.get("traffic"))),
            "url": f"{SITE_BASE}/chintai/{b.get('block')}/{b.get('tdfk')}/{danchi_key(b)}.html",
            "vacancies_now": int(b.get("roomCount") or 0),
        }
        for b in rows
    ]
    for i, c in enumerate(candidates, 1):  # 団地ページから家賃・間取りを取得
        add_summary(c)
        if i % 20 == 0:
            print(f"  詳細取得 {i}/{len(candidates)}")
    candidates.sort(key=lambda c: c["key"])

    header = (
        "# 候補団地リスト（discover.py が生成）\n"
        "# 監視したい団地だけ残して watchlist.yaml として保存してください。\n"
        "# 必須なのは key と name だけです（他は参考情報）。\n"
        "# rent_range / madori_range は団地ページの「管理物件全体」の情報（空室がなくても出る。下限は割引込みの場合あり）。\n"
    )
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump({"danchi": candidates}, f, allow_unicode=True, sort_keys=False, width=200)
    print(f"{len(candidates)} 団地を {args.out} に出力しました")


if __name__ == "__main__":
    main()
