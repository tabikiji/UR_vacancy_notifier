"""UR賃貸 公式サイトの内部API クライアント.

サイトのページ（https://www.ur-net.go.jp/chintai/...）が裏で呼んでいる JSON API を使う。
- 団地一覧（エリア検索）: POST bukken/result/bukken_result/
- 団地ごとの空室一覧    : POST bukken/detail/detail_bukken_room/
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, asdict

import requests

API_BASE = "https://chintai.r6.ur-net.go.jp/chintai/api/"
SITE_BASE = "https://www.ur-net.go.jp"
REQUEST_INTERVAL_SEC = 1.5  # サイト負荷を抑えるためのリクエスト間隔
MAX_PAGES = 30  # 念のための上限

_session = requests.Session()
_session.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (personal UR vacancy watcher; low-frequency)",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": SITE_BASE,
        "Referer": SITE_BASE + "/chintai/",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
)
_last_request = 0.0


def _post(path: str, data: dict) -> list[dict]:
    global _last_request
    wait = REQUEST_INTERVAL_SEC - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    for attempt in range(3):
        try:
            r = _session.post(API_BASE + path, data=data, timeout=30)
            _last_request = time.time()
            r.raise_for_status()
            body = r.json()
            return body if isinstance(body, list) else []
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    return []


# ---------- 値の整形 ----------

def _to_int_yen(s: str | None) -> int | None:
    if not s:
        return None
    m = re.sub(r"[^\d]", "", s)
    return int(m) if m else None


def _to_float_m2(s: str | None) -> float | None:
    if not s:
        return None
    m = re.search(r"[\d.]+", html.unescape(s))
    return float(m.group()) if m else None


def _to_floor(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


# ---------- 団地ページの概要（管理物件全体の家賃・間取り） ----------

def _throttle() -> None:
    wait = REQUEST_INTERVAL_SEC - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)


def fetch_danchi_summary(url: str) -> dict:
    """団地ページ上部の「家賃(共益費)」「間取り/床面積」（管理物件全体の情報）を取得する。

    空室の有無に関係なく取れる。取れなかった項目は None。
    ※表示中の家賃には定期借家等の割引適用後の家賃を含む場合がある（下限は割引込みの可能性）。
    """
    global _last_request
    _throttle()
    r = _session.get(url, timeout=30)
    _last_request = time.time()
    r.raise_for_status()
    if "charset" not in r.headers.get("Content-Type", "").lower():
        r.encoding = r.apparent_encoding
    text = html.unescape(re.sub(r"<[^>]+>", " ", r.text))
    text = re.sub(r"\s+", " ", text)
    return parse_danchi_summary(text)


def parse_danchi_summary(text: str) -> dict:
    """タグ除去・空白正規化済みのページ文字列から家賃・間取り・床面積を抜き出す。"""
    out = {"rent_min": None, "rent_max": None, "commonfee": None,
           "madori": None, "area_min": None, "area_max": None}
    m = re.search(
        r"家賃\s*[(（]\s*共益費\s*[)）]\s*([\d,]+)\s*円\s*(?:[～~〜]\s*([\d,]+)\s*円)?"
        r"\s*[(（]\s*([\d,]+)\s*円\s*[)）]", text)
    if m:
        out["rent_min"] = int(m[1].replace(",", ""))
        out["rent_max"] = int(m[2].replace(",", "")) if m[2] else out["rent_min"]
        out["commonfee"] = int(m[3].replace(",", ""))
    m = re.search(
        r"間取り\s*/\s*床面積\s*(.+?)\s*/\s*([\d.]+)\s*(?:㎡|m2|m²)?"
        r"\s*(?:[～~〜]\s*([\d.]+)\s*(?:㎡|m2|m²))?", text)
    if m:
        out["madori"] = m[1].strip()
        out["area_min"] = float(m[2])
        out["area_max"] = float(m[3]) if m[3] else out["area_min"]
    return out


# ---------- 通勤時間（駅→駅の所要時間） ----------

def reachable_stations(station_cd: str, max_min: int) -> dict[str, tuple[int, str]]:
    """UR サイトの所要時間データ（駅から60分以内の駅一覧）から、max_min 分以内の駅を返す。

    戻り値: {駅コード: (所要分, 駅名)}。起点駅自身も 0 分で含む。
    ※駅→駅の電車の所要時間（日中平均・待ち時間含まず）。駅から物件までの徒歩は含まない。
    """
    global _last_request
    url = f"{SITE_BASE}/chintai/common/xml/cost-time/cost-time_{int(station_cd):08d}.xml"
    r = _session.get(url, timeout=30)
    _last_request = time.time()
    r.raise_for_status()
    text = r.content.decode("euc-jp", errors="replace")
    result = {str(station_cd): (0, "")}
    for m in re.finditer(
        r'<stationTo code="(\d+)">\s*<stationName>([^<]*)</stationName>\s*<costTime>(\d+)</costTime>',
        text,
    ):
        code, name, cost = m.group(1), m.group(2), int(m.group(3))
        if cost <= max_min:
            result[code] = (cost, name)
    return result


def commute_station_condition(commute: list[dict]) -> list[str]:
    """複数の通勤先の条件を「すべて」満たす駅コードの一覧（積集合）。"""
    sets = [set(reachable_stations(c["station_cd"], int(c["max_min"]))) for c in commute]
    return sorted(set.intersection(*sets))


# ---------- 団地一覧 ----------

def search_danchi_by_commute(
    block: str, tdfk: str, base_station_cd: str, station_condition: list[str], walk: str = ""
) -> list[dict]:
    """通勤時間検索（mode=leadtime）で団地一覧を返す。各要素の roomCount が現在の空室数。"""
    results: list[dict] = []
    page_size = 50
    for page in range(MAX_PAGES):
        data = {
            "rent_low": "", "rent_high": "", "walk": str(walk or ""),
            "floorspace_low": "", "floorspace_high": "", "years": "",
            "station_cd": base_station_cd, "station_condition": ",".join(station_condition),
            "station_cost": "60", "station_change": "5",
            "mode": "leadtime", "eki": base_station_cd, "block": block,
            "tdfk": tdfk, "rireki_tdfk": tdfk,
            "orderByField": "1", "pageSize": str(page_size), "pageIndex": str(page),
            "shisya": "", "danchi": "", "shikibetu": "", "pageIndexRoom": "0", "sp": "",
        }
        rows = _post("bukken/result/bukken_result/", data)
        results.extend(rows)
        if len(rows) < page_size:
            break
    return results


def search_all(search_cfg: dict) -> list[dict]:
    """config.yaml の search 設定で、全都道府県の団地一覧を返す（重複除去済み）。"""
    commute = search_cfg["commute"]
    cond = commute_station_condition(commute)
    seen: dict[str, dict] = {}
    for tdfk in search_cfg["prefectures"]:
        for b in search_danchi_by_commute(
            search_cfg.get("block", "kanto"), str(tdfk), str(commute[0]["station_cd"]),
            cond, search_cfg.get("walk_max", ""),
        ):
            seen.setdefault(danchi_key(b), b)
    return list(seen.values())


def danchi_key(b: dict) -> str:
    return f"{b['shisya']}_{b['danchi']}{b['shikibetu']}"


# ---------- 空室一覧（フェーズB） ----------

@dataclass
class Room:
    key: str            # 一意キー: 20_1310_000011301
    danchi_key: str     # 20_1310
    danchi_name: str
    room_id: str
    name: str           # 11号棟301号室
    type: str           # 1LDK
    rent: int | None
    commonfee: int | None
    floorspace: float | None
    floor: int | None
    url: str
    madori_img: str | None

    @property
    def total_rent(self) -> int | None:
        if self.rent is None:
            return None
        return self.rent + (self.commonfee or 0)

    def to_dict(self) -> dict:
        return asdict(self)


def fetch_rooms(shisya: str, danchi: str, shikibetu: str, danchi_name: str = "") -> list[Room]:
    """団地の空室を全件返す（ページングを辿る）。"""
    rooms: dict[str, Room] = {}
    danchi_key = f"{shisya}_{danchi}{shikibetu}"
    for page in range(MAX_PAGES):
        data = {
            "rent_low": "", "rent_high": "", "floorspace_low": "", "floorspace_high": "",
            "shisya": shisya, "danchi": danchi, "shikibetu": shikibetu,
            "newBukkenRoom": "", "orderByField": "0", "orderBySort": "0",
            "pageIndex": str(page), "sp": "",
        }
        rows = _post("bukken/detail/detail_bukken_room/", data)
        if not rows:
            break
        new_count = 0
        for r in rows:
            rid = r.get("id")
            if not rid:
                continue
            key = f"{danchi_key}_{rid}"
            if key in rooms:
                continue
            new_count += 1
            link = r.get("roomDetailLink") or ""
            rooms[key] = Room(
                key=key,
                danchi_key=danchi_key,
                danchi_name=danchi_name,
                room_id=rid,
                name=r.get("name") or rid,
                type=r.get("type") or "",
                rent=_to_int_yen(r.get("rent")),
                commonfee=_to_int_yen(r.get("commonfee")),
                floorspace=_to_float_m2(r.get("floorspace")),
                floor=_to_floor(r.get("floor")),
                url=SITE_BASE + link if link.startswith("/") else link,
                madori_img=r.get("madori"),
            )
        all_count = int(rows[0].get("allCount") or 0)
        if new_count == 0 or len(rooms) >= all_count:
            break
    return list(rooms.values())
