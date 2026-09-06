#!/usr/bin/env python3
"""Проверки сборщика текстов. Запуск: python tests/test_tracker.py"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_content
import wb_kg_tracker as T

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixture_A-1280.json")
ok = 0


def check(name, cond):
    global ok
    assert cond, f"ПРОВАЛ: {name}"
    ok += 1
    print(f"  ok  {name}")


# --- страница статьи в том же виде, в каком её отдаёт сайт --------------------
MATERIAL = {
    "id": "A-1232", "fullId": "A-1232-ru-1",
    "categoryIds": ["019eb1ef-32cf-7794-a460-450293adcb2d"],
    "categorySlugs": ["inventory-analytics"],
    "title": "Отчёт «История остатков»",
    "contentUrl": "https://static-basket-02.wbbasket.ru/vol20/A-1232/ccaab4df.json",
    "publishedContentUrl": "", "status": "published", "type": "article",
    "slug": "history-of-stocks-report-kyrgyzstan", "language": "ru",
    "availableLanguages": ["ru"], "version": 1, "views": 0,
    "createdAt": "2025-09-05T13:56:43.835Z",
    "updatedAt": "2026-09-03T11:48:13.429319Z",
}


def fake_page(spaced: bool = False) -> str:
    """Собирает HTML так же, как это делает сайт: данные внутри <script>.

    spaced=True — тот же материал, но с пробелами после двоеточий: проверяем,
    что разбор не развалится, если WB однажды поменяет форматирование.
    """
    seps = (", ", ": ") if spaced else (",", ":")
    payload = (
        '3c:[["$","$L4e",null,{"material":'
        + json.dumps(MATERIAL, ensure_ascii=False, separators=seps)
        + ',"other":1}]]\n'
    )
    # Внутри страницы payload лежит как JSON-строка внутри JS-вызова —
    # ровно так же, как у WB, включая двойное экранирование.
    chunk = json.dumps(payload, ensure_ascii=False)
    head = (
        "<html><head><title>Отчёт «История остатков» | Wildberries</title></head><body>"
        "<p>Обновлено 03.09.2026</p><p>для продавцов из Кыргызстана</p>"
        "<script>self.__next_f.push([1," + chunk + "])</script></body></html>"
    )
    return head


print("Разбор страницы статьи:")
meta = wb_content.extract_material(fake_page())
check("нашёлся идентификатор", meta["id"] == "A-1232")
check("нашлось название", meta["title"] == "Отчёт «История остатков»")
check("нашёлся адрес текста", meta["content_url"].endswith("ccaab4df.json"))
check("нашлось точное время правки", meta["updated_at"] == "2026-09-03T11:48:13.429319Z")
check("нашёлся раздел", meta["category_slugs"] == ["inventory-analytics"])
check("нашлась версия", meta["version"] == 1)

print("\nРазбор не зависит от форматирования:")
spaced = wb_content.extract_material(fake_page(spaced=True))
check("пробелы после двоеточий не мешают", spaced["id"] == "A-1232"
      and spaced["updated_at"] == "2026-09-03T11:48:13.429319Z")

print("\nСлужебный шаблон не путается с настоящей датой:")
noisy = fake_page().replace("<body>", '<body><script>self.__next_f.push([1,"\\"updatedAt\\":\\"Обновлено {date}\\""])</script>')
check("шаблон «Обновлено {date}» не перебивает настоящее значение",
      wb_content.extract_material(noisy)["updated_at"] == "2026-09-03T11:48:13.429319Z")

print("\nПревращение содержимого в текст:")
md = wb_content.content_to_markdown(json.load(open(FIXTURE, encoding="utf-8")))
check("заголовки на месте", "## Для чего нужен отчёт" in md)
check("списки на месте", "- количеству проданного товара в штуках," in md)
check("ссылки на месте", "[отчёт](https://seller.wildberries.ru/analytics-reports/region-sale)" in md)
check("плашки подписаны", "> **Обратите внимание**" in md)
check("абзацы отделены пустой строкой", "\n\n" in md)
check("каждый абзац — своя строка",
      all(len(l) < 400 for l in md.split("\n")))

print("\nВшитая картинка не попадает в файл целиком:")
big = {"reachEditorState": {"root": {"children": [
    {"type": "paragraph", "children": [
        {"type": "image", "altText": "схема", "src": "data:image/png;base64," + "A" * 500000}]}]}}}
out = wb_content.content_to_markdown(big)
check("файл остался маленьким", len(out) < 200)
check("вместо картинки — отпечаток", "вшитая-картинка:image/png:" in out)
first = out
big["reachEditorState"]["root"]["children"][0]["children"][0]["src"] = "data:image/png;base64," + "B" * 500000
check("подмена картинки видна", wb_content.content_to_markdown(big) != first)

print("\nТаблицы:")
tbl = {"reachEditorState": {"root": {"children": [{"type": "table", "children": [
    {"type": "tablerow", "children": [
        {"type": "tablecell", "children": [{"type": "paragraph", "children": [{"type": "text", "text": "Сервис", "format": 0}]}]},
        {"type": "tablecell", "children": [{"type": "paragraph", "children": [{"type": "text", "text": "Что это | зачем", "format": 0}]}]}]},
    {"type": "tablerow", "children": [
        {"type": "tablecell", "children": [{"type": "paragraph", "children": [{"type": "text", "text": "PRO WB", "format": 1}]}]},
        {"type": "tablecell", "children": [{"type": "paragraph", "children": [{"type": "text", "text": "Центр поддержки", "format": 0}]}]}]}]}]}}}
t = wb_content.content_to_markdown(tbl)
check("есть строка-разделитель", "| --- | --- |" in t)
check("вертикальная черта в тексте экранирована", "Что это \\| зачем" in t)
check("жирный текст сохранён", "**PRO WB**" in t)

print("\nСравнение снимков:")
old = {"u1": {"updated": "18.05.2026", "updated_at": "2026-05-18T11:08:37Z", "content_url": "a.json", "title": "A"},
       "u2": {"updated": "01.06.2026", "updated_at": "2026-06-01T10:00:00Z", "content_url": "b.json", "title": "B"},
       "u3": {"updated": "02.06.2026", "updated_at": "2026-06-02T10:00:00Z", "content_url": "c.json", "title": "C"}}
new = {"u1": {"updated": "03.09.2026", "updated_at": "2026-09-03T11:48:13Z", "content_url": "a2.json", "title": "A"},
       "u2": {"updated": "01.06.2026", "updated_at": "2026-06-01T18:00:00Z", "content_url": "b2.json", "title": "B"},
       "u4": {"updated": "04.09.2026", "updated_at": "2026-09-04T10:00:00Z", "content_url": "d.json", "title": "D"}}
d = T.diff(old, new)
check("смена даты поймана", [x[0] for x in d["date_changed"]] == ["u1"])
check("правка без смены даты поймана", "u2" in [x[0] for x in d["text_changed"]])
check("новая статья поймана", [x[0] for x in d["added"]] == ["u4"])
check("исчезнувшая статья поймана", [x[0] for x in d["removed"]] == ["u3"])

print("\nОтчёт:")
rep = T.render_report(d, T.make_config("kg", "ru"), "2026-09-04 13:55")
check("в отчёте есть раздел про тихие правки", "Правки без смены даты" in rep)
check("тихая правка не задублирована в обоих разделах", rep.count("**A**") == 1)

print("\nПовторно текст не качаем:")
calls = {"n": 0}


def fake_fetch(session, url, tries=3):
    calls["n"] += 1
    return json.dumps(json.load(open(FIXTURE, encoding="utf-8")), ensure_ascii=False)


T.fetch = fake_fetch
with tempfile.TemporaryDirectory() as tmp:
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        cfg = T.make_config("kg", "ru")
        url = "https://seller.wildberries.ru/instructions/ru/kg/material/some-article"
        entry = {"title": "T", "material_id": "A-1", "updated_at": "2026-09-03T11:48:13Z",
                 "content_url": "https://cdn/x.json"}
        r1 = T.save_content(None, url, dict(entry), None, cfg)
        check("в первый раз текст скачан", r1 == "saved" and calls["n"] == 1)
        check("файл создан по ожидаемому пути",
              os.path.exists(os.path.join("content", "kg", "some-article.md")))
        r2 = T.save_content(None, url, dict(entry), entry, cfg)
        check("во второй раз загрузки не было", r2 == "skipped" and calls["n"] == 1)
        moved = dict(entry, updated_at="2026-09-05T09:00:00Z")
        r3 = T.save_content(None, url, moved, entry, cfg)
        check("после правки текст скачан заново", r3 == "saved" and calls["n"] == 2)
        body = open(os.path.join("content", "kg", "some-article.md"), encoding="utf-8").read()
        check("в файле есть шапка со ссылкой", url in body)
        check("в файле есть текст статьи", "## Для чего нужен отчёт" in body)
    finally:
        os.chdir(cwd)

print(f"\nВсе проверки пройдены: {ok}")
