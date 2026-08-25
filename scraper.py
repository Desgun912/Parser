"""
Парсер объявлений аренды квартир с krisha.kz и olx.kz
Ищет новые объявления по заданным критериям и шлёт уведомления в Telegram
(можно нескольким получателям сразу).

Крыша парсится обычным HTTP-запросом (requests) — этого достаточно.

OLX активно блокирует простые HTTP-запросы (403 Forbidden), поэтому для
него используется Playwright — по-настоящему запускает Chrome в фоне,
что выглядит для антибот-защиты как обычный пользователь. Это не даёт
100% гарантии обхода блокировки, но заметно повышает шансы.

Про фильтры: URL-фильтры обоих сайтов не всегда применяются надёжно.
Поэтому скрипт всегда дополнительно проверяет цену и количество комнат
сам, уже после парсинга — так в Telegram попадут только реально
подходящие объявления, независимо от фильтров самого сайта.
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

KRISHA_BASE_URL = f"https://krisha.kz/arenda/kvartiry/{CITY}/"
OLX_BASE_URL = f"https://www.olx.kz/nedvizhimost/arenda-kvartiry/{CITY}/"

# Максимум страниц результатов, которые проверяем за один прогон (на каждый сайт)
MAX_PAGES = 6

# Файл, где хранятся ID уже увиденных объявлений
SEEN_FILE = "seen_ids.json"

# ---------------------- TELEGRAM ----------------------

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Несколько получателей: в секрете TELEGRAM_CHAT_IDS перечисляем chat_id
# через запятую, например: "111111111,222222222"
_raw_chat_ids = os.environ.get("TELEGRAM_CHAT_IDS", "") or os.environ.get("TELEGRAM_CHAT_ID", "")
CHAT_IDS = [c.strip() for c in _raw_chat_ids.split(",") if c.strip()]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}


def parse_price(text: str) -> int | None:
    """Ищет в тексте цену вида '150 000 ₸' или '150 000 тг.' и возвращает
    её как число. Крыша использует символ ₸, OLX пишет сокращение 'тг.'."""
    m = re.search(r"([\d\s]{4,})\s*(₸|тг\.)", text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


PRICE_MARKER_RE = re.compile(r"₸|тг\.")


_ROOM_WORDS = {
    "одно": 1, "одна": 1,
    "двух": 2, "двушк": 2,
    "трех": 3, "трёх": 3,
    "четырех": 4, "четырёх": 4,
}


def parse_rooms(text: str) -> int | None:
    """Ищет в тексте количество комнат в разных форматах написания:
    '2-комнатная', '3 ком', '1-ком', '2х комнатную', 'двухкомнатная' и т.п."""
    m = re.search(r"(\d+)\s*[-хХ]{0,2}\.?\s*комн?", text, flags=re.IGNORECASE)
    if m:
        return int(m.group(1))

    m = re.search(r"(одно|одна|двух|двушк|трех|трёх|четырех|четырёх)\s*комнат", text, flags=re.IGNORECASE)
    if m:
        return _ROOM_WORDS.get(m.group(1).lower())

    return None


def extract_by_price_anchor(soup: BeautifulSoup, href_pattern: str) -> list[dict]:
    """
    Общая логика извлечения карточек объявлений: находим ссылки по паттерну
    href_pattern, поднимаемся к ближайшему родителю с ценой и вытаскиваем
    оттуда цену/комнаты. Работает и для Крыши, и для OLX — устойчиво к смене
    конкретных CSS-классов на сайте.

    Одна и та же ссылка на объявление может встречаться на странице
    несколько раз (например, обёрткой вокруг картинки без текста и отдельно
    вокруг заголовка) — поэтому сначала собираем все варианты по href и
    берём тот, где есть непустой текст заголовка.
    """
    by_href: dict[str, dict] = {}

    for a in soup.select(f'a[href*="{href_pattern}"]'):
        href = a.get("href", "")
        title = a.get_text(strip=True)

        if href not in by_href:
            card = a
            card_text = ""
            for _ in range(6):
                card = card.find_parent("div")
                if card is None:
                    break
                card_text = card.get_text(" ", strip=True)
                if PRICE_MARKER_RE.search(card_text):
                    break

            if not card_text or not PRICE_MARKER_RE.search(card_text):
                continue

            by_href[href] = {
                "href": href,
                "title": title,
                "price": parse_price(card_text),
                "rooms": parse_rooms(card_text),
            }
        elif title and not by_href[href]["title"]:
            # нашли вариант ссылки с непустым текстом — обновляем заголовок
            by_href[href]["title"] = title

    listings = list(by_href.values())
    for item in listings:
        if not item["title"]:
            item["title"] = "Квартира"

    return listings


# ---------------------- KRISHA.KZ (requests) ----------------------

def fetch_page_requests(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"Ошибка загрузки {url}: {e}")
        return None


def build_krisha_url(page: int) -> str:
    params = ["sort_by=price-asc"]
    if page > 1:
        params.append(f"page={page}")
    return KRISHA_BASE_URL + "?" + "&".join(params)


def parse_krisha_listings(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    raw = extract_by_price_anchor(soup, "/a/show/")

    listings = []
    for item in raw:
        m = re.search(r"/a/show/(\d+)", item["href"])
        if not m:
            continue
        ad_id = m.group(1)
        full_url = item["href"] if item["href"].startswith("http") else "https://krisha.kz" + item["href"]
        listings.append({
            "source": "krisha",
            "id": f"krisha:{ad_id}",
            "title": item["title"],
            "price": item["price"],
            "rooms": item["rooms"],
            "url": full_url,
        })
    return listings


def fetch_krisha_listings() -> list[dict]:
    all_listings = []
    for page in range(1, MAX_PAGES + 1):
        html = fetch_page_requests(build_krisha_url(page))
        if not html:
            break
        listings = parse_krisha_listings(html)
        if not listings:
            break
        all_listings.extend(listings)
        time.sleep(2)
    return all_listings


# ---------------------- OLX.KZ (Playwright) ----------------------

def build_olx_url(page: int) -> str:
    if page > 1:
        return OLX_BASE_URL + f"?page={page}"
    return OLX_BASE_URL


def parse_olx_listings(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    raw = extract_by_price_anchor(soup, "/d/obyavlenie/")

    listings = []
    for item in raw:
        href = item["href"]
        m = re.search(r"ID([a-zA-Z0-9]+)\.html", href)
        ad_id = m.group(1) if m else href

        full_url = href if href.startswith("http") else "https://www.olx.kz" + href
        listings.append({
            "source": "olx",
            "id": f"olx:{ad_id}",
            "title": item["title"],
            "price": item["price"],
            "rooms": item["rooms"],
            "url": full_url,
        })
    return listings


def fetch_olx_listings() -> list[dict]:
    """Парсит OLX через настоящий браузер (Playwright), чтобы обойти
    антибот-защиту, которая блокирует обычные HTTP-запросы."""
    all_listings = []

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("OLX: библиотека playwright не установлена — пропускаем OLX")
        return all_listings

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="ru-RU",
            )
            page_obj = context.new_page()

            for page_num in range(1, MAX_PAGES + 1):
                url = build_olx_url(page_num)
                try:
                    page_obj.goto(url, timeout=30000, wait_until="domcontentloaded")
                    # даём странице время дорисоваться / пройти проверку антибота
                    page_obj.wait_for_timeout(4000)
                    html = page_obj.content()
                except Exception as e:
                    print(f"OLX: ошибка загрузки страницы {page_num}: {e}")
                    break

                listings = parse_olx_listings(html)
                if not listings:
                    if page_num == 1:
                        print("OLX: на первой странице ничего не найдено — возможно, всё ещё заблокировано")
                        print(f"OLX: заголовок страницы: {page_obj.title()!r}")
                        try:
                            page_obj.screenshot(path="olx_debug.png", full_page=True)
                            with open("olx_debug.html", "w", encoding="utf-8") as f:
                                f.write(html)
                            print("OLX: сохранён отладочный скриншот olx_debug.png и olx_debug.html")
                        except Exception as e:
                            print(f"OLX: не удалось сохранить отладочные файлы: {e}")
                    break
                all_listings.extend(listings)
                time.sleep(2)

            browser.close()
    except Exception as e:
        print(f"OLX: не удалось запустить браузер: {e}")

    return all_listings


SUSPICIOUS_SELLER_RE = re.compile(
    r"(ватсап|whatsapp|wattsap|vatsap)\s*[\d\s\-]{5,}",
    re.IGNORECASE,
)

MAX_SELLER_CHECKS = 15  # ограничение на кол-во доп. проверок за один прогон


def flag_suspicious_olx_items(new_olx_items: list[dict]) -> None:
    """
    Для новых объявлений с OLX заходит на страницу самого объявления и
    проверяет имя продавца на признак мошенничества — когда вместо имени
    указано что-то вроде 'Ватсап775 159 5147'. Настоящие частные
    объявления обычно имеют нормальное имя.

    Не блокирует объявление, а помечает его полем 'suspicious': True —
    решение всё равно принимает пользователь, автоматический фильтр не
    может быть точным на 100%.

    Ограничено MAX_SELLER_CHECKS, чтобы не раздувать время прогона, если
    новых объявлений окажется много — остальные просто не проверяются
    и остаются непомеченными.
    """
    if not new_olx_items:
        return

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return

    to_check = new_olx_items[:MAX_SELLER_CHECKS]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="ru-RU",
            )
            page_obj = context.new_page()

            for item in to_check:
                try:
                    page_obj.goto(item["url"], timeout=20000, wait_until="domcontentloaded")
                    page_obj.wait_for_timeout(2000)
                    body_text = page_obj.inner_text("body")
                except Exception as e:
                    print(f"OLX: не удалось проверить продавца для {item['url']}: {e}")
                    continue

                if SUSPICIOUS_SELLER_RE.search(body_text):
                    item["suspicious"] = True

            browser.close()
    except Exception as e:
        print(f"OLX: не удалось запустить браузер для проверки продавцов: {e}")


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
    if not BOT_TOKEN or not CHAT_IDS:
        print("Нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_IDS — уведомление не отправлено:")
        print(text)
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for chat_id in CHAT_IDS:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": False,
        }
        try:
            resp = requests.post(url, data=payload, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"Ошибка отправки в Telegram (chat_id={chat_id}): {e}")


def format_message(item: dict) -> str:
    price_str = f"{item['price']:,} ₸".replace(",", " ") if item["price"] else "?"
    rooms_str = f"{item['rooms']}-комнатная" if item["rooms"] else item["title"]
    source_label = "Крыша" if item["source"] == "krisha" else "OLX"
    warning = "\n⚠️ Похоже на подозрительное объявление (проверьте продавца)" if item.get("suspicious") else ""
    return (
        f"🏠 {rooms_str} [{source_label}]\n"
        f"💰 {price_str}\n"
        f"{item['url']}"
        f"{warning}"
    )


def main():
    seen = load_seen_ids()

    krisha_listings = fetch_krisha_listings()
    olx_listings = fetch_olx_listings()

    all_listings = krisha_listings + olx_listings

    matching = [item for item in all_listings if matches_criteria(item)]
    new_items = [item for item in matching if item["id"] not in seen]

    new_olx_items = [item for item in new_items if item["source"] == "olx"]
    flag_suspicious_olx_items(new_olx_items)

    print(
        f"Крыша: просмотрено {len(krisha_listings)}. "
        f"OLX: просмотрено {len(olx_listings)}. "
        f"Подходящих под критерии: {len(matching)}, новых: {len(new_items)}"
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
