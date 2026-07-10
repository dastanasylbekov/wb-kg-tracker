#!/usr/bin/env python3
"""
Трекер изменений справочного центра Wildberries для продавцов из Кыргызстана.

Что делает:
  1. Обходит раздел /instructions/ru/kg/ (categories -> category -> subcategory -> material),
     собирает все статьи (material).
  2. По каждой статье вытаскивает: заголовок, дату «Обновлено DD.MM.YYYY»,
     признак «Эта статья — для продавцов из Кыргызстана».
  3. Складывает всё в отсортированный snapshot.json.
  4. Сравнивает с предыдущим snapshot.json и пишет отчёт об изменениях (CHANGES.md):
     какие статьи новые, у каких сменилась дата, какие исчезли.
  5. Опционально шлёт сводку в Telegram (--notify).

Зависимости: requests, beautifulsoup4, lxml
    pip install requests beautifulsoup4 lxml

Запуск:
    python wb_kg_tracker.py                 # обойти, обновить snapshot, показать изменения
    python wb_kg_tracker.py --notify        # + отправить сводку в Telegram (env: TG_TOKEN, TG_CHAT)
    python wb_kg_tracker.py --delay 0.7     # пауза между запросами (вежливость), сек

Сайт отдаётся сервером уже отрендеренным (SSR), поэтому headless-браузер не нужен.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urldefrag, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

BASE = "https://seller.wildberries.ru"
START_URL = f"{BASE}/instructions/ru/kg/categories"
SECTION_PREFIX = "/instructions/ru/kg/"  # обходим только этот регион

SNAPSHOT_FILE = "snapshot.json"
CHANGES_FILE = "CHANGES.md"            # сводка только за последний прогон (перезаписывается)
HISTORY_FILE = "changes_history.json" # накопительная лента изменений (машинная)
HISTORY_MD_FILE = "HISTORY.md"        # та же лента, человекочитаемая, за последние N дней
HISTORY_DAYS = 90                     # глубина истории в HISTORY.md

# «Обновлено 18.05.2026» / «Обновлена 1.6.2026»
DATE_RE = re.compile(r"Обновлен[оа]\s+(\d{1,2}\.\d{1,2}\.\d{4})")
KG_MARKER = "для продавцов из Кыргызстана"

HEADERS = {
    # Честный User-Agent с контактом — хороший тон для краулера.
    "User-Agent": "WB-KG-HelpCenter-Tracker/1.0 (personal monitoring; contact: you@example.com)",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def canonical(url: str) -> str:
    """Убираем query и fragment, приводим к виду https://.../material/{slug}."""
    url, _ = urldefrag(url)
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def kind(path: str) -> str | None:
    """category / subcategory / material — или None, если не наша ссылка."""
    if not path.startswith(SECTION_PREFIX):
        return None
    for k in ("category", "subcategory", "material", "categories"):
        if f"{SECTION_PREFIX}{k}" in path or path.endswith("/categories"):
            if "/material/" in path:
                return "material"
            if "/subcategory/" in path:
                return "subcategory"
            if "/category/" in path:
                return "category"
            if path.endswith("/categories"):
                return "index"
    return None


def fetch(session: requests.Session, url: str, tries: int = 3) -> str | None:
    for attempt in range(1, tries + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.text
            print(f"  [warn] {r.status_code} на {url}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  [warn] попытка {attempt}: {e}", file=sys.stderr)
        time.sleep(1.5 * attempt)
    print(f"  [error] не удалось загрузить {url}", file=sys.stderr)
    return None


def discover_materials(session: requests.Session, delay: float) -> list[str]:
    """BFS-обход графа ссылок начиная с /categories. Возвращает список URL статей."""
    frontier = [canonical(START_URL)]
    visited: set[str] = set()
    materials: set[str] = set()

    while frontier:
        url = frontier.pop()
        if url in visited:
            continue
        visited.add(url)

        html = fetch(session, url)
        if html is None:
            continue

        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            target = canonical(urljoin(url, a["href"]))
            if urlparse(target).netloc != urlparse(BASE).netloc:
                continue
            k = kind(urlparse(target).path)
            if k == "material":
                materials.add(target)
            elif k in ("category", "subcategory", "index") and target not in visited:
                frontier.append(target)

        time.sleep(delay)

    return sorted(materials)


def parse_material(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.replace(" | Wildberries", "").strip()
    if not title:
        h1 = soup.find(["h1", "h2"])
        if h1:
            title = h1.get_text(strip=True)

    text = soup.get_text(" ", strip=True)

    m = DATE_RE.search(text)
    updated_raw = m.group(1) if m else None
    updated_iso = None
    if updated_raw:
        try:
            d, mo, y = (int(x) for x in updated_raw.split("."))
            updated_iso = f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            pass

    return {
        "title": title,
        "updated": updated_raw,          # как на сайте: 18.05.2026
        "updated_iso": updated_iso,      # для сортировки/сравнения: 2026-05-18
        "kg": KG_MARKER in text,         # статья именно для Кыргызстана?
    }


def build_snapshot(session: requests.Session, delay: float) -> dict:
    materials = discover_materials(session, delay)
    print(f"Найдено статей: {len(materials)}", file=sys.stderr)

    today = dt.date.today().isoformat()
    snapshot: dict[str, dict] = {}
    for i, url in enumerate(materials, 1):
        html = fetch(session, url)
        if html is None:
            continue
        data = parse_material(html)
        data["checked"] = today
        snapshot[url] = data
        if i % 25 == 0:
            print(f"  ...обработано {i}/{len(materials)}", file=sys.stderr)
        time.sleep(delay)

    # отсортированный по ключу dict -> аккуратные построчные git-диффы
    return dict(sorted(snapshot.items()))


def load_old() -> dict:
    if os.path.exists(SNAPSHOT_FILE):
        with open(SNAPSHOT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def diff(old: dict, new: dict) -> dict:
    added, removed, date_changed, flag_changed = [], [], [], []

    for url, n in new.items():
        o = old.get(url)
        if o is None:
            added.append((url, n))
        else:
            if o.get("updated") != n.get("updated"):
                date_changed.append((url, o.get("updated"), n.get("updated"), n))
            if o.get("kg") != n.get("kg"):
                flag_changed.append((url, o.get("kg"), n.get("kg"), n))

    for url in old:
        if url not in new:
            removed.append((url, old[url]))

    return {
        "added": added,
        "removed": removed,
        "date_changed": date_changed,
        "flag_changed": flag_changed,
    }


def render_report(d: dict) -> str:
    today = dt.date.today().isoformat()
    lines = [f"# Изменения справочного центра WB (KG) — {today}", ""]

    if not any(d.values()):
        lines.append("Изменений с прошлого запуска нет.")
        return "\n".join(lines) + "\n"

    if d["date_changed"]:
        lines.append(f"## Сменилась дата «Обновлено» ({len(d['date_changed'])})")
        for url, old_dt, new_dt, n in d["date_changed"]:
            flag = " 🇰🇬" if n.get("kg") else ""
            lines.append(f"- **{n['title']}**{flag}: {old_dt} → {new_dt}")
            lines.append(f"  {url}")
        lines.append("")

    if d["added"]:
        lines.append(f"## Новые статьи ({len(d['added'])})")
        for url, n in d["added"]:
            flag = " 🇰🇬" if n.get("kg") else ""
            lines.append(f"- **{n['title']}**{flag} (Обновлено {n.get('updated')})")
            lines.append(f"  {url}")
        lines.append("")

    if d["removed"]:
        lines.append(f"## Исчезли статьи ({len(d['removed'])})")
        for url, o in d["removed"]:
            lines.append(f"- ~~{o.get('title')}~~  {url}")
        lines.append("")

    if d["flag_changed"]:
        lines.append(f"## Сменился признак «для Кыргызстана» ({len(d['flag_changed'])})")
        for url, old_f, new_f, n in d["flag_changed"]:
            lines.append(f"- **{n['title']}**: {old_f} → {new_f}  {url}")
        lines.append("")

    return "\n".join(lines) + "\n"


def append_history(d: dict) -> None:
    """Дописывает изменения текущего прогона в changes_history.json (для админки)."""
    run = dt.date.today().isoformat()
    records = []
    for url, n in d["added"]:
        records.append({"run": run, "type": "added", "url": url,
                        "title": n["title"], "old": None, "new": n.get("updated"),
                        "kg": n.get("kg")})
    for url, old_dt, new_dt, n in d["date_changed"]:
        records.append({"run": run, "type": "date_changed", "url": url,
                        "title": n["title"], "old": old_dt, "new": new_dt,
                        "kg": n.get("kg")})
    for url, o in d["removed"]:
        records.append({"run": run, "type": "removed", "url": url,
                        "title": o.get("title"), "old": o.get("updated"), "new": None,
                        "kg": o.get("kg")})
    if not records:
        return
    hist = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, encoding="utf-8") as f:
            hist = json.load(f)
    hist.extend(records)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)


def write_history_md() -> None:
    """Строит HISTORY.md — ленту изменений за последние HISTORY_DAYS дней,
    сгруппированную по дате прогона (свежие сверху). Берёт данные из changes_history.json."""
    if not os.path.exists(HISTORY_FILE):
        return
    with open(HISTORY_FILE, encoding="utf-8") as f:
        hist = json.load(f)

    cutoff = (dt.date.today() - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    recent = [r for r in hist if (r.get("run") or "") >= cutoff]

    today = dt.date.today().isoformat()
    lines = [f"# История изменений справочного центра WB (KG) — последние {HISTORY_DAYS} дней",
             "", f"_Сформировано: {today}_", ""]

    if not recent:
        lines.append(f"За последние {HISTORY_DAYS} дней изменений не зафиксировано.")
        with open(HISTORY_MD_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return

    # группируем по дате прогона, свежие сверху
    runs: dict[str, list] = {}
    for r in recent:
        runs.setdefault(r.get("run"), []).append(r)

    titles = {"date_changed": "Сменилась дата «Обновлено»",
              "added": "Новые статьи", "removed": "Исчезли статьи"}
    order = ["date_changed", "added", "removed"]

    for run in sorted(runs, reverse=True):
        lines.append(f"## {run}")
        recs = runs[run]
        for typ in order:
            group = [r for r in recs if r.get("type") == typ]
            if not group:
                continue
            lines.append(f"### {titles[typ]} ({len(group)})")
            for r in group:
                flag = " 🇰🇬" if r.get("kg") else ""
                title = r.get("title") or "(без названия)"
                if typ == "date_changed":
                    lines.append(f"- **{title}**{flag}: {r.get('old')} → {r.get('new')}")
                    lines.append(f"  {r.get('url')}")
                elif typ == "added":
                    lines.append(f"- **{title}**{flag} (Обновлено {r.get('new')})")
                    lines.append(f"  {r.get('url')}")
                else:  # removed
                    lines.append(f"- ~~{title}~~  {r.get('url')}")
            lines.append("")
        lines.append("")

    with open(HISTORY_MD_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def notify_telegram(text: str) -> None:
    token, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
    if not token or not chat:
        print("[notify] TG_TOKEN/TG_CHAT не заданы — пропускаю Telegram.", file=sys.stderr)
        return
    # Telegram ограничивает сообщение ~4096 символами
    chunk = text[:3900]
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": chunk, "disable_web_page_preview": True},
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"[notify] Telegram вернул {resp.status_code}: {resp.text}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"[notify] ошибка Telegram: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="Трекер изменений справочного центра WB (Кыргызстан)")
    ap.add_argument("--delay", type=float, default=0.5, help="пауза между запросами, сек (по умолчанию 0.5)")
    ap.add_argument("--notify", action="store_true", help="отправить сводку в Telegram")
    ap.add_argument("--no-missing-date-warning", action="store_true",
                    help="не предупреждать о статьях без распознанной даты")
    args = ap.parse_args()

    session = requests.Session()

    old = load_old()
    new = build_snapshot(session, args.delay)

    if not new:
        print("[error] ничего не собрано — снимок пустой, snapshot.json НЕ перезаписан.", file=sys.stderr)
        return 1

    # Предупреждение: у скольких статей не удалось распознать дату
    missing = [u for u, v in new.items() if not v.get("updated")]
    if missing and not args.no_missing_date_warning:
        print(f"[warn] дата не распознана у {len(missing)} статей "
              f"(возможно, нужно поправить DATE_RE/селектор). Примеры:", file=sys.stderr)
        for u in missing[:5]:
            print(f"        {u}", file=sys.stderr)

    d = diff(old, new) if old else {"added": [(u, v) for u, v in new.items()],
                                     "removed": [], "date_changed": [], "flag_changed": []}
    report = render_report(d)

    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, indent=2)
    with open(CHANGES_FILE, "w", encoding="utf-8") as f:
        f.write(report)

    # Историю копим только начиная со второго прогона (на первом «новые» = все статьи).
    if old:
        append_history(d)

    # HISTORY.md перестраиваем всегда (если есть накопленная история).
    write_history_md()

    print(report)

    if args.notify and old and any(v for k, v in d.items()):
        notify_telegram(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
