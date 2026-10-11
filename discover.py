"""フェーズA: 通勤条件から候補団地を洗い出し、watchlist の下書き（candidates.yaml）を作る（初回・手動）.

使い方:
    python discover.py                 # → candidates.yaml
出力を見て、監視したい団地だけ残し watchlist.yaml として保存する。
"""
from __future__ import annotations

import argparse
import re
import unicodedata

import yaml

from ur_client import SITE_BASE, danchi_key, search_all


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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-o", "--out", default="candidates.yaml")
    args = ap.parse_args()

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
    candidates.sort(key=lambda c: c["key"])

    header = (
        "# 候補団地リスト（discover.py が生成）\n"
        "# 監視したい団地だけ残して watchlist.yaml として保存してください。\n"
        "# 必須なのは key と name だけです（他は参考情報）。\n"
    )
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump({"danchi": candidates}, f, allow_unicode=True, sort_keys=False, width=200)
    print(f"{len(candidates)} 団地を {args.out} に出力しました")


if __name__ == "__main__":
    main()
