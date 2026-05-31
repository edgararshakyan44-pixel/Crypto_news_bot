import json, time, logging, hashlib, re, requests, os
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup
from dotenv import load_dotenv
load_dotenv()

OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHANNELS = [
    "Coin_Post",
    "DeCenter",
    "GetblockMagazine",
    "cointelegraph",
    "Pro_Blockchain",
    "okcrypta1",
    "forklog",
    "criptopatolog",
    "crypton_off",
    "cryptodaily",
    "cryptobosh",
    "WatcherGuru",
    "coinpost",
]

MAX_POSTS_PER_PAGE  = 20   # max posts visible on t.me/s/ page (hardware limit)
CAPTION_LIMIT       = 900
STATE_FILE          = Path(__file__).parent / "bot_state.json"
LOG_FILE            = Path(__file__).parent / "bot.log"
PROMPT_FILE         = Path(__file__).parent / "prompt_instructions.txt"
OPENAI_MODEL        = "gpt-4o"

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ============================================================
# TRANSLATION SYSTEM PROMPT — loaded from prompt_instructions.txt
# ============================================================

def load_translation_prompt():
    try:
        text = PROMPT_FILE.read_text(encoding="utf-8").strip()
        log.info(f"Loaded prompt from {PROMPT_FILE.name} ({len(text)} chars)")
        return text
    except Exception as e:
        log.warning(f"Could not read {PROMPT_FILE}: {e} — using fallback prompt")
        return (
            "You are a translator for an Armenian crypto Telegram channel. "
            "Translate posts into natural Armenian script only. "
            "Keep tickers/brand names in English. "
            "At the end add: Աղբյուր։ @SourceChannel"
        )

# ============================================================
# STATE
# ============================================================

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_msg_ids": {}, "last_run": None}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# ============================================================
# TELEGRAM CHANNEL SCRAPING  (t.me/s/ChannelName)
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

def fetch_channel(channel_name):
    url = f"https://t.me/s/{channel_name}"
    try:
        r = requests.get(url, timeout=20, headers=HEADERS)
        log.info(f"  HTTP {r.status_code}, size={len(r.text)} chars")
        if r.status_code != 200 or len(r.text) < 500:
            log.warning(f"  [{channel_name}] bad response")
            return []

        soup  = BeautifulSoup(r.text, "html.parser")
        items = []

        for wrap in soup.find_all("div", class_="tgme_widget_message_wrap"):
            # text
            text_el = wrap.find("div", class_="tgme_widget_message_text")
            if not text_el:
                continue
            text = text_el.get_text(separator="\n", strip=True)
            if len(text) < 20:
                continue

            # unique id from message link
            link_el  = wrap.find("a", class_="tgme_widget_message_date")
            msg_link = (
                link_el["href"]
                if link_el and link_el.get("href")
                else f"{channel_name}/{text[:40]}"
            )

            # image URL — scan ALL elements for background-image
            image_url = None
            bg_pat1 = re.compile(r"background-image:url\('([^']+)'\)")
            bg_pat2 = re.compile(r'background-image:url\("([^"]+)"\)')
            for el in wrap.find_all(True):
                style = el.get("style", "")
                if "background-image" in style:
                    m = bg_pat1.search(style) or bg_pat2.search(style)
                    if m:
                        image_url = m.group(1)
                        break
            # fallback: <img src>
            if not image_url:
                img_tag = wrap.find("img")
                if img_tag:
                    src = img_tag.get("src", "")
                    if src.startswith("http") and ("cdn" in src or "photo" in src):
                        image_url = src

            if image_url:
                log.info(f"  Image detected: {image_url[:80]}")

            # extract numeric message id from URL e.g. https://t.me/Chan/1234
            msg_id = 0
            m_id = re.search(r"/(\d+)$", msg_link)
            if m_id:
                msg_id = int(m_id.group(1))

            items.append({
                "id":        hashlib.md5(msg_link.encode()).hexdigest(),
                "msg_id":    msg_id,
                "title":     text[:100],
                "content":   text,
                "source":    channel_name,
                "image_url": image_url,
            })

        log.info(f"  parsed {len(items)} messages total")
        return items

    except Exception as e:
        log.warning(f"Fetch failed [{channel_name}]: {e}")
        return []

def get_all_news(last_msg_ids):
    all_items = []
    for name in CHANNELS:
        log.info(f"Fetching {name} ...")
        items = fetch_channel(name)
        last_id = last_msg_ids.get(name, 0)
        if last_id == 0:
            # first time seeing this channel — only take the very latest post to initialize
            new = [items[-1]] if items else []
        else:
            new = [i for i in items if i["msg_id"] > last_id]
        if new:
            log.info(f"  \u2713 {len(new)} new item(s) (after msg_id={last_id})")
            all_items.extend(new)
        else:
            log.info(f"  \u2014 no new items (last msg_id={last_id})")
        time.sleep(1)
    return all_items

# ============================================================
# TRANSLATION
# ============================================================

def translate_post(item):
    system_prompt = load_translation_prompt()
    user_msg = (
        f"Translate this crypto news post into Armenian. "
        f"Source channel: @{item['source']}\n\n"
        f"{item['content']}"
    )
    for attempt in range(3):
        try:
            r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": OPENAI_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_msg},
                    ],
                    "max_tokens": 1200,
                    "temperature": 0.3,
                },
                timeout=30,
            )
            data = r.json()
            if "choices" in data:
                return data["choices"][0]["message"]["content"].strip()
            else:
                log.warning(f"OpenAI error response: {data}")
                break
        except Exception as e:
            log.warning(f"Translation attempt {attempt+1} failed: {e}")
            time.sleep(4)
    return None

# ============================================================
# IMAGE DOWNLOAD
# ============================================================

def download_image(url):
    try:
        r = requests.get(url, timeout=30, headers=HEADERS)
        if r.status_code == 200 and len(r.content) > 1000:
            return r.content
        log.warning(f"Image download bad response: status={r.status_code}, size={len(r.content)}")
    except Exception as e:
        log.warning(f"Image download failed: {e}")
    return None

# ============================================================
# TELEGRAM SENDING
# ============================================================

def send_text(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=30,
        )
        ok = r.json().get("ok", False)
        if not ok:
            log.warning(f"sendMessage failed: {r.text[:200]}")
        return ok
    except Exception as e:
        log.error(f"sendMessage error: {e}")
        return False

def send_photo_with_caption(image_bytes, caption):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
            files={"photo": ("image.jpg", image_bytes, "image/jpeg")},
            timeout=60,
        )
        ok = r.json().get("ok", False)
        if not ok:
            log.warning(f"sendPhoto failed: {r.text[:200]}")
        return ok
    except Exception as e:
        log.error(f"sendPhoto error: {e}")
        return False

def send_photo_only(image_bytes):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHAT_ID},
            files={"photo": ("image.jpg", image_bytes, "image/jpeg")},
            timeout=60,
        )
        ok = r.json().get("ok", False)
        if not ok:
            log.warning(f"sendPhoto (no caption) failed: {r.text[:200]}")
        return ok
    except Exception as e:
        log.error(f"sendPhoto error: {e}")
        return False

# ============================================================
# POST DISPATCHER
# ============================================================

def post_item(item, translated_text):
    image_url = item.get("image_url")

    if image_url:
        log.info(f"  Downloading image: {image_url[:80]}")
        img_bytes = download_image(image_url)

        if img_bytes:
            if len(translated_text) <= CAPTION_LIMIT:
                ok = send_photo_with_caption(img_bytes, translated_text)
                log.info(f"  Photo+caption sent: {ok}")
                return ok
            else:
                ok_photo = send_photo_only(img_bytes)
                time.sleep(1)
                ok_text  = send_text(translated_text)
                log.info(f"  Photo sent: {ok_photo}, Text sent: {ok_text}")
                return ok_photo and ok_text
        else:
            log.warning("  Image download failed — sending text only")
            ok = send_text(translated_text)
            log.info(f"  Text sent: {ok}")
            return ok
    else:
        log.info("  No image in post — sending text only")
        ok = send_text(translated_text)
        log.info(f"  Text sent: {ok}")
        return ok

# ============================================================
# MAIN
# ============================================================

def main():
    log.info("=" * 50)
    log.info("Bot run started")
    state        = load_state()
    last_msg_ids = state.get("last_msg_ids", {})

    news_items = get_all_news(last_msg_ids)
    if not news_items:
        log.info("No new items. Exiting.")
        return

    log.info(f"Total new items: {len(news_items)}")

    posted = 0
    for item in news_items:
        log.info(f"Translating [{item['source']}]: {item['title'][:60]}")
        log.info(f"  msg_id={item['msg_id']} | Has image: {bool(item.get('image_url'))}")

        translated = translate_post(item)
        if not translated:
            log.warning("  Translation failed, skipping")
            continue

        ok = post_item(item, translated)
        if ok:
            posted += 1
            ch = item["source"]
            if item["msg_id"] > last_msg_ids.get(ch, 0):
                last_msg_ids[ch] = item["msg_id"]

        time.sleep(2)

    state["last_msg_ids"] = last_msg_ids
    state["last_run"]     = datetime.now(timezone.utc).isoformat()
    save_state(state)
    log.info(f"Done. Posted {posted} items.")
    log.info("=" * 50)

if __name__ == "__main__":
    main()
