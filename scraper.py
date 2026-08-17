"""
Парсер объявлений аренды квартир с krisha.kz
Ищет новые объявления по заданным критериям и шлёт уведомления в Telegram.

Критерии задаются через переменные окружения (см. workflow-файл) или
можно поправить значения по умолчанию ниже.
"""

import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup

# ---------------------- НАСТРОЙКИ ПОИСКА ----------------------

CITY = "petropavlovsk"
PRICE_TO = 145000          # максимальная цена, тг/мес
ROOMS = [1, 2]              # список нужных вариантов комнат

BASE_URL = f"https://krisha.kz/arenda/kvartiry/{CITY}/"

# Максимум страниц результатов, которые проверяем за один прогон
MAX_PAGES = 6

# Файл, где хранятся ID уже увиденных объявлений
SEEN_FILE = "seen_ids.json"

# ---------------------- TELEGRAM ----------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def build_search_url(page: int) -> str:
    params = [f"das[price][to]={PRICE_TO}"]
    for r in ROOMS:
        params.append(f"das[live.rooms][]={r}")
    if page > 1:
        params.append(f"page={page}")
    return BASE_URL + "?" + "&".join(params)


def load_seen_ids() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            try:
                return set(json.load(f))
            except json.JSONDecodeError:
                return set()
    return set()


def save_seen_ids(ids: set) -> None:
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, ensure_ascii=False, indent=2)


def fetch_page(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"Ошибка загрузки {url}: {e}")
        return None


def parse_listings(html: str) -> list[dict]:
    """
    Извлекает объявления из HTML страницы результатов.
    Krisha рисует карточки в контейнерах с классом 'a-card'
    и атрибутом data-id, содержащим ID объявления.
    """
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    cards = soup.select("div.a-card[data-id]")

    # Фолбэк на случай изменения вёрстки: ищем все ссылки на /a/show/ID
    if not cards:
        cards = []
        seen_local = set()
        for a in soup.select('a[href*="/a/show/"]'):
            m = re.search(r"/a/show/(\d+)", a.get("href", ""))
            if not m:
                continue
            ad_id = m.group(1)
            if ad_id in seen_local:
                continue
            seen_local.add(ad_id)
            # поднимаемся к родительскому блоку карточки
            card = a.find_parent("div")
            if card:
                cards.append(card)

    for card in cards:
        ad_id = card.get("data-id")
        if not ad_id:
            link = card.select_one('a[href*="/a/show/"]')
            if not link:
                continue
            m = re.search(r"/a/show/(\d+)", link.get("href", ""))
            if not m:
                continue
            ad_id = m.group(1)

        title_el = card.select_one("a.a-card__title")
        price_el = card.select_one("div.a-card__price")
        addr_el = card.select_one("div.a-card__subtitle")
        link_el = card.select_one('a[href*="/a/show/"]')

        title = title_el.get_text(strip=True) if title_el else "Квартира"
        price = price_el.get_text(strip=True) if price_el else "?"
        address = addr_el.get_text(strip=True) if addr_el else ""
        href = link_el.get("href") if link_el else f"/a/show/{ad_id}"
        if href and not href.startswith("http"):
            href = "https://krisha.kz" + href

        listings.append({
            "id": ad_id,
            "title": title,
            "price": price,
            "address": address,
            "url": href,
        })

    return listings


def send_telegram_message(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("Нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — уведомление не отправлено:")
        print(text)
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, data=payload, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Ошибка отправки в Telegram: {e}")


def format_message(item: dict) -> str:
    return (
        f"🏠 <b>{item['title']}</b>\n"
        f"💰 {item['price']}\n"
        f"📍 {item['address']}\n"
        f"{item['url']}"
    )


def main():
    seen = load_seen_ids()
    all_listings = []

    for page in range(1, MAX_PAGES + 1):
        url = build_search_url(page)
        html = fetch_page(url)
        if not html:
            break
        listings = parse_listings(html)
        if not listings:
            break
        all_listings.extend(listings)
        time.sleep(2)  # небольшая пауза между страницами

    new_items = [item for item in all_listings if item["id"] not in seen]

    print(f"Всего найдено объявлений: {len(all_listings)}, новых: {len(new_items)}")

    for item in new_items:
        send_telegram_message(format_message(item))
        seen.add(item["id"])
        time.sleep(1)  # чтобы не спамить Telegram API

    save_seen_ids(seen)


if __name__ == "__main__":
    main()
