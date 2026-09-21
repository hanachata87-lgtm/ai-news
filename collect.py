#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RSS / Atom フィードから生成AI関連の新着記事を集めて JSON に貯めるスクリプト。

やっていること:
  1. feeds.yml に書かれた情報源 (フィード) を順番に取りに行く
  2. 取れた記事を「タイトル・リンク・情報源・日付・概要・カテゴリ」に整える
  3. すでに持っている記事 (URL が同じもの) は無視する
  4. 新しい記事だけを articles.json に足して保存する

1つのフィードが落ちても、そこで止まらずに次のフィードへ進みます。
どのフィードが成功して、どれが失敗したかは実行ログにも JSON にも残します。
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests
import yaml

# このファイルが置かれているフォルダ
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "feeds.yml"
DEFAULT_STORE = BASE_DIR / "articles.json"

JST = dt.timezone(dt.timedelta(hours=9), "JST")

# ブラウザからのアクセスに見せる。これがないと弾くサイトがあるため。
USER_AGENT = (
    "Mozilla/5.0 (compatible; ai-news-collector/1.0; "
    "+https://github.com/hanachata87-lgtm/ai-stock)"
)

# リンクに付いてくる広告用パラメータ。重複判定の邪魔になるので消す。
TRACKING_PARAMS = (
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src",
    "spm", "at_medium", "at_campaign", "CMP", "cmpid", "ncid", "sh",
)

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------- 設定の読み込み

def load_config(path: Path) -> dict[str, Any]:
    """feeds.yml を読み込む。書式が壊れていれば分かりやすく落とす。"""
    if not path.exists():
        raise SystemExit(f"設定ファイルが見つかりません: {path}")
    try:
        with path.open(encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"設定ファイル {path} の書式が正しくありません:\n{exc}") from exc

    feeds = config.get("feeds")
    if not isinstance(feeds, list) or not feeds:
        raise SystemExit(f"{path} に feeds: の一覧がありません")
    config.setdefault("settings", {})
    return config


def enabled_feeds(config: dict[str, Any]) -> list[dict[str, Any]]:
    """enabled: false が付いていない情報源だけを返す。"""
    result = []
    for feed in config["feeds"]:
        if not isinstance(feed, dict):
            print(f"  [警告] feeds: の書き方が正しくない項目を飛ばします: {feed!r}")
            continue
        if feed.get("enabled", True) is False:
            continue
        if not feed.get("url") or not feed.get("name"):
            print(f"  [警告] name か url が無い項目を飛ばします: {feed!r}")
            continue
        result.append(feed)
    return result


# ---------------------------------------------------------------- 文字列の整形

def strip_html(raw: str) -> str:
    """HTML タグを消して、ふつうの文章にする。"""
    if not raw:
        return ""
    text = TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return SPACE_RE.sub(" ", text).strip()


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def normalize_url(url: str) -> str:
    """重複判定のために URL を揃える (広告パラメータや末尾スラッシュを無視する)。"""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    scheme = "https" if parts.scheme in ("http", "https", "") else parts.scheme
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in {p.lower() for p in TRACKING_PARAMS}]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def normalize_title(title: str) -> str:
    """別々のフィードから来た同じ記事をまとめるための、ゆるいタイトル比較用の文字列。"""
    text = SPACE_RE.sub("", title.lower())
    return re.sub(r"[!-/:-@\[-`{-~、。「」・…ー–—’'\"]", "", text)


def make_id(normalized_url: str) -> str:
    return hashlib.sha1(normalized_url.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------- 日付の処理

def entry_datetime(entry: Any) -> dt.datetime | None:
    """記事の日付を取り出して日本時間にする。日付が無いフィードもあるので None を返すことがある。"""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                stamp = calendar.timegm(parsed)  # フィードの日付は UTC として扱われる
                return dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc).astimezone(JST)
            except (ValueError, OverflowError, TypeError):
                continue
    return None


# ---------------------------------------------------------------- フィードの取得

def fetch_feed(url: str, timeout: int) -> tuple[Any | None, str | None]:
    """フィードを1つ取りに行く。成功したら (解析結果, None)、失敗したら (None, エラー内容)。"""
    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            },
        )
    except requests.exceptions.Timeout:
        return None, f"タイムアウト ({timeout}秒以内に応答なし)"
    except requests.exceptions.RequestException as exc:
        return None, f"接続エラー: {exc.__class__.__name__}: {exc}"

    if response.status_code != 200:
        return None, f"HTTP {response.status_code} ({response.reason})"

    parsed = feedparser.parse(response.content)
    if not parsed.entries:
        reason = "記事が0件"
        if getattr(parsed, "bozo", 0) and getattr(parsed, "bozo_exception", None):
            reason += f" / 解析エラー: {parsed.bozo_exception}"
        return None, reason
    return parsed, None


ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def matches_keywords(title: str, summary: str, keywords: list[str]) -> bool:
    """include_keywords が指定されたフィードで、関係ない記事をふるい落とす。

    "AI" のような英字だけのキーワードは、単語として現れたときだけ一致させます。
    (そうしないと "available" や "email" の中の ai にも当たってしまうため)
    日本語を含むキーワードは、そのまま文中に含まれていれば一致とみなします。
    """
    if not keywords:
        return True
    haystack = f"{title} {summary}"
    lowered = haystack.lower()
    for word in keywords:
        text = str(word)
        if not text:
            continue
        if ASCII_WORD_RE.fullmatch(text):
            pattern = rf"(?<![A-Za-z0-9]){re.escape(text)}(?![A-Za-z0-9])"
            if re.search(pattern, haystack, re.IGNORECASE):
                return True
        elif text.lower() in lowered:
            return True
    return False


# ---------------------------------------------------------------- 保存データ

def load_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"articles": [], "feeds": [], "generated_at": None}
    try:
        with path.open(encoding="utf-8") as f:
            store = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[警告] {path} を読めませんでした ({exc})。新しく作り直します。")
        return {"articles": [], "feeds": [], "generated_at": None}
    store.setdefault("articles", [])
    store.setdefault("feeds", [])
    return store


def save_store(path: Path, store: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
        f.write("\n")
    tmp.replace(path)  # 途中で失敗しても元のファイルを壊さないよう、書けてから置き換える


def sort_key(article: dict[str, Any]) -> str:
    return article.get("published") or article.get("first_seen") or ""


# ---------------------------------------------------------------- 本体

def collect(config: dict[str, Any], store: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    settings = config.get("settings") or {}
    timeout = int(settings.get("request_timeout", 20))
    summary_length = int(settings.get("summary_length", 220))
    max_per_feed = int(settings.get("max_entries_per_feed", 40))
    polite_wait = float(settings.get("wait_seconds_between_feeds", 1.0))
    ignore_older_than_days = int(settings.get("ignore_older_than_days", 30))
    oldest_allowed = now - dt.timedelta(days=ignore_older_than_days)

    articles: list[dict[str, Any]] = list(store.get("articles", []))
    known_urls = {a["url_key"] for a in articles if a.get("url_key")}
    known_titles = {a["title_key"] for a in articles if a.get("title_key")}

    feed_status: list[dict[str, Any]] = []
    targets = enabled_feeds(config)
    print(f"■ {len(targets)} 件の情報源を確認します\n")

    for index, feed in enumerate(targets, start=1):
        name = str(feed["name"])
        url = str(feed["url"])
        category = str(feed.get("category", "other"))
        keywords = feed.get("include_keywords") or []

        print(f"[{index}/{len(targets)}] {name}")
        print(f"    {url}")

        started = time.monotonic()
        try:
            parsed, error = fetch_feed(url, timeout)
        except Exception as exc:  # 想定外の失敗でも全体を止めない
            parsed, error = None, f"想定外のエラー: {exc.__class__.__name__}: {exc}"
        elapsed = time.monotonic() - started

        if error is not None:
            print(f"    → 失敗: {error}\n")
            feed_status.append({
                "name": name, "url": url, "category": category,
                "ok": False, "error": error, "new": 0,
                "checked_at": now.isoformat(),
            })
            continue

        candidates: list[tuple[str, dict[str, Any]]] = []
        skipped_keyword = 0
        skipped_old = 0

        for entry in parsed.entries:
            link = (entry.get("link") or "").strip()
            title = strip_html(entry.get("title") or "").strip()
            if not link or not title:
                continue

            raw_summary = entry.get("summary") or ""
            if not raw_summary and entry.get("content"):
                raw_summary = entry["content"][0].get("value", "")
            summary = shorten(strip_html(raw_summary), summary_length)

            if not matches_keywords(title, summary, keywords):
                skipped_keyword += 1
                continue

            published = entry_datetime(entry)
            # 初回実行で何年も前の記事まで大量に入らないよう、古すぎる記事は取り込まない。
            # (日付が無い記事は「今見つけた新しい記事」として扱う)
            if published is not None and published < oldest_allowed:
                skipped_old += 1
                continue

            url_key = normalize_url(link)
            when = (published or now).isoformat()
            candidates.append((when, {
                "id": make_id(url_key),
                "title": title,
                "url": link,
                "url_key": url_key,
                "title_key": normalize_title(title),
                "source": name,
                "category": category,
                "published": when,
                "date_estimated": published is None,
                "summary": summary,
                "first_seen": now.isoformat(),
            }))

        # 記事数の多いフィードでは、新しいものから順に max_entries_per_feed 件だけ見る
        candidates.sort(key=lambda pair: pair[0], reverse=True)

        added = 0
        for _, article in candidates:
            if added >= max_per_feed:
                break
            if article["url_key"] in known_urls:
                continue
            # 別の情報源から来た同じ記事 (タイトルがほぼ同一) も1件にまとめる
            title_key = article["title_key"]
            if len(title_key) >= 12 and title_key in known_titles:
                continue
            articles.append(article)
            known_urls.add(article["url_key"])
            if len(title_key) >= 12:
                known_titles.add(title_key)
            added += 1

        note = f"    → 成功: {len(parsed.entries)} 件中 {added} 件が新着 ({elapsed:.1f}秒)"
        if skipped_keyword:
            note += f" / キーワード条件で {skipped_keyword} 件を除外"
        if skipped_old:
            note += f" / 古い記事 {skipped_old} 件を除外"
        print(note + "\n")
        feed_status.append({
            "name": name, "url": url, "category": category,
            "ok": True, "error": None, "new": added,
            "checked_at": now.isoformat(),
        })

        if polite_wait and index < len(targets):
            time.sleep(polite_wait)  # 相手のサーバーに連続アクセスしない

    # --- 古い記事を整理する ---
    max_articles = int(settings.get("max_articles", 1500))
    max_age_days = int(settings.get("max_age_days", 180))
    limit_date = (now - dt.timedelta(days=max_age_days)).isoformat()

    articles.sort(key=sort_key, reverse=True)
    before = len(articles)
    articles = [a for a in articles if sort_key(a) >= limit_date][:max_articles]
    removed = before - len(articles)

    ok_count = sum(1 for f in feed_status if f["ok"])
    new_total = sum(f["new"] for f in feed_status)

    print("─" * 60)
    print(f"情報源: 成功 {ok_count} / 失敗 {len(feed_status) - ok_count}")
    print(f"新着記事: {new_total} 件")
    print(f"保存件数: {len(articles)} 件 (古い記事を {removed} 件整理しました)")
    for failed in [f for f in feed_status if not f["ok"]]:
        print(f"  [失敗] {failed['name']}: {failed['error']}")
    print("─" * 60)

    return {
        "generated_at": now.isoformat(),
        "new_articles": new_total,
        "feeds": feed_status,
        "articles": articles,
    }


def write_job_summary(result: dict[str, Any], saved: bool) -> None:
    """GitHub Actions の実行結果ページに要約を表示する (ローカル実行時は何もしない)。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    feeds = result.get("feeds", [])
    failed = [f for f in feeds if not f["ok"]]
    lines = [
        "## 生成AI 最新情報のあつめ結果",
        "",
        f"- 実行日時: {result.get('generated_at')} (日本時間)",
        f"- 新着記事: **{result.get('new_articles', 0)} 件**",
        f"- 保存件数: {len(result.get('articles', []))} 件",
        f"- 情報源: 成功 {len(feeds) - len(failed)} 件 / 失敗 {len(failed)} 件",
        f"- データ保存: {'あり' if saved else 'なし (全件失敗のため見送り)'}",
    ]
    if failed:
        lines += ["", "### 取得できなかった情報源", "",
                  "| 情報源 | 理由 |", "| --- | --- |"]
        for f in failed:
            reason = str(f["error"]).replace("|", "\\|")[:160]
            lines.append(f"| {f['name']} | {reason} |")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="生成AIの最新情報をRSSから集める")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="情報源の設定ファイル")
    parser.add_argument("--store", default=str(DEFAULT_STORE), help="記事を貯める JSON ファイル")
    args = parser.parse_args()

    now = dt.datetime.now(JST).replace(microsecond=0)
    print("=" * 60)
    print(f"生成AI 最新情報あつめ  {now.strftime('%Y-%m-%d %H:%M:%S')} (日本時間)")
    print("=" * 60)

    config = load_config(Path(args.config))
    store_path = Path(args.store)
    store = load_store(store_path)
    result = collect(config, store, now)

    # すべての情報源が失敗したときは、保存せずに失敗で終わる。
    # (更新日時だけ新しくなって「動いているように見える」のを防ぐため)
    if result["feeds"] and not any(f["ok"] for f in result["feeds"]):
        print("\n[エラー] すべての情報源の取得に失敗しました。")
        print("         前回のデータはそのまま残します。ネットワークか設定を確認してください。")
        write_job_summary(result, saved=False)
        return 1

    save_store(store_path, result)
    print(f"保存しました: {store_path}")
    write_job_summary(result, saved=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
