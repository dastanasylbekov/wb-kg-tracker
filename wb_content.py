#!/usr/bin/env python3
"""
Сбор ТЕКСТА статей справочного центра Wildberries.

Зачем отдельный модуль
----------------------
Основной трекер (wb_kg_tracker.py) следит только за датой «Обновлено» и говорит,
ЧТО поменялось на уровне списка статей. Этот модуль достаёт сам ТЕКСТ статьи,
чтобы было видно, ЧТО ИМЕННО поменялось внутри.

Как устроен сайт (важно)
------------------------
Страница статьи НЕ содержит текста в обычной вёрстке — привычный разбор HTML
видит только меню, шапку и подвал. Текст приезжает внутри данных Next.js,
в тегах <script> вида `self.__next_f.push([1,"..."])`.

Но разбирать эту кашу не нужно. Внутри неё лежит компактное описание статьи:

    "material":{"id":"A-1232", ... ,"title":"...",
                "contentUrl":"https://static-basket-02.wbbasket.ru/vol20/A-1232/<uuid>.json",
                "updatedAt":"2026-09-03T11:48:13.429319Z", ...}

`contentUrl` — прямая ссылка на JSON с ИСХОДНЫМ содержимым статьи (формат
редактора Lexical). Это то, что редакторы WB реально написали, без вёрстки.
Оттуда и берём текст.

Плюсы такого пути:
  * не ломается при редизайне сайта (в вёрстке имена классов меняются постоянно);
  * `updatedAt` — точное время правки, а не дата без времени;
  * JSON лежит на CDN, отдаётся с Last-Modified и кэшируется;
  * контент можно НЕ качать, если `updatedAt` не изменился.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# ---------------------------------------------------------------------------
# 1. Достаём описание материала со страницы статьи
# ---------------------------------------------------------------------------

# Куски данных Next.js: self.__next_f.push([1,"...экранированная строка..."])
_FLIGHT_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')

# Границы объекта "material" внутри данных страницы.
# Пробелы вокруг двоеточий сайт сейчас не ставит, но допускаем их на случай,
# если однажды начнёт — иначе трекер молча перестанет находить статьи.
_S = r"\s*"
_MATERIAL_OBJ = re.compile(
    r'"material"' + _S + r":" + _S + r'\{' + _S + r'"id"' + _S + r":" + _S + r'"[^"]+"'
    r'.*?"updatedAt"' + _S + r":" + _S + r'"[^"]*"',
    re.S,
)


def _field(name: str, value: str = r'"((?:[^"\\]|\\.)*)"') -> str:
    """Собирает выражение вида "имя": значение, не придираясь к пробелам."""
    return f'"{name}"{_S}:{_S}{value}'


def _decode_flight(html: str) -> str:
    """Склеивает данные Next.js со страницы в одну строку.

    Каждый кусок — это JSON-строка, поэтому её надо «разэкранировать»
    (превратить \\" в ", \\u0439 в й и т.д.). Проще всего — доверить это json.
    """
    parts: list[str] = []
    for m in _FLIGHT_CHUNK.finditer(html):
        try:
            parts.append(json.loads('"' + m.group(1) + '"'))
        except ValueError:
            continue  # битый кусок — пропускаем, остальные всё равно полезны
    return "".join(parts)


def _first(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else None


def extract_material(html: str) -> dict[str, Any]:
    """Вытаскивает описание статьи со страницы.

    Возвращает словарь с ключами: id, title, slug, content_url, updated_at,
    version, category_slugs. Отсутствующие поля будут None.
    """
    flight = _decode_flight(html)

    # Ищем именно объект "material" — иначе можно случайно поймать
    # служебный шаблон "Обновлено {date}", который лежит в переводах рядом.
    m = _MATERIAL_OBJ.search(flight)
    scope = m.group(0) if m else ""

    cats_raw = _first(_field("categorySlugs", r"\[([^\]]*)\]"), scope)
    cats = re.findall(r'"([^"]+)"', cats_raw) if cats_raw else []

    version = _first(_field("version", r"(\d+)"), scope)

    return {
        "id": _first(_field("id"), scope),
        "title": _first(_field("title"), scope),
        "slug": _first(_field("slug"), scope),
        "content_url": _first(_field("contentUrl"), scope),
        "updated_at": _first(_field("updatedAt"), scope),
        "version": int(version) if version else None,
        "category_slugs": cats,
    }


# ---------------------------------------------------------------------------
# 2. Превращаем содержимое статьи в читаемый Markdown
# ---------------------------------------------------------------------------

# Битовая маска оформления текста в Lexical
_BOLD, _ITALIC, _STRIKE, _UNDERLINE, _CODE = 1, 2, 4, 8, 16

# Как подписывать блоки-плашки
_BLOCK_LABELS = {
    "alert": "Примечание",
    "alert-note": "Обратите внимание",
    "alert-important": "Важно",
    "alert-advice": "Совет",
    "faq": "Вопрос-ответ",
}


def _fmt_text(node: dict) -> str:
    """Текстовый узел -> Markdown с сохранением жирного/курсива."""
    text = node.get("text", "")
    if not text:
        return ""
    # Оформление применяем к обрезанному тексту, а пробелы возвращаем снаружи,
    # иначе Markdown вида "** жирный **" не сработает.
    lead = text[: len(text) - len(text.lstrip())]
    tail = text[len(text.rstrip()):]
    core = text.strip()
    if not core:
        return text

    fmt = node.get("format", 0) or 0
    if fmt & _CODE:
        core = f"`{core}`"
    if fmt & _BOLD:
        core = f"**{core}**"
    if fmt & _ITALIC:
        core = f"_{core}_"
    if fmt & _STRIKE:
        core = f"~~{core}~~"
    return f"{lead}{core}{tail}"


def _image_ref(node: dict) -> str:
    """Ссылка на картинку.

    Сами картинки не скачиваем — записываем адрес. Тогда подмена картинки
    видна как изменение строки, а репозиторий не растёт.

    Отдельный случай: часть картинок вшита прямо в текст статьи как
    `data:image/png;base64,...` — одна такая занимает под 700 КБ. Записывать её
    целиком нельзя: это раздует репозиторий и сделает сравнение нечитаемым.
    Поэтому от такой картинки сохраняем короткий отпечаток — он меняется
    вместе с картинкой, так что подмена всё равно будет видна.
    """
    alt = (node.get("altText") or "").strip()
    src = node.get("src") or ""
    if src.startswith("data:"):
        digest = hashlib.sha256(src.encode("utf-8")).hexdigest()[:16]
        kind = src[5 : src.find(";")] if ";" in src[:40] else "image"
        src = f"вшитая-картинка:{kind}:{digest}"
    return f"![{alt}]({src})"


def _inline(nodes: list[dict]) -> str:
    """Склеивает содержимое одной строки: текст, ссылки, картинки, переносы."""
    out: list[str] = []
    for n in nodes or []:
        t = n.get("type")
        if t == "text":
            out.append(_fmt_text(n))
        elif t == "link":
            label = _inline(n.get("children", [])).strip()
            url = n.get("url", "")
            out.append(f"[{label}]({url})" if url else label)
        elif t == "image":
            out.append(_image_ref(n))
        elif t == "linebreak":
            out.append("\n")
        elif n.get("children"):
            out.append(_inline(n["children"]))
    return "".join(out)


def _cell_text(cell: dict) -> str:
    """Содержимое ячейки таблицы — одной строкой.

    В ячейке может лежать несколько абзацев, картинки и ссылки, а в разметке
    таблицы перевод строки недопустим. Склеиваем через <br>, а вертикальную
    черту экранируем, иначе таблица развалится.
    """
    parts: list[str] = []
    for child in cell.get("children", []) or []:
        piece = _inline([child]).strip() if child.get("type") != "list" else ""
        if not piece:
            # список внутри ячейки: собираем пункты через запятую
            if child.get("type") == "list":
                items = [_inline(li.get("children", [])).strip()
                         for li in child.get("children", []) or []]
                piece = "; ".join(i for i in items if i)
        if piece:
            parts.append(piece)
    return "<br>".join(parts).replace("|", "\\|")


def _table(node: dict) -> list[str]:
    """Таблица -> таблица Markdown."""
    rows: list[list[str]] = []
    for row in node.get("children", []) or []:
        if row.get("type") != "tablerow":
            continue
        rows.append([_cell_text(c) for c in row.get("children", []) or []
                     if c.get("type") == "tablecell"])
    rows = [r for r in rows if r]
    if not rows:
        return []

    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]

    # Разметка Markdown требует строку-заголовок, поэтому первую строку
    # всегда считаем заголовком — так таблица отображается корректно.
    head, body = rows[0], rows[1:]
    lines = ["| " + " | ".join(head) + " |",
             "|" + "|".join([" --- "] * width) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return ["\n".join(lines)]


def _block(node: dict, depth: int = 0) -> list[str]:
    """Блочный узел -> список абзацев Markdown (каждый отдельным элементом)."""
    t = node.get("type")
    kids = node.get("children", []) or []

    if t == "heading":
        level = node.get("tag", "h2")
        hashes = "#" * max(2, min(6, int(level[1:]) if level[1:].isdigit() else 2))
        # В заголовке иногда лежит картинка — вынесем её отдельным абзацем ниже.
        text_kids = [k for k in kids if k.get("type") != "image"]
        img_kids = [k for k in kids if k.get("type") == "image"]
        blocks = [f"{hashes} {_inline(text_kids).strip()}"]
        blocks += [_inline([k]) for k in img_kids]
        return [b for b in blocks if b.strip()]

    if t == "paragraph":
        text = _inline(kids).strip()
        return [text] if text else []

    if t == "list":
        ordered = node.get("listType") == "number"
        lines: list[str] = []
        for i, item in enumerate(kids, start=int(node.get("start", 1) or 1)):
            marker = f"{i}." if ordered else "-"
            pad = "  " * depth
            # Вложенный список внутри пункта
            nested = [k for k in item.get("children", []) if k.get("type") == "list"]
            plain = [k for k in item.get("children", []) if k.get("type") != "list"]
            body = _inline(plain).strip()
            lines.append(f"{pad}{marker} {body}" if body else "")
            for sub in nested:
                lines.extend(_block(sub, depth + 1))
        return ["\n".join(l for l in lines if l)] if any(lines) else []

    if t == "block":
        label = _BLOCK_LABELS.get(node.get("contentType"), node.get("contentType") or "Блок")
        inner: list[str] = []
        for k in kids:
            inner.extend(_block(k, depth))
        if not inner:
            return []
        # Плашку оформляем цитатой — так она заметна и в diff, и глазами.
        body = "\n\n".join(inner)
        quoted = "\n".join(f"> {line}" if line else ">" for line in body.split("\n"))
        return [f"> **{label}**\n>\n{quoted}"]

    if t in ("collapsible-container", "collapsible-content"):
        out: list[str] = []
        for k in kids:
            out.extend(_block(k, depth))
        return out

    if t == "collapsible-title":
        text = _inline(kids).strip()
        return [f"### {text}"] if text else []

    if t == "table":
        return _table(node)

    if t in ("tablerow", "tablecell"):
        # Встречаются только внутри таблицы — отдельно не рендерим.
        return []

    if t == "quote":
        text = _inline(kids).strip()
        return ["\n".join(f"> {l}" for l in text.split("\n"))] if text else []

    if t == "image":
        return [_image_ref(node)]

    # Неизвестный тип — не теряем содержимое, разбираем детей.
    out = []
    for k in kids:
        out.extend(_block(k, depth))
    return out


def content_to_markdown(content_json: dict) -> str:
    """Содержимое статьи (JSON редактора) -> Markdown.

    Каждый абзац идёт отдельной строкой и отделяется пустой строкой.
    Это нужно для сравнения версий: если абзацы слить в одну длинную строку,
    git подсветит её целиком даже при правке одного слова.
    """
    root = (content_json or {}).get("reachEditorState", {}).get("root", {})
    blocks: list[str] = []
    for node in root.get("children", []) or []:
        blocks.extend(_block(node))
    text = "\n\n".join(b for b in blocks if b.strip())
    return text.strip() + "\n" if text.strip() else ""


def render_article(meta: dict, content_json: dict, url: str) -> str:
    """Собирает готовый файл статьи: шапка с метаданными + текст."""
    head = [
        f"# {meta.get('title') or '(без названия)'}",
        "",
        f"- Адрес: {url}",
        f"- Идентификатор: {meta.get('id')}",
        f"- Обновлено: {meta.get('updated_at')}",
    ]
    if meta.get("category_slugs"):
        head.append(f"- Разделы: {', '.join(meta['category_slugs'])}")
    head += ["", "---", ""]
    return "\n".join(head) + content_to_markdown(content_json)
