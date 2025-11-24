#!/usr/bin/env python3
# main.py — архіватор новин з pravda.com.ua RSS
# Працює без зовнішніх бібліотек, крім requests.

import os
import sqlite3
import requests
import datetime
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import html
from typing import List, Dict

# -------------------------
# Налаштування
# -------------------------
RSS_URL = "https://www.pravda.com.ua/rss/"
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
NEWS_DIR = os.path.join(BASE_DIR, "news")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
TEMPLATE_FILE = os.path.join(TEMPLATES_DIR, "base_template.html")
DB_FILE = os.path.join(BASE_DIR, "db.sqlite3")


# -------------------------
# Утиліти для роботи з файлами та БД
# -------------------------
def ensure_dirs():
    os.makedirs(NEWS_DIR, exist_ok=True)
    os.makedirs(TEMPLATES_DIR, exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, "styles"), exist_ok=True)


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
                CREATE TABLE IF NOT EXISTS news
                (
                    id
                    INTEGER
                    PRIMARY
                    KEY
                    AUTOINCREMENT,
                    link
                    TEXT
                    UNIQUE,
                    title
                    TEXT,
                    category
                    TEXT,
                    pubdate_iso
                    TEXT,
                    pubdate_raw
                    TEXT,
                    saved_at
                    TEXT
                )
                """)
    conn.commit()
    conn.close()


# -------------------------
# Отримання RSS
# -------------------------
def fetch_rss(url: str) -> str:
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.text


# -------------------------
# Парсинг RSS (гнучко — без залежності від namespace)
# -------------------------
def _tag_endswith(tag: str, name: str) -> bool:
    return tag.lower().endswith(name.lower())


def get_child_text(item: ET.Element, name: str):
    for child in item:
        if _tag_endswith(child.tag, name):
            return child.text or ""
    return ""


def parse_rss(xml_text: str) -> List[Dict]:
    root = ET.fromstring(xml_text)
    # знайдемо всі item незалежно від namespace
    items = []
    for item in root.iter():
        if _tag_endswith(item.tag, "item"):
            title = get_child_text(item, "title").strip()
            link = get_child_text(item, "link").strip()
            category = get_child_text(item, "category").strip()
            pubdate_raw = get_child_text(item, "pubdate").strip()

            # спроба розпарсити pubDate у datetime (RFC-2822)
            pubdate_iso = None
            pubdate_dt = None
            if pubdate_raw:
                try:
                    pubdate_dt = parsedate_to_datetime(pubdate_raw)
                    # збережемо ISO-формат з часовою зоною, та окремо YYYY-MM-DD
                    pubdate_iso = pubdate_dt.isoformat()
                except Exception:
                    pubdate_iso = None

            items.append({
                "title": html.unescape(title),
                "link": link,
                "category": category,
                "pubdate_raw": pubdate_raw,
                "pubdate_iso": pubdate_iso,
                "pubdate_dt": pubdate_dt
            })
    return items


# -------------------------
# Фільтрація (опціонально). За умовчанням — беремо ВСІ пункти.
# Можна викликати з date_filter=datetime.date(...) або зі списком категорій.
# -------------------------
def filter_news(items: List[Dict], date_filter: datetime.date = None, categories: List[str] = None) -> List[Dict]:
    out = []
    for it in items:
        ok = True
        if date_filter and it.get("pubdate_dt"):
            if it["pubdate_dt"].date() != date_filter:
                ok = False
        if categories and it.get("category"):
            if it["category"] not in categories:
                ok = False
        if ok:
            out.append(it)
    return out


# -------------------------
# Збереження нових записів у sqlite (пункт 3)
# Повертає список доданих записів.
# -------------------------
def save_new_to_db(items: List[Dict]) -> List[Dict]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    added = []
    for it in items:
        try:
            now = datetime.datetime.now().isoformat(timespec='seconds')
            cur.execute("""
                        INSERT INTO news (link, title, category, pubdate_iso, pubdate_raw, saved_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """, (it["link"], it["title"], it["category"], it["pubdate_iso"], it["pubdate_raw"], now))
            conn.commit()
            added.append(it)
        except sqlite3.IntegrityError:
            # такий лінк уже є — вважати як існуючий
            continue
    conn.close()
    return added


# -------------------------
# Формування HTML (пункт 4)
# Вибір джерела даних для формування файлу:
#   - можна формувати з 'items' (поточний RSS)
#   - або формувати з БД (наприклад, всі збережені за певну дату)
# Тут реалізовано формування з БД за конкретну дату (безпечніше: тільки ті, що збережені)
# -------------------------
def query_db_for_date(date_obj: datetime.date) -> List[Dict]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    start = date_obj.isoformat()  # YYYY-MM-DD
    # У БД ми зберігаємо pubdate_iso (повна ISO) — отримаємо рядок і відфільтруємо WHERE pubdate_iso LIKE 'YYYY-MM-DD%'
    cur.execute(
        "SELECT title, link, category, pubdate_iso, pubdate_raw FROM news WHERE pubdate_iso LIKE ? ORDER BY pubdate_iso DESC",
        (start + '%',))
    rows = cur.fetchall()
    conn.close()
    res = []
    for r in rows:
        res.append({
            "title": r[0],
            "link": r[1],
            "category": r[2],
            "pubdate_iso": r[3],
            "pubdate_raw": r[4]
        })
    return res


def load_template() -> str:
    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        return f.read()


def render_news_html(date_obj: datetime.date, items: List[Dict]) -> str:
    tpl = load_template()
    news_blocks = []
    for it in items:
        # формат дати для відображення
        pub = it.get("pubdate_iso") or it.get("pubdate_raw") or ""
        # якщо pubdate_iso є, зручно відформатувати коротко:
        if it.get("pubdate_iso"):
            try:
                dt = datetime.datetime.fromisoformat(it["pubdate_iso"])
                pub = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

        title_esc = html.escape(it.get("title", ""))
        link_esc = html.escape(it.get("link", ""))
        cat = html.escape(it.get("category", ""))

        block = f"""
        <article class="news-item">
            <h2><a href="{link_esc}" target="_blank" rel="noopener noreferrer">{title_esc}</a></h2>
            <p class="news-meta">{cat} | {pub}</p>
        </article>
        """
        news_blocks.append(block)

    body = "\n".join(news_blocks) if news_blocks else "<p>Нічого не знайдено.</p>"
    rendered = tpl.replace("{{date}}", date_obj.isoformat())
    rendered = rendered.replace("{{news_list}}", body)
    rendered = rendered.replace("{{generated_at}}", datetime.datetime.now().isoformat(sep=' ', timespec='seconds'))
    return rendered


def write_news_file(date_obj: datetime.date, html_text: str) -> str:
    filename = f"news_{date_obj.strftime('%Y%m%d')}.html"
    path = os.path.join(NEWS_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_text)
    return path


# -------------------------
# Головна логіка
# -------------------------
def main():
    print("=== News archiver ===")
    ensure_dirs()
    init_db()

    # Перевірка наявності шаблону
    if not os.path.exists(TEMPLATE_FILE):
        print(f"Помилка: шаблон {TEMPLATE_FILE} не знайдено. Помістіть templates/base_template.html")
        return

    try:
        xml = fetch_rss(RSS_URL)
    except Exception as e:
        print("Помилка при отриманні RSS:", e)
        return

    items = parse_rss(xml)
    print(f"Знайдено елементів у RSS: {len(items)}")

    # Фільтрація: беремо новини за сьогодні
    today = datetime.datetime.now().date()
    todays = filter_news(items, date_filter=today)
    print(f"Новин за сьогодні у RSS: {len(todays)}")

    # Зберегти нові у БД
    added = save_new_to_db(todays)
    print(f"Додано нових записів у БД: {len(added)}")

    # Для формування HTML беремо записи з БД за сьогодні
    db_items = query_db_for_date(today)
    print(f"Записів у БД за сьогодні: {len(db_items)}")

    html_text = render_news_html(today, db_items)
    out_path = write_news_file(today, html_text)
    print("Файл збережено:", out_path)
    print("Готово.")


if __name__ == "__main__":
    main()
