#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""貯めた記事 (articles.json) から、閲覧用のページ docs/index.html を作る。

ひな型は template.html です。見た目を変えたいときはそちらを編集してください。
出力は1枚のHTMLファイルで完結するので、GitHub Pages でも、
パソコンでファイルをダブルクリックして開いても、同じように見られます。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_STORE = BASE_DIR / "articles.json"
DEFAULT_TEMPLATE = BASE_DIR / "template.html"
DEFAULT_OUTPUT = BASE_DIR / "docs" / "index.html"

PLACEHOLDER = "__DATA_JSON__"

# ページに埋め込まなくてよい項目 (重複判定にしか使わない)
INTERNAL_FIELDS = ("url_key", "title_key")

JST = dt.timezone(dt.timedelta(hours=9), "JST")


def escape_for_script(text: str) -> str:
    """<script> の中に安全に置けるようにする。

    JSON の中に "</script>" のような文字列があるとページが壊れるため、
    < > & を JSON のエスケープ表記に置き換える (意味は変わりません)。
    """
    return (text.replace("&", r"&")
                .replace("<", r"<")
                .replace(">", r">"))


def build_page(store: dict[str, Any], template: str) -> str:
    articles = []
    for article in store.get("articles", []):
        trimmed = {k: v for k, v in article.items() if k not in INTERNAL_FIELDS}
        articles.append(trimmed)

    payload = {
        "generated_at": store.get("generated_at"),
        "new_articles": store.get("new_articles", 0),
        "feeds": store.get("feeds", []),
        "articles": articles,
    }
    data_json = escape_for_script(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    if PLACEHOLDER not in template:
        raise SystemExit(f"ひな型に {PLACEHOLDER} が見つかりません。template.html を確認してください。")
    return template.replace(PLACEHOLDER, data_json)


def main() -> int:
    parser = argparse.ArgumentParser(description="閲覧用のHTMLを作る")
    parser.add_argument("--store", default=str(DEFAULT_STORE))
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    store_path = Path(args.store)
    template_path = Path(args.template)
    output_path = Path(args.output)

    if not store_path.exists():
        raise SystemExit(
            f"記事データ {store_path} がありません。先に collect.py を実行してください。")
    if not template_path.exists():
        raise SystemExit(f"ひな型 {template_path} がありません。")

    with store_path.open(encoding="utf-8") as f:
        store = json.load(f)
    template = template_path.read_text(encoding="utf-8")

    page = build_page(store, template)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(page, encoding="utf-8")

    articles = store.get("articles", [])
    feeds = store.get("feeds", [])
    counts: dict[str, int] = {}
    for article in articles:
        counts[article.get("category", "other")] = counts.get(article.get("category", "other"), 0) + 1

    print(f"ページを作成しました: {output_path}")
    print(f"  記事 {len(articles)} 件 "
          f"(Claude {counts.get('claude', 0)} / Copilot {counts.get('copilot', 0)} "
          f"/ その他AI {counts.get('other', 0)})")
    print(f"  情報源 {len(feeds)} 件 (うち取得失敗 {sum(1 for f in feeds if not f.get('ok'))} 件)")
    print(f"  ファイルサイズ {output_path.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
