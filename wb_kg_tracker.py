#!/usr/bin/env python3
"""
Трекер изменений справочного центра Wildberries — мультистрановая версия.

Отслеживает дату «Обновлено DD.MM.YYYY» у статей раздела
/instructions/{lang}/{country}/ для выбранной страны и показывает,
у каких статей изменилась дата (а значит — внесли правки) и какие статьи появились/исчезли.

Каждая страна пишется в свою папку data/{country}/, чтобы изменения по странам
не смешивались:
    data/kg/snapshot.json          — текущее состояние всех статей
    data/kg/CHANGES.md             — сводка только за последний прогон
    data/kg/HISTORY.md             — лента изменений за последние 90 дней
    data/kg/changes_history.json   — та же лента в машинном виде

Зависимости: requests, beautifulsoup4, lxml
    pip install requests beautifulsoup4 lxml

Запуск (одна страна за вызов):
    python wb_kg_tracker.py --country kg
    python wb_kg_tracker.py --country by --notify --delay 0.6

Сайт отдаётся сервером уже отрендеренным (SSR), headless-браузер не нужен.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from urllib.parse import urljoin, urldefrag, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

BASE = "https://seller.wildberries.ru"
HISTORY_DAYS = 90  # глубина истории в HISTORY.md

# «Обновлено 18.05.2026» / «Обновлена 1.6.2026»
DATE_RE = re.compile(r"Обновлен[оа]\s+(\d{1,2}\.\d{1,2}\.\d{4})")

# Как называется страна в маркере «Эта статья — для продавцов из ...»
# (родительный падеж). Добавляйте страны сюда по мере надобности.
COUNTRY_MARKERS = {
    "kg": "из Кыргызстана",
    "by": "из Беларуси",
    "kz": "из Казахстана",
    "am": "из Армении",
    "uz": "из Узбекистана",
    "ge": "из Грузии",
    "tj": "из Таджикистана",
    "ru": "из России",
}
COUNTRY_NAMES = {
    "kg": "Кыргызстан", "by": "Беларусь", "kz": "Казахстан", "am": "Армения",
    "uz": "Узбекистан", "ge": "Грузия", "tj": "Таджикистан", "ru": "Россия",
}
COUNTRY_FLAGS = {
    "kg": "🇰🇬", "by": "🇧🇾", "kz": "🇰🇿", "am": "🇦🇲",
    "uz": "🇺🇿", "ge": "🇬🇪", "tj": "🇹🇯", "ru": "🇷🇺",
}

HEADERS = {
    "User-Agent": "WB-HelpCenter-Tracker/2.0 (personal monitoring; contact: you@example.com)",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def make_config(country: str, lang: str) -> dict:
    """Собирает все страно-зависимые настройки в один объект."""
    section_prefix = f"/instructions/{lang}/{country}/"
    marker_tail = COUNTRY_MARKERS.get(country)
    out_dir = os.path.join("data", country)
    return {
        "country": country,
        "lang": lang,
        "name": COUNTRY_NAMES.get(country, country.upper()),
        "flag": COUNTRY_FLAGS.get(country, ""),
        "section_prefix": section_prefix,
        "start_url": f"{BASE}{section_prefix}categories",
        # None -> у страны не задан маркер, признак «для страны» не вычисляем
        "marker": f"для продавцов {marker_tail}" if marker_tail else None,
        "out_dir": out_dir,
        "snapshot": os.path.join(out_dir, "snapshot.json"),
        "changes": os.path.join(out_dir, "CHANGES.md"),
        "history_json": os.path.join(out_dir, "changes_history.json"),
        "history_md": os.path.join(out_dir, "HISTORY.md"),
    }


def canonical(url: str) -> str:
    """Убираем query и fragment."""
    url, _ = urldefrag(url)
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def kind(path: str, section_prefix: str) -> str | None:
    """category / subcategory / material / index — или None, если не наш раздел."""
    if not path.startswith(section_prefix):
        return None
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


def discover_materials(session: requests.Session, delay: float, cfg: dict) -> list[str]:
    """BFS-обход графа ссылок начиная с /categories выбранной страны."""
    frontier = [canonical(cfg["start_url"])]
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
            k = kind(urlparse(target).path, cfg["section_prefix"])
            if k == "material":
                materials.add(target)
            elif k in ("category", "subcategory", "index") and target not in visited:
                frontier.append(target)

        time.sleep(delay)

    return sorted(materials)


def parse_material(html: str, cfg: dict) -> dict:
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

    # для конкретной страны? (у общих статей маркера нет)
    country_specific = bool(cfg["marker"]) and (cfg["marker"] in text)

    return {
        "title": title,
        "updated": updated_raw,
        "updated_iso": updated_iso,
        "local": country_specific,  # статья именно для этой страны
    }


def build_snapshot(session: requests.Session, delay: float, cfg: dict) -> dict:
    materials = discover_materials(session, delay, cfg)
    print(f"[{cfg['country']}] найдено статей: {len(materials)}", file=sys.stderr)

    today = dt.date.today().isoformat()
    snapshot: dict[str, dict] = {}
    for i, url in enumerate(materials, 1):
        html = fetch(session, url)
        if html is None:
            continue
        data = parse_material(html, cfg)
        data["checked"] = today
        snapshot[url] = data
        if i % 25 == 0:
            print(f"  [{cfg['country']}] ...обработано {i}/{len(materials)}", file=sys.stderr)
        time.sleep(delay)

    return dict(sorted(snapshot.items()))


def load_old(cfg: dict) -> dict:
    if os.path.exists(cfg["snapshot"]):
        with open(cfg["snapshot"], encoding="utf-8") as f:
            return json.load(f)
    return {}


def diff(old: dict, new: dict) -> dict:
    added, removed, date_changed = [], [], []
    for url, n in new.items():
        o = old.get(url)
        if o is None:
            added.append((url, n))
        elif o.get("updated") != n.get("updated"):
            date_changed.append((url, o.get("updated"), n.get("updated"), n))
    for url in old:
        if url not in new:
            removed.append((url, old[url]))
    return {"added": added, "removed": removed, "date_changed": date_changed}


def run_timestamps() -> dict:
    """Метки времени прогона. Кыргызстан — UTC+6 круглый год (без перевода часов)."""
    now_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    local = now_utc + dt.timedelta(hours=6)
    return {
        "utc": now_utc.strftime("%Y-%m-%d %H:%M UTC"),
        "local": local.strftime("%Y-%m-%d %H:%M"),
        "iso_utc": now_utc.isoformat(),
    }


def render_report(d: dict, cfg: dict, run_local: str) -> str:
    today = dt.date.today().isoformat()
    flag = (cfg["flag"] + " ") if cfg["flag"] else ""
    lines = [f"# Изменения справки WB — {flag}{cfg['name']} — {today}", "",
             f"_Прогон: {run_local} (Бишкек)_", ""]

    if not any(d.values()):
        lines.append("Изменений с прошлого запуска нет.")
        return "\n".join(lines) + "\n"

    if d["date_changed"]:
        lines.append(f"## Сменилась дата «Обновлено» ({len(d['date_changed'])})")
        for url, old_dt, new_dt, n in d["date_changed"]:
            lines.append(f"- **{n['title']}**: {old_dt} → {new_dt}")
            lines.append(f"  {url}")
        lines.append("")

    if d["added"]:
        lines.append(f"## Новые статьи ({len(d['added'])})")
        for url, n in d["added"]:
            lines.append(f"- **{n['title']}** (Обновлено {n.get('updated')})")
            lines.append(f"  {url}")
        lines.append("")

    if d["removed"]:
        lines.append(f"## Исчезли статьи ({len(d['removed'])})")
        for url, o in d["removed"]:
            lines.append(f"- ~~{o.get('title')}~~  {url}")
        lines.append("")

    return "\n".join(lines) + "\n"


def append_history(d: dict, cfg: dict) -> None:
    """Дописывает изменения текущего прогона в data/{country}/changes_history.json."""
    run = dt.date.today().isoformat()
    records = []
    for url, n in d["added"]:
        records.append({"run": run, "type": "added", "url": url,
                        "title": n["title"], "old": None, "new": n.get("updated")})
    for url, old_dt, new_dt, n in d["date_changed"]:
        records.append({"run": run, "type": "date_changed", "url": url,
                        "title": n["title"], "old": old_dt, "new": new_dt})
    for url, o in d["removed"]:
        records.append({"run": run, "type": "removed", "url": url,
                        "title": o.get("title"), "old": o.get("updated"), "new": None})
    if not records:
        return
    hist = []
    if os.path.exists(cfg["history_json"]):
        with open(cfg["history_json"], encoding="utf-8") as f:
            hist = json.load(f)
    hist.extend(records)
    with open(cfg["history_json"], "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)


def write_history_md(cfg: dict) -> None:
    """Строит HISTORY.md — ленту за последние HISTORY_DAYS дней, по датам прогона (свежие сверху)."""
    if not os.path.exists(cfg["history_json"]):
        return
    with open(cfg["history_json"], encoding="utf-8") as f:
        hist = json.load(f)

    cutoff = (dt.date.today() - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    recent = [r for r in hist if (r.get("run") or "") >= cutoff]

    today = dt.date.today().isoformat()
    flag = (cfg["flag"] + " ") if cfg["flag"] else ""
    lines = [f"# История изменений справки WB — {flag}{cfg['name']} — последние {HISTORY_DAYS} дней",
             "", f"_Сформировано: {today}_", ""]

    if not recent:
        lines.append(f"За последние {HISTORY_DAYS} дней изменений не зафиксировано.")
        with open(cfg["history_md"], "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return

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
                title = r.get("title") or "(без названия)"
                if typ == "date_changed":
                    lines.append(f"- **{title}**: {r.get('old')} → {r.get('new')}")
                    lines.append(f"  {r.get('url')}")
                elif typ == "added":
                    lines.append(f"- **{title}** (Обновлено {r.get('new')})")
                    lines.append(f"  {r.get('url')}")
                else:
                    lines.append(f"- ~~{title}~~  {r.get('url')}")
            lines.append("")
        lines.append("")

    with open(cfg["history_md"], "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def write_status() -> None:
    """Строит верхнеуровневый STATUS.md — «пульс»: когда какая страна собиралась.
    Читает data/{country}/last_run.json по всем странам."""
    rows = []
    if os.path.isdir("data"):
        for c in sorted(os.listdir("data")):
            p = os.path.join("data", c, "last_run.json")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    rows.append(json.load(f))

    now = run_timestamps()
    lines = ["# Статус трекера справки WB", "",
             f"_Сформировано: {now['local']} (Бишкек, UTC+6)_", ""]

    if not rows:
        lines.append("Пока нет данных о прогонах.")
    else:
        lines.append("| Страна | Последний прогон (Бишкек) | Статей | Изменений в прогоне | Статус |")
        lines.append("|---|---|---:|---:|:---:|")
        for r in rows:
            changes = r.get("added", 0) + r.get("date_changed", 0) + r.get("removed", 0)
            flag = (r.get("flag", "") + " ") if r.get("flag") else ""
            lines.append(f"| {flag}{r.get('name')} | {r.get('run_local')} | "
                         f"{r.get('articles')} | {changes} | ✅ |")

    with open("STATUS.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def notify_telegram(text: str, cfg: dict) -> None:
    token, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
    if not token or not chat:
        print("[notify] TG_TOKEN/TG_CHAT не заданы — пропускаю Telegram.", file=sys.stderr)
        return
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


def run_country(country: str, lang: str, delay: float, notify: bool) -> int:
    cfg = make_config(country, lang)
    if cfg["marker"] is None:
        print(f"[{country}] [warn] для страны не задан маркер — признак «для страны» "
              f"считаться не будет. Добавьте её в COUNTRY_MARKERS.", file=sys.stderr)

    os.makedirs(cfg["out_dir"], exist_ok=True)
    session = requests.Session()

    old = load_old(cfg)
    new = build_snapshot(session, delay, cfg)

    if not new:
        print(f"[{country}] [error] ничего не собрано — снимок пустой, файлы НЕ перезаписаны.",
              file=sys.stderr)
        return 1

    missing = [u for u, v in new.items() if not v.get("updated")]
    if missing:
        print(f"[{country}] [warn] дата не распознана у {len(missing)} статей.", file=sys.stderr)

    d = diff(old, new) if old else {"added": [(u, v) for u, v in new.items()],
                                    "removed": [], "date_changed": []}
    ts = run_timestamps()
    report = render_report(d, cfg, ts["local"])

    with open(cfg["snapshot"], "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, indent=2)
    with open(cfg["changes"], "w", encoding="utf-8") as f:
        f.write(report)

    if old:
        append_history(d, cfg)
    write_history_md(cfg)

    # «пульс» страны — из этого собирается общий STATUS.md
    with open(os.path.join(cfg["out_dir"], "last_run.json"), "w", encoding="utf-8") as f:
        json.dump({
            "country": cfg["country"], "name": cfg["name"], "flag": cfg["flag"],
            "run_local": ts["local"], "run_utc": ts["utc"], "run_iso": ts["iso_utc"],
            "articles": len(new),
            "added": len(d["added"]), "date_changed": len(d["date_changed"]),
            "removed": len(d["removed"]),
        }, f, ensure_ascii=False, indent=2)

    print(report)

    if notify and old and any(d.values()):
        notify_telegram(report, cfg)

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Трекер изменений справочного центра WB (мультистрановой)")
    ap.add_argument("--country", help="код страны: kg, by, kz, am, uz, ge, tj, ru")
    ap.add_argument("--lang", default="ru", help="язык интерфейса (по умолчанию ru)")
    ap.add_argument("--delay", type=float, default=0.5, help="пауза между запросами, сек")
    ap.add_argument("--notify", action="store_true", help="отправить сводку в Telegram")
    ap.add_argument("--status", action="store_true",
                    help="собрать STATUS.md по всем странам (запускать после прогонов)")
    args = ap.parse_args()

    if args.status:
        write_status()
        return 0
    if not args.country:
        ap.error("укажите --country <код> или --status")
    return run_country(args.country.lower(), args.lang.lower(), args.delay, args.notify)


if __name__ == "__main__":
    raise SystemExit(main())
