"""
Парсер объявлений аренды квартир с krisha.kz
Ищет новые объявления по заданным критериям и шлёт уведомления в Telegram.

Важно: фильтры в URL Крыши (das[price][to] и т.д.) не всегда применяются
надёжно при обычном GET-запросе (похоже, часть фильтрации у них работает
через JS в браузере). Поэтому скрипт всегда дополнительно проверяет
цену и количество комнат сам, уже после парсинга — это гарантирует, что
в Telegram попадут только реально подходящие объявления.
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
    # Сортировка "сначала дешёвые" — так новые дешёвые объявления
    # с большей вероятностью попадут в первые проверяемые страницы.
    params = ["sort_by=price-asc"]
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


def parse_price(text: str) -> int | None:
    """Ищет в тексте цену вида '150 000 ₸' и возвращает её как число."""
    m = re.search(r"([\d\s]{4,})\s*₸", text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def parse_rooms(text: str) -> int | None:
    """Ищет в тексте количество комнат вида '2-комнатная'."""
    m = re.search(r"(\d+)-комнатн", text)
    if m:
        return int(m.group(1))
    return None


def parse_listings(html: str) -> list[dict]:
    """
    Извлекает объявления из HTML страницы результатов.

    Подход устойчив к смене конкретных CSS-классов: сначала находим все
    ссылки на /a/show/ID, а затем для каждой ищем ближайший родительский
    блок, в тексте которого есть цена (символ ₸) — это и есть карточка
    объявления. Из текста этого блока вытаскиваем цену и число комнат.
    """
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    seen_ids = set()

    for a in soup.select('a[href*="/a/show/"]'):
        href = a.get("href", "")
        m = re.search(r"/a/show/(\d+)", href)
        if not m:
            continue
        ad_id = m.group(1)
        if ad_id in seen_ids:
            continue

        # Поднимаемся вверх по дереву, пока не найдём блок с ценой (₸)
        card = a
        card_text = ""
        for _ in range(6):
            card = card.find_parent("div")
            if card is None:
                break
            card_text = card.get_text(" ", strip=True)
            if "₸" in card_text:
                break

        if not card_text or "₸" not in card_text:
            continue  # не нашли карточку с ценой — пропускаем

        seen_ids.add(ad_id)

        price = parse_price(card_text)
        rooms = parse_rooms(card_text)

        title = a.get_text(strip=True) or "Квартира"

        full_url = href if href.startswith("http") else "https://krisha.kz" + href

        listings.append({
            "id": ad_id,
            "title": title,
            "price": price,
            "rooms": rooms,
            "url": full_url,
        })

    return listings


def matches_criteria(item: dict) -> bool:
    """Проверяет объявление по цене и комнатам. Без явной цены — пропускаем,
    чтобы случайно не прислать что-то мимо критериев."""
    if item["price"] is None:
        return False
    if item["price"] > PRICE_TO:
        return False
    if ROOMS and item["rooms"] is not None and item["rooms"] not in ROOMS:
        return False
    return True


def send_telegram_message(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("Нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — уведомление не отправлено:")
        print(text)
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, data=payload, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"Ошибка отправки в Telegram: {e}")


def format_message(item: dict) -> str:
    price_str = f"{item['price']:,} ₸".replace(",", " ") if item["price"] else "?"
    rooms_str = f"{item['rooms']}-комнатная" if item["rooms"] else item["title"]
    return (
        f"🏠 {rooms_str}\n"
        f"💰 {price_str}\n"
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

    matching = [item for item in all_listings if matches_criteria(item)]
    new_items = [item for item in matching if item["id"] not in seen]

    print(
        f"Всего просмотрено объявлений: {len(all_listings)}, "
        f"подходящих под критерии: {len(matching)}, новых: {len(new_items)}"
    )

    for item in new_items:
        send_telegram_message(format_message(item))
        time.sleep(1)  # чтобы не спамить Telegram API

    # Помечаем увиденными вообще все просмотренные объявления (не только
    # подходящие) — иначе объявление, которое сначала не подходило, а потом
    # его цену поправили под критерии, будет считаться "новым" повторно.
    for item in all_listings:
        seen.add(item["id"])

    save_seen_ids(seen)


if __name__ == "__main__":
    main()
