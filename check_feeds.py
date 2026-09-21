#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""feeds.yml に書かれた情報源が本当に取得できるかを確認するスクリプト。

情報源を追加・変更したときに実行すると、URL の打ち間違いや
配信停止になったフィードをその場で見つけられます。

  python check_feeds.py          # 有効な情報源だけ確認する
  python check_feeds.py --all    # enabled: false のものも含めて確認する

1つでも取得できない情報源があると、終了コード 1 (失敗) になります。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

from collect import (
    JST,
    entry_datetime,
    fetch_feed,
    load_config,
    strip_html,
)

BASE_DIR = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="情報源(フィード)が取得できるか確認する")
    parser.add_argument("--config", default=str(BASE_DIR / "feeds.yml"))
    parser.add_argument("--all", action="store_true",
                        help="enabled: false の情報源も確認する")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    timeout = int((config.get("settings") or {}).get("request_timeout", 20))

    feeds = [f for f in config["feeds"] if isinstance(f, dict) and f.get("url")]
    if not args.all:
        feeds = [f for f in feeds if f.get("enabled", True) is not False]

    print("=" * 78)
    print(f"情報源の取得確認  {dt.datetime.now(JST):%Y-%m-%d %H:%M:%S} (日本時間)")
    print(f"対象: {len(feeds)} 件")
    print("=" * 78)

    ok_list: list[str] = []
    ng_list: list[tuple[str, str, str]] = []

    for index, feed in enumerate(feeds, start=1):
        name = str(feed.get("name", "(名前なし)"))
        url = str(feed["url"])
        category = str(feed.get("category", "other"))

        print(f"\n[{index}/{len(feeds)}] {name}  <{category}>")
        print(f"    {url}")
        try:
            parsed, error = fetch_feed(url, timeout)
        except Exception as exc:
            parsed, error = None, f"想定外のエラー: {exc.__class__.__name__}: {exc}"

        if error is not None:
            print(f"    NG  {error}")
            ng_list.append((name, url, error))
        else:
            newest = parsed.entries[0]
            when = entry_datetime(newest)
            when_text = when.strftime("%Y-%m-%d %H:%M") if when else "日付なし"
            title = strip_html(newest.get("title") or "")
            print(f"    OK  記事 {len(parsed.entries)} 件 / 最新: {when_text} / {title[:60]}")
            ok_list.append(name)
        time.sleep(0.7)

    print("\n" + "=" * 78)
    print(f"結果: OK {len(ok_list)} 件 / NG {len(ng_list)} 件")
    if ng_list:
        print("\n取得できなかった情報源 (feeds.yml から消すか、enabled: false にしてください):")
        for name, url, error in ng_list:
            print(f"  - {name}")
            print(f"      {url}")
            print(f"      理由: {error}")
    print("=" * 78)
    return 1 if ng_list else 0


if __name__ == "__main__":
    sys.exit(main())
