#!/usr/bin/env python3
"""e-Gov法令API連携パイプライン。

各法域の現行版・直前版の本則条文をe-Gov API v2から取得し、
条文単位で新旧比較して変更・追加・削除を検出する。
検出した変更条文を辞書エントリ（laws/*.json）と突き合わせて
タグ・強度・noteを付与し、data/feed.json に書き出す。
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

API_BASE = "https://laws.e-gov.go.jp/api/2"
JST = timezone(timedelta(hours=9))

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAWS_DIR = os.path.join(ROOT_DIR, "laws")
DATA_DIR = os.path.join(ROOT_DIR, "data")
FEED_PATH = os.path.join(DATA_DIR, "feed.json")

HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
HTTP_RETRY_WAIT = 2


def log(msg):
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def fetch_json(url):
    """GETしてJSONを返す。失敗時はHTTP_RETRIES回まで再試行。"""
    last_err = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "joubun-watch/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last_err = e
            log("  [retry %d/%d] %s : %s" % (attempt, HTTP_RETRIES, url, e))
            if attempt < HTTP_RETRIES:
                time.sleep(HTTP_RETRY_WAIT)
    raise last_err


# ---------------------------------------------------------------------------
# 条文番号の正規化・比較
# ---------------------------------------------------------------------------
def parse_num(s):
    """条文番号を整数タプルに変換する。

    e-Gov形式 "24_2" も辞書形式 "24の2" も単純番号 "306" も受ける。
    アンダースコアと "の" の両方を区切りとして分解する。
    比較不能な入力は None を返す。
    """
    if s is None:
        return None
    tokens = re.split(r"_|の", str(s).strip())
    parts = []
    for t in tokens:
        t = t.strip()
        if t == "":
            continue
        if not t.isdigit():
            return None
        parts.append(int(t))
    if not parts:
        return None
    return tuple(parts)


def display_num(s):
    """条文番号を辞書表記（"の"区切り）に正規化した表示文字列を返す。"""
    key = parse_num(s)
    if key is None:
        return str(s)
    return "の".join(str(p) for p in key)


def range_tokens(article_range):
    """article_range 文字列をトークンのリストに分解する。

    返り値の各要素は以下のいずれか:
      ("wild",)              -> "*"（全条文ワイルドカード）
      ("single", key)        -> 単一条文
      ("range", lo, hi)      -> 範囲（両端含む）
    解釈不能なトークンは無視する。
    """
    tokens = []
    for raw in str(article_range).split(","):
        raw = raw.strip()
        if raw == "":
            continue
        if raw == "*":
            tokens.append(("wild",))
            continue
        if "-" in raw:
            lo_s, hi_s = raw.split("-", 1)
            lo = parse_num(lo_s)
            hi = parse_num(hi_s)
            if lo is None or hi is None:
                continue
            if lo > hi:
                lo, hi = hi, lo
            tokens.append(("range", lo, hi))
        else:
            key = parse_num(raw)
            if key is None:
                continue
            tokens.append(("single", key))
    return tokens


def entry_matches(entry, num_key):
    """辞書エントリが条文番号キーにマッチするか判定する。

    ("*" のみのワイルドカードは specific マッチとしては扱わず False を返す。
    ワイルドカードは呼び出し側で catch-all として別処理する。)
    """
    if num_key is None:
        return False
    art_num = entry.get("article_num")
    if art_num is not None:
        if parse_num(art_num) == num_key:
            return True
    art_range = entry.get("article_range")
    if art_range is not None:
        for tok in range_tokens(art_range):
            if tok[0] == "single" and tok[1] == num_key:
                return True
            if tok[0] == "range" and tok[1] <= num_key <= tok[2]:
                return True
    return False


def find_wildcard_entry(entries):
    """entries の中から "*" ワイルドカードエントリを返す（なければ None）。"""
    for entry in entries:
        art_range = entry.get("article_range")
        if art_range is not None:
            for tok in range_tokens(art_range):
                if tok[0] == "wild":
                    return entry
    return None


# ---------------------------------------------------------------------------
# 条文全文の抽出
# ---------------------------------------------------------------------------
def flatten_text(node):
    """{tag, attr, children} 木、または文字列から全テキストを連結して返す。"""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(flatten_text(c) for c in node)
    if isinstance(node, dict):
        return flatten_text(node.get("children"))
    return ""


def flatten_excluding(node, exclude_tags):
    """flatten_text と同じだが、exclude_tags のタグを持つ部分木を丸ごと除外する。"""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(flatten_excluding(c, exclude_tags) for c in node)
    if isinstance(node, dict):
        if node.get("tag") in exclude_tags:
            return ""
        return flatten_excluding(node.get("children"), exclude_tags)
    return ""


def first_node_text(node, tag):
    """node 配下で最初に見つかった指定タグのテキストを返す（なければ空文字）。"""
    for found in iter_nodes(node, tag):
        return re.sub(r"\s+", "", flatten_text(found))
    return ""


def iter_nodes(node, tag):
    """木を再帰的に辿り、指定タグのノードを列挙する。"""
    if isinstance(node, dict):
        if node.get("tag") == tag:
            yield node
        yield from iter_nodes(node.get("children"), tag)
    elif isinstance(node, list):
        for c in node:
            yield from iter_nodes(c, tag)


def extract_main_articles(law_full_text):
    """law_full_text から本則（MainProvision）配下の条文を抽出する。

    条文番号（"の"区切りに正規化）をキー、以下を値とする辞書を返す。
      {
        "caption": 見出し（ArticleCaption）テキスト,
        "body":    条番号（ArticleTitle）と見出しを除いた本文テキスト,
        "full":    表示用の全文テキスト,
      }
    番号変更（引っ越し）の照合では caption と body を用い、
    条番号そのものの差異に引きずられないようにする。
    """
    articles = {}
    for main in iter_nodes(law_full_text, "MainProvision"):
        for art in iter_nodes(main, "Article"):
            attr = art.get("attr") or {}
            num = attr.get("Num")
            if num is None:
                continue
            key = display_num(num)
            full = re.sub(r"\s+", "", flatten_text(art))
            caption = first_node_text(art, "ArticleCaption")
            body = re.sub(
                r"\s+", "",
                flatten_excluding(art, ("ArticleTitle", "ArticleCaption")),
            )
            articles[key] = {"caption": caption, "body": body, "full": full}
    return articles


# ---------------------------------------------------------------------------
# リビジョン特定
# ---------------------------------------------------------------------------
def months_ago(dt, n):
    """dt から n ヶ月前の日付を返す。存在しない日（月末調整）はその月の末日に丸める。"""
    month = dt.month - n
    year = dt.year
    while month <= 0:
        month += 12
        year -= 1
    day = dt.day
    while True:
        try:
            return dt.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1


def get_enforced_revisions(law_id, today_str):
    """施行日が today_str 以前のリビジョンを施行日昇順に並べて返す。

    各要素は (date_s, rev_id) のタプル。該当なしなら空リスト。
    """
    data = fetch_json("%s/law_revisions/%s" % (API_BASE, law_id))
    revisions = data.get("revisions") or []
    enforced = []
    for rev in revisions:
        date_s = rev.get("amendment_enforcement_date")
        rev_id = rev.get("law_revision_id")
        if not date_s or not rev_id:
            continue
        if date_s <= today_str:
            enforced.append((date_s, rev_id))
    enforced.sort(key=lambda x: x[0])
    return enforced


def build_diff_pairs(enforced, window_start_str):
    """施行日昇順の enforced リストから diff 対象ペアを決める。

    返り値は (old_rev, new_rev, enforcement_date) のリスト。
    old_rev / new_rev はいずれも (date_s, rev_id)。

    - window_start_str 以降に施行された各リビジョンについて、その直前の
      リビジョンとの 1 ペアを作る（3ヶ月以内に複数回改正があれば複数ペア）。
    - 3ヶ月以内に該当リビジョンが 1 つも無い場合は、下限措置として
      現行版 vs 直前版の 1 ペアだけを返す。
    - 直前版が存在しない（初版のみ）リビジョンはペアを作れないため除外する。
    """
    if not enforced:
        return []

    pairs = []
    in_window = [
        i for i, (date_s, _rev_id) in enumerate(enforced)
        if date_s >= window_start_str
    ]

    if in_window:
        for i in in_window:
            if i == 0:
                # 直前版が存在しない初版はスキップ
                continue
            old_rev = enforced[i - 1]
            new_rev = enforced[i]
            pairs.append((old_rev, new_rev, new_rev[0]))
        return pairs

    # 3ヶ月以内に該当なし → 現行版 vs 直前版の下限措置
    if len(enforced) >= 2:
        current = enforced[-1]
        previous = enforced[-2]
        pairs.append((previous, current, current[0]))
    return pairs


# ---------------------------------------------------------------------------
# 差分検出
# ---------------------------------------------------------------------------
def diff_articles(old_map, new_map):
    """新旧の条文辞書を比較し、変更条文リストを返す。

    各要素:
      {
        "article":      新（または現存）条文番号,
        "old_article":  旧条文番号（renumbered系のみ、それ以外は None）,
        "change_type":  "changed" | "renumbered" | "renumbered_and_changed"
                        | "added" | "removed",
        "old_text": ..., "new_text": ...,
      }

    番号変更（引っ越し）検知:
      条文の同一性を「見出し（ArticleCaption）」で判定する。番号を据え置いた
      内容改正と、番号を付け替えただけの引っ越しを区別するため、まず見出し
      完全一致で新旧を対応付け、対応した番号が異なれば引っ越しとみなす。
      これにより、条番号が1つずつ繰り下がる「シフト型再編」（旧5→新7 等、
      番号が両側に存在してしまうケース）も捕捉できる。

      対応付け手順:
        Pass1: 見出しが非空かつ完全一致する新旧を対応付ける。
        Pass2: Pass1で余った条文を、同一番号どうしで対応付ける
               （見出し空・見出しごと改称された同番号条文の受け皿）。
        残った新条文 -> "added" / 残った旧条文 -> "removed"。

      対応した番号が一致 -> 内容差があれば "changed"。
      対応した番号が相違 -> 本文一致で "renumbered"、
                            本文差ありで "renumbered_and_changed"。
      各引っ越しには old_orphaned（旧番号が新版に存在しない=辞書が旧番号で
      残っている可能性）を付し、タグ継承の安全判定に用いる。
    """
    changes = []
    consumed_old = set()
    consumed_new = set()

    def numeric_sort_key(k):
        return parse_num(k) or (10 ** 9,)

    new_keys_sorted = sorted(new_map, key=numeric_sort_key)

    # Pass1: 見出し完全一致（非空）で対応付け
    old_by_caption = {}
    for k in sorted(old_map, key=numeric_sort_key):
        cap = old_map[k]["caption"]
        if cap:
            old_by_caption.setdefault(cap, []).append(k)

    matched_pairs = []
    for new_key in new_keys_sorted:
        cap = new_map[new_key]["caption"]
        if not cap:
            continue
        candidates = old_by_caption.get(cap)
        if not candidates:
            continue
        old_key = candidates.pop(0)
        consumed_old.add(old_key)
        consumed_new.add(new_key)
        matched_pairs.append((old_key, new_key))

    # Pass2: 余りを同一番号どうしで対応付け
    for new_key in new_keys_sorted:
        if new_key in consumed_new:
            continue
        if new_key in old_map and new_key not in consumed_old:
            consumed_old.add(new_key)
            consumed_new.add(new_key)
            matched_pairs.append((new_key, new_key))

    # 対応ペアを分類
    for old_key, new_key in matched_pairs:
        old_a = old_map[old_key]
        new_a = new_map[new_key]
        if old_key == new_key:
            if old_a["full"] != new_a["full"]:
                changes.append({
                    "article": new_key,
                    "old_article": None,
                    "change_type": "changed",
                    "old_orphaned": False,
                    "old_text": old_a["full"],
                    "new_text": new_a["full"],
                })
            continue
        same_body = old_a["body"] == new_a["body"]
        changes.append({
            "article": new_key,
            "old_article": old_key,
            "change_type": "renumbered" if same_body else "renumbered_and_changed",
            "old_orphaned": old_key not in new_map,
            "old_text": old_a["full"],
            "new_text": new_a["full"],
        })

    # 対応しなかった新条文 -> 追加、旧条文 -> 削除
    for new_key in new_keys_sorted:
        if new_key in consumed_new:
            continue
        changes.append({
            "article": new_key,
            "old_article": None,
            "change_type": "added",
            "old_orphaned": False,
            "old_text": "",
            "new_text": new_map[new_key]["full"],
        })
    for old_key in sorted(old_map, key=numeric_sort_key):
        if old_key in consumed_old:
            continue
        changes.append({
            "article": old_key,
            "old_article": None,
            "change_type": "removed",
            "old_orphaned": False,
            "old_text": old_map[old_key]["full"],
            "new_text": "",
        })

    return changes


# ---------------------------------------------------------------------------
# 辞書突き合わせ
# ---------------------------------------------------------------------------
def info_from_entry(entry):
    """辞書エントリから feedエントリ用の分類情報を組み立てる。"""
    return {
        "heading": entry.get("heading", ""),
        "tier": entry.get("tier", 3),
        "severity": entry.get("severity", ""),
        "tags": entry.get("tags", {"industry": [], "stance": []}),
        "note": entry.get("note", ""),
    }


def match_specific_entry(entries, num_key):
    """entries から num_key に具体マッチ（ワイルドカード以外）するエントリを返す。"""
    if num_key is None:
        return None
    for entry in entries:
        if entry_matches(entry, num_key):
            return entry
    return None


def classify(change, law_meta):
    """変更条文を辞書と突き合わせ、feedエントリの分類情報を返す。

    (info_dict, category) を返す。
    category は "tagged" | "inherited" | "unclassified"。

    番号変更（renumbered系）で新番号が辞書未登録の場合、見出し一致した
    旧番号が辞書に登録されていれば、そのタグ・強度・note を継承する。
    """
    entries = law_meta["entries"]
    num_key = parse_num(change["article"])

    # 1) 新番号での具体マッチ
    entry = match_specific_entry(entries, num_key)
    if entry is not None:
        return info_from_entry(entry), "tagged"

    # 2) renumbered系なら旧番号でタグ継承を試みる
    #    ただし辞書は新番号基準で更新され得るため、旧番号が新版に残っている
    #    （old_orphaned=False）場合は、その辞書エントリは新版の同番号条文を
    #    指しており、引っ越し先に流用すると誤タグになる。旧番号が新版から
    #    消えている場合（辞書が旧番号のまま取り残されている場合）のみ継承する。
    change_type = change.get("change_type", "")
    if change_type in ("renumbered", "renumbered_and_changed") and change.get("old_orphaned"):
        old_key = parse_num(change.get("old_article"))
        old_entry = match_specific_entry(entries, old_key)
        if old_entry is not None:
            info = info_from_entry(old_entry)
            moved = "旧第%s条から移動" % display_num(change.get("old_article"))
            info["note"] = (info["note"] + "。" + moved) if info["note"] else moved
            return info, "inherited"

    # 3) ワイルドカード（catch-all）→ 未分類扱い
    wildcard = find_wildcard_entry(entries)
    if wildcard is not None:
        return {
            "heading": wildcard.get("heading", ""),
            "tier": wildcard.get("tier", 3),
            "severity": wildcard.get("severity", ""),
            "tags": wildcard.get("tags", {"industry": [], "stance": []}),
            "note": wildcard.get("note", "未分類"),
        }, "unclassified"

    return {
        "heading": "",
        "tier": 3,
        "severity": "",
        "tags": {"industry": [], "stance": []},
        "note": "未分類",
    }, "unclassified"


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def load_law_files():
    laws = []
    for fname in sorted(os.listdir(LAWS_DIR)):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(LAWS_DIR, fname)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        laws.append(data)
    return laws


def get_articles(rev_id, cache):
    """リビジョンの本則条文辞書を取得する（cache でHTTP重複を避ける）。"""
    if rev_id in cache:
        return cache[rev_id]
    data = fetch_json("%s/law_data/%s" % (API_BASE, rev_id))
    articles = extract_main_articles(data.get("law_full_text"))
    cache[rev_id] = articles
    return articles


def process_law(law_meta, today_str, window_start_str):
    """1法域を処理し、feedエントリのリストと集計を返す。"""
    law_id = law_meta["law_id"]
    law_title = law_meta.get("law_title", "")
    log("[%s] %s" % (law_id, law_title))

    enforced = get_enforced_revisions(law_id, today_str)
    if not enforced:
        log("  現行版が見つかりません。スキップ。")
        return [], {"tagged": 0, "inherited": 0, "unclassified": 0}

    pairs = build_diff_pairs(enforced, window_start_str)
    if not pairs:
        log("  比較対象ペアがありません（初版のみ等）。スキップ。")
        return [], {"tagged": 0, "inherited": 0, "unclassified": 0}
    log("  比較ペア数 %d（基準日 %s 以降の改正を優先）" % (len(pairs), window_start_str))

    articles_cache = {}
    feed_entries = []
    counts = {"tagged": 0, "inherited": 0, "unclassified": 0}

    for old_rev, new_rev, enforcement_date in pairs:
        old_date, old_id = old_rev
        new_date, new_id = new_rev
        log("  diff: 旧 %s (%s) → 新 %s (%s) 施行日 %s"
            % (old_id, old_date, new_id, new_date, enforcement_date))

        old_map = get_articles(old_id, articles_cache)
        new_map = get_articles(new_id, articles_cache)
        changes = diff_articles(old_map, new_map)
        log("    変更検出 %d 条" % len(changes))

        for change in changes:
            info, category = classify(change, law_meta)
            counts[category] += 1
            feed_entries.append({
                "law_id": law_id,
                "law_title": law_title,
                "article_num": change["article"],
                "old_article_num": change.get("old_article"),
                "change_type": change["change_type"],
                "enforcement_date": enforcement_date,
                "heading": info["heading"],
                "old_text": change["old_text"],
                "new_text": change["new_text"],
                "tier": info["tier"],
                "severity": info["severity"],
                "tags": info["tags"],
                "note": info["note"],
            })
    return feed_entries, counts


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    now = datetime.now(JST)
    today_str = now.strftime("%Y-%m-%d")
    window_start_str = months_ago(now, 3).strftime("%Y-%m-%d")

    laws = load_law_files()
    log("対象法域 %d 件 / 基準日 %s / 3ヶ月ウィンドウ開始 %s"
        % (len(laws), today_str, window_start_str))

    all_changes = []
    total = {"tagged": 0, "inherited": 0, "unclassified": 0}
    for law_meta in laws:
        try:
            entries, counts = process_law(law_meta, today_str, window_start_str)
        except Exception as e:
            log("  [ERROR] %s をスキップ: %s" % (law_meta.get("law_id"), e))
            continue
        all_changes.extend(entries)
        total["tagged"] += counts["tagged"]
        total["inherited"] += counts["inherited"]
        total["unclassified"] += counts["unclassified"]

    feed = {
        "generated_at": now.isoformat(),
        "changes": all_changes,
    }
    with open(FEED_PATH, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)

    type_counts = {}
    for c in all_changes:
        t = c.get("change_type", "")
        type_counts[t] = type_counts.get(t, 0) + 1

    log("")
    log("=== 完了 ===")
    log("変更条文 総数    : %d" % len(all_changes))
    log("辞書タグ付け     : %d" % total["tagged"])
    log("タグ継承(引っ越し): %d" % total["inherited"])
    log("未分類          : %d" % total["unclassified"])
    log("change_type内訳  : %s"
        % ", ".join("%s=%d" % (k, type_counts[k]) for k in sorted(type_counts)))
    log("出力: %s" % FEED_PATH)


if __name__ == "__main__":
    main()
