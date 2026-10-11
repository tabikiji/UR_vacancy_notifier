"""フェーズB: watchlist.yaml の団地の空室を確認し、条件に合う「新しい空室」を Discord に通知する.

環境変数:
    DISCORD_WEBHOOK_URL  通知先 Webhook（未設定なら標準出力に表示するだけ）
    STATE_BUCKET         前回結果を保存する GCS バケット名（未設定ならローカル state.json）
    DRY_RUN=1            通知せず、内容を表示するだけ

使い方:
    python monitor.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
import yaml

from ur_client import Room, danchi_key, fetch_rooms, search_all

JST = timezone(timedelta(hours=9))
STATE_OBJECT = "ur-vacancy-state.json"
LOCAL_STATE = "state.json"


# ---------- 状態の保存（前回見えていた空室） ----------

def load_state() -> dict | None:
    bucket = os.environ.get("STATE_BUCKET")
    if bucket:
        from google.cloud import storage

        blob = storage.Client().bucket(bucket).blob(STATE_OBJECT)
        if not blob.exists():
            return None
        return json.loads(blob.download_as_text())
    if not os.path.exists(LOCAL_STATE):
        return None
    with open(LOCAL_STATE, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    text = json.dumps(state, ensure_ascii=False, indent=1)
    bucket = os.environ.get("STATE_BUCKET")
    if bucket:
        from google.cloud import storage

        storage.Client().bucket(bucket).blob(STATE_OBJECT).upload_from_string(
            text, content_type="application/json"
        )
    else:
        with open(LOCAL_STATE, "w", encoding="utf-8") as f:
            f.write(text)


# ---------- 条件フィルタ ----------

def matches(room: Room, f: dict) -> bool:
    rent = room.total_rent if f.get("include_commonfee", True) else room.rent
    if f.get("rent_max") and (rent is None or rent > f["rent_max"]):
        return False
    if f.get("rent_min") and (rent is None or rent < f["rent_min"]):
        return False
    if f.get("madori") and room.type not in f["madori"]:
        return False
    if f.get("floorspace_min") and (room.floorspace is None or room.floorspace < f["floorspace_min"]):
        return False
    if f.get("floor_min") and (room.floor is None or room.floor < f["floor_min"]):
        return False
    return True


# ---------- Discord 通知 ----------

def _yen(v: int | None) -> str:
    return f"{v:,}円" if v is not None else "-"


def room_embed(room: Room) -> dict:
    fields = [
        {"name": "間取り", "value": room.type or "-", "inline": True},
        {"name": "面積", "value": f"{room.floorspace:g}㎡" if room.floorspace else "-", "inline": True},
        {"name": "階", "value": f"{room.floor}階" if room.floor else "-", "inline": True},
        {"name": "家賃", "value": _yen(room.rent), "inline": True},
        {"name": "共益費", "value": _yen(room.commonfee), "inline": True},
        {"name": "合計", "value": _yen(room.total_rent), "inline": True},
    ]
    embed = {
        "title": f"{room.danchi_name} {room.name}",
        "url": room.url,
        "color": 0x2E8B57,
        "fields": fields,
    }
    if room.madori_img:
        # 寝室の広さ（〇畳）はAPIに無く間取り図にしか載っていないため、間取り図を大きく表示
        embed["image"] = {"url": room.madori_img}
    return embed


def notify(content: str, embeds: list[dict]) -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    dry = os.environ.get("DRY_RUN") == "1" or not webhook
    # Discord は 1メッセージ最大10 embed
    chunks = [embeds[i : i + 10] for i in range(0, len(embeds), 10)] or [[]]
    for i, chunk in enumerate(chunks):
        payload = {"content": content if i == 0 else "", "embeds": chunk}
        if dry:
            print("[DRY_RUN] Discord:", json.dumps(payload, ensure_ascii=False, indent=1))
            continue
        for _ in range(3):
            r = requests.post(webhook, json=payload, timeout=30)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)))
                continue
            r.raise_for_status()
            break


# ---------- メイン ----------

def main() -> int:
    with open(os.environ.get("CONFIG", "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    with open(os.environ.get("WATCHLIST", "watchlist.yaml"), encoding="utf-8") as f:
        watch = yaml.safe_load(f)["danchi"]
    flt = cfg.get("filters", {})
    opts = cfg.get("notify", {})

    # 1) 通勤条件の検索1回（数リクエスト）で、各団地の現在の空室数を取得
    #    → 空室がある「監視対象の団地」だけ部屋一覧を取りに行く（アクセス数を最小化）
    vacancy_count: dict[str, int] = {}
    try:
        for b in search_all(cfg["search"]):
            vacancy_count[danchi_key(b)] = int(b.get("roomCount") or 0)
    except Exception as e:
        print(f"一覧検索に失敗。全監視団地を個別に確認します: {e}")

    current: dict[str, Room] = {}
    errors: list[str] = []
    failed_keys: set[str] = set()  # 取得に失敗した団地（前回の状態を引き継ぐ）
    checked = 0
    for d in watch:
        # 一覧に載っていて空室0なら、部屋一覧は取りに行かない
        if vacancy_count.get(d["key"], -1) == 0:
            continue
        checked += 1
        shisya, rest = d["key"].split("_")
        danchi, shikibetu = rest[:-1], rest[-1]
        try:
            for room in fetch_rooms(shisya, danchi, shikibetu, d.get("name", d["key"])):
                if matches(room, flt):
                    current[room.key] = room
        except Exception as e:  # 1団地の失敗で全体を止めない
            errors.append(f"{d.get('name')}({d['key']}): {e}")
            failed_keys.add(d["key"])
            # 途中まで取れた部屋は捨てる（前回の状態を丸ごと引き継ぐため）
            current = {k: r for k, r in current.items() if r.danchi_key != d["key"]}
    print(f"監視 {len(watch)} 団地（うち空室あり {checked}）/ 条件に合う空室 {len(current)} 件 / エラー {len(errors)} 件")

    prev = load_state()
    first_run = prev is None
    prev_rooms: dict = (prev or {}).get("rooms", {})

    new_rooms = [r for k, r in current.items() if k not in prev_rooms]
    # 取得に失敗した団地の部屋は「募集終了」とみなさない
    gone = [v for k, v in prev_rooms.items()
            if k not in current and v.get("danchi_key") not in failed_keys]
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")

    if first_run:
        if opts.get("notify_existing_on_first_run", True):
            notify(f"🏠 UR空室監視を開始しました（{now}）。現在の該当空室: {len(new_rooms)}件",
                   [room_embed(r) for r in new_rooms])
    elif new_rooms:
        notify(f"🆕 条件に合う新しい空室が {len(new_rooms)} 件出ました（{now}）",
               [room_embed(r) for r in new_rooms])

    if gone and not first_run and opts.get("notify_gone", False):
        lines = "\n".join(f"・{g['danchi_name']} {g['name']}" for g in gone)
        notify(f"📭 募集終了になった空室（{now}）\n{lines}", [])

    # 失敗が続く（サイト仕様変更など）場合に気づけるよう通知
    if errors and len(errors) == checked:
        notify(f"⚠️ UR空室監視: 全団地で取得に失敗しました（{now}）\n" + "\n".join(errors[:5]), [])
        return 1  # 状態は更新しない

    # 失敗した団地は前回の部屋をそのまま残す（次回取れたときに「新着」と誤判定して再通知しないため）
    rooms_state = {k: r.to_dict() for k, r in current.items()}
    for k, v in prev_rooms.items():
        if v.get("danchi_key") in failed_keys:
            rooms_state.setdefault(k, v)
    save_state({"updated_at": now, "rooms": rooms_state})
    return 0


if __name__ == "__main__":
    sys.exit(main())
