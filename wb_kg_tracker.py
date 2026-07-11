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

DEFAULT_BASE = "https://seller.wildberries.ru"
# У некоторых стран справка расположена на собственном домене.
# Напр. у Грузии — seller.wildberries.ge, а не .ru.
COUNTRY_BASE = {
    "ge": "https://seller.wildberries.ge",
}
HISTORY_DAYS = 90  # глубина истории в HISTORY.md

# «Обновлено 18.05.2026» / «Обновлена 1.6.2026»
DATE_RE = re.compile(r"Обновлен[оа]\s+(\d{1,2}\.\d{1,2}\.\d{4})")

# Страны без маркера «Эта статья — для продавцов из ...»:
# все статьи их раздела считаем местными по факту нахождения в /{country}/.
NO_MARKER_COUNTRIES = {"tj"}

# Слово-метка «Обновлено» по языкам интерфейса. Для нерусских языков,
# если метка не сработала, применяется запасной поиск даты (см. extract_date).
UPDATED_LABELS = {
    "ru": r"Обновлен[оа]",
    "ka": r"განახლდა",  # грузинский «обновлено» — ПРОВЕРИТЬ на первом прогоне
}
DATE_TOKEN = re.compile(r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b")

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
    base = COUNTRY_BASE.get(country, DEFAULT_BASE)
    section_prefix = f"/instructions/{lang}/{country}/"
    marker_tail = COUNTRY_MARKERS.get(country)

    # У некоторых стран нет маркера «для продавцов из ...» (напр. Таджикистан),
    # и на нерусских языках русский маркер тоже не сработает.
    # В таких случаях считаем все статьи раздела «местными» по факту нахождения в /{country}/.
    no_marker = (country in NO_MARKER_COUNTRIES) or (lang != "ru")
    marker = None if no_marker else (f"для продавцов {marker_tail}" if marker_tail else None)

    # Отдельная папка/имя для нерусских версий, чтобы не смешивать с русской.
    folder = country if lang == "ru" else f"{country}-{lang}"
    out_dir = os.path.join("data", folder)
    name = COUNTRY_NAMES.get(country, country.upper())
    if lang != "ru":
        name = f"{name} ({lang})"

    return {
        "country": country,
        "lang": lang,
        "name": name,
        "flag": COUNTRY_FLAGS.get(country, ""),
        "base": base,
        "section_prefix": section_prefix,
        "start_url": f"{base}{section_prefix}categories",
        "marker": marker,
        "local_all": no_marker,  # True -> все статьи раздела считаем «местными»
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
            if urlparse(target).netloc != urlparse(cfg["base"]).netloc:
                continue
            k = kind(urlparse(target).path, cfg["section_prefix"])
            if k == "material":
                materials.add(target)
            elif k in ("category", "subcategory", "index") and target not in visited:
                frontier.append(target)

        time.sleep(delay)

    return sorted(materials)


def extract_date(text: str, lang: str) -> str | None:
    """Ищет дату «Обновлено DD.MM.YYYY» по метке нужного языка.
    Для нерусских языков, если метка не найдена, берёт первую дату на странице (best-effort)."""
    label = UPDATED_LABELS.get(lang)
    if label:
        m = re.search(label + r"[\s:]*?(\d{1,2}\.\d{1,2}\.\d{4})", text)
        if m:
            return m.group(1)
    if lang != "ru":  # запасной вариант только для нерусских версий
        m = DATE_TOKEN.search(text)
        if m:
            return m.group(1)
    return None


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

    updated_raw = extract_date(text, cfg["lang"])
    updated_iso = None
    if updated_raw:
        try:
            d, mo, y = (int(x) for x in updated_raw.split("."))
            updated_iso = f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            pass

    # статья для этой страны? либо по маркеру, либо (для стран/языков без маркера)
    # по факту нахождения в разделе /{country}/ — тогда local_all=True.
    if cfg["local_all"]:
        country_specific = True
    else:
        country_specific = bool(cfg["marker"]) and (cfg["marker"] in text)

    return {
        "title": title,
        "updated": updated_raw,
        "updated_iso": updated_iso,
        "local": country_specific,
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


HISTORY_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>История изменений справки WB</title>
<style>
  :root{
    --bg:#14142a; --surface:#1c1c38; --surface-2:#23234a; --border:#2c2c52;
    --text:#e9e9f3; --muted:#8a8ab0; --accent:#4361ee; --accent-soft:#2a3a8f;
    --coral:#ff6464; --blue:#9db0ff; --radius:12px;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Arial,sans-serif;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);
    font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased}
  a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
  .wrap{max-width:920px;margin:0 auto;padding:28px 20px 64px}
  h1{font-size:22px;font-weight:700;letter-spacing:-.01em;margin:0}
  .sub{color:var(--muted);font-size:13px;margin-top:4px}
  .tabs{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0 6px}
  .tab{background:var(--surface);border:1px solid var(--border);border-radius:999px;
    padding:8px 14px;font-size:14px;cursor:pointer;color:var(--muted);white-space:nowrap}
  .tab.on{background:var(--accent-soft);border-color:var(--accent);color:#fff}
  .tab .cnt{opacity:.7;font-size:12px;margin-left:4px}
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0}
  .chip{background:var(--surface);border:1px solid var(--border);border-radius:999px;
    padding:6px 12px;font-size:13px;cursor:pointer;color:var(--muted)}
  .chip.on{background:var(--surface-2);border-color:var(--accent);color:#fff}
  .meta{color:var(--muted);font-size:13px;margin:6px 0 16px}
  .day{margin:22px 0 6px;font-size:15px;font-weight:700;
    padding-bottom:6px;border-bottom:1px solid var(--border)}
  .item{padding:10px 0;border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:baseline}
  .tag{font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;white-space:nowrap}
  .tag.date_changed{background:rgba(255,100,100,.15);color:var(--coral)}
  .tag.added{background:rgba(67,97,238,.18);color:var(--blue)}
  .tag.removed{background:rgba(138,138,176,.15);color:var(--muted)}
  .title{font-weight:500}
  .change{color:var(--muted);font-size:13px;margin-left:auto;white-space:nowrap;font-variant-numeric:tabular-nums}
  .empty{color:var(--muted);padding:40px 0;text-align:center}
</style>
</head>
<body>
<div class="wrap">
  <h1>История изменений справки WB</h1>
  <div class="sub">за последние __DAYS__ дней · сформировано __TS__ (Бишкек)</div>
  <div class="tabs" id="tabs"></div>
  <div class="chips" id="chips">
    <span class="chip on" data-f="all">Все</span>
    <span class="chip" data-f="date_changed">Смена даты</span>
    <span class="chip" data-f="added">Новые</span>
    <span class="chip" data-f="removed">Исчезли</span>
  </div>
  <div id="feed"></div>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
  const DATA = JSON.parse(document.getElementById("data").textContent);
  const LABELS = {date_changed:"смена даты", added:"новая", removed:"исчезла"};
  let active = DATA.length ? DATA[0].folder : null;
  let filter = "all";
  const esc = s => (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

  function renderTabs(){
    document.getElementById("tabs").innerHTML = DATA.map(c=>{
      const on = c.folder===active ? " on" : "";
      const flag = c.flag ? c.flag+" " : "";
      return `<span class="tab${on}" data-c="${esc(c.folder)}">${flag}${esc(c.name)}<span class="cnt">${c.changes.length}</span></span>`;
    }).join("");
    document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{active=t.dataset.c;render();});
  }
  function render(){
    renderTabs();
    document.querySelectorAll(".chip").forEach(ch=>ch.classList.toggle("on",ch.dataset.f===filter));
    const c = DATA.find(x=>x.folder===active);
    const feed = document.getElementById("feed");
    if(!c){ feed.innerHTML = '<div class="empty">Нет данных.</div>'; return; }
    let recs = c.changes.slice();
    if(filter!=="all") recs = recs.filter(r=>r.type===filter);
    if(!recs.length){
      feed.innerHTML = `<div class="meta">${c.last_run?('Последний прогон: '+esc(c.last_run)+' · статей: '+c.articles):''}</div>`
        + '<div class="empty">За выбранный период изменений нет.</div>';
      return;
    }
    const byRun = {};
    recs.forEach(r=>{ (byRun[r.run]=byRun[r.run]||[]).push(r); });
    const runs = Object.keys(byRun).sort().reverse();
    let html = c.last_run ? `<div class="meta">Последний прогон: ${esc(c.last_run)} · статей: ${c.articles}</div>` : "";
    for(const run of runs){
      html += `<div class="day">${esc(run)}</div>`;
      for(const r of byRun[run]){
        const change = r.type==="date_changed" ? `${esc(r.old)} → ${esc(r.new)}`
                     : r.type==="added" ? `Обновлено ${esc(r.new)}` : "исчезла";
        const link = r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)||"(без названия)"}</a>` : esc(r.title);
        html += `<div class="item"><span class="tag ${r.type}">${LABELS[r.type]||""}</span>`
              + `<span class="title">${link}</span><span class="change">${change}</span></div>`;
      }
    }
    feed.innerHTML = html;
  }
  document.querySelectorAll(".chip").forEach(ch=>ch.onclick=()=>{filter=ch.dataset.f;render();});
  render();
</script>
</body>
</html>
"""


def write_history_html() -> None:
    """Собирает один самодостаточный history.html с вшитыми данными по всем странам
    за последние HISTORY_DAYS дней. Открывается локально двойным кликом или через Pages."""
    cutoff = (dt.date.today() - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    countries = []
    if os.path.isdir("data"):
        for folder in sorted(os.listdir("data")):
            d = os.path.join("data", folder)
            if not os.path.isdir(d):
                continue
            meta = {}
            lr = os.path.join(d, "last_run.json")
            if os.path.exists(lr):
                with open(lr, encoding="utf-8") as f:
                    meta = json.load(f)
            changes = []
            hp = os.path.join(d, "changes_history.json")
            if os.path.exists(hp):
                with open(hp, encoding="utf-8") as f:
                    changes = [r for r in json.load(f) if (r.get("run") or "") >= cutoff]
            changes.sort(key=lambda r: r.get("run", ""), reverse=True)
            countries.append({
                "folder": folder,
                "name": meta.get("name", folder),
                "flag": meta.get("flag", ""),
                "last_run": meta.get("run_local"),
                "articles": meta.get("articles"),
                "changes": changes,
            })

    now = run_timestamps()
    payload = json.dumps(countries, ensure_ascii=False).replace("</", "<\\/")
    html = (HISTORY_HTML_TEMPLATE
            .replace("__DAYS__", str(HISTORY_DAYS))
            .replace("__TS__", now["local"])
            .replace("__DATA__", payload))
    with open("history.html", "w", encoding="utf-8") as f:
        f.write(html)


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
    if cfg["marker"] is None and not cfg["local_all"]:
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
        write_history_html()
        return 0
    if not args.country:
        ap.error("укажите --country <код> или --status")
    return run_country(args.country.lower(), args.lang.lower(), args.delay, args.notify)


if __name__ == "__main__":
    raise SystemExit(main())
