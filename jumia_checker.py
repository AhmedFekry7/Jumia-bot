import json
import os
import re
import time
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = str(os.environ["CHAT_ID"]).strip()
STATE_FILE = "state.json"

STARTER_URLS = [
    "https://www.jumia.com.eg/defacto-regular-fit-hooded-zip-up-ski-jacket-135305752.html"
]


def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN.strip()}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": CHAT_ID.strip(),
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        print("Telegram response:", resp.text)
    except Exception as e:
        print(f"Telegram send error: {e}")


def fetch_new_telegram_messages(last_update_id: int) -> list:
    url = f"https://api.telegram.org/bot{BOT_TOKEN.strip()}/getUpdates"
    params = {"timeout": 5}
    if last_update_id:
        params["offset"] = last_update_id + 1
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        if data.get("ok"):
            return data.get("result", [])
    except Exception as e:
        print(f"Error fetching Telegram updates: {e}")
    return []


def scrape_jumia_product(url: str) -> dict:
    clean_url = url.split("?")[0].strip()
    
    # Use Jina reader proxy to bypass Cloudflare 403 blocks on GitHub servers
    proxy_url = f"https://r.jina.ai/{clean_url}"
    headers = {"Accept": "text/plain"}
    
    response = requests.get(proxy_url, headers=headers, timeout=30)
    response.raise_for_status()
    text = response.text

    # Extract Title (usually first markdown heading or line)
    title_match = re.search(r"Title:\s*(.+)", text)
    title = title_match.group(1).strip() if title_match else "Jumia Product"

    # Extract Price (look for EGP or numbers following currency)
    price = None
    # Search for patterns like EGP 1,234.00 or similar in text
    price_matches = re.findall(r"(?:EGP|LE)\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if price_matches:
        # Clean and pick the first valid price found
        for p_str in price_matches:
            cleaned = p_str.replace(",", "").strip()
            try:
                val = float(cleaned)
                if val > 1.0:
                    price = val
                    break
            except ValueError:
                continue

    if price is None:
        # Fallback search for any large number that looks like a price
        num_matches = re.findall(r"\b\d{2,5}(?:\.\d{2})?\b", text)
        if num_matches:
            try:
                price = float(num_matches[0])
            except ValueError:
                pass

    if price is None:
        raise ValueError(f"Could not parse price from Jumia page text.")

    in_stock = True
    if "out of stock" in text.lower() or "غير متوفر" in text.lower():
        in_stock = False

    return {"title": title, "price": price, "in_stock": in_stock}


def send_product_list(products: dict):
    if not products:
        send_telegram("📭 *Your tracking list is empty.*\nSend me any `jumia.com.eg` link to start tracking!")
        return

    lines = ["📋 *Currently Tracked Jumia Products:*\n"]
    for idx, (url, info) in enumerate(products.items(), start=1):
        if info.get("price") is None:
            lines.append(f"*{idx}.* Pending initial check...\n   [View on Jumia]({url})")
        else:
            stock_icon = "✅" if info.get("in_stock", True) else "❌"
            lines.append(
                f"*{idx}. {info['title']}*\n"
                f"   💰 EGP {info['price']:,.2f} | {stock_icon}\n"
                f"   [View on Jumia]({url})"
            )
    send_telegram("\n".join(lines))


def process_chat_commands(state: dict) -> bool:
    updates = fetch_new_telegram_messages(state.get("last_update_id", 0))
    if not updates:
        return False

    state_changed = False
    products = state.setdefault("products", {})
    status_requested = False

    for upd in updates:
        state["last_update_id"] = upd["update_id"]
        state_changed = True

        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue

        sender_chat_id = str(msg.get("chat", {}).get("id", ""))
        if sender_chat_id != CHAT_ID:
            continue

        text = (msg.get("text") or "").strip()
        if not text:
            continue

        if text.lower().startswith(("/list", "/status", "/start")):
            status_requested = True
        elif text.lower().startswith("/remove"):
            parts = text.split()
            urls = list(products.keys())
            if len(parts) > 1 and parts[1].isdigit():
                idx = int(parts[1]) - 1
                if 0 <= idx < len(urls):
                    target_url = urls[idx]
                    removed_title = products[target_url].get("title") or target_url
                    del products[target_url]
                    send_telegram(f"🗑️ Removed from tracker:\n*{removed_title}*")
                else:
                    send_telegram(f"❌ Invalid number. Send `/list` to see item numbers.")
            else:
                send_telegram("Usage: `/remove 1` (send `/list` to check numbers).")

        found_urls = re.findall(r"https?://(?:www\.)?jumia\.com\.eg/[^\s]+", text)
        for raw_url in found_urls:
            clean_url = raw_url.split("?")[0].strip()
            if clean_url not in products:
                products[clean_url] = {"title": None, "price": None, "in_stock": True}

    if status_requested:
        send_product_list(products)

    return state_changed


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            if "products" not in raw:
                return {"last_update_id": 0, "products": raw}
            return raw

    initial_products = {
        u.split("?")[0]: {"title": None, "price": None, "in_stock": True}
        for u in STARTER_URLS
    }
    return {"last_update_id": 0, "products": initial_products}


def main():
    state = load_state()
    changed = process_chat_commands(state)
    products = state.get("products", {})

    for idx, (url, saved) in enumerate(list(products.items())):
        if idx > 0:
            time.sleep(2.0)

        try:
            current = scrape_jumia_product(url)
            new_price = current["price"]
            now_in_stock = current["in_stock"]

            if saved.get("price") is None:
                products[url] = current
                changed = True
                stock_badge = "✅ In Stock" if now_in_stock else "❌ Out of Stock"
                send_telegram(
                    f"✅ *Now Tracking New Jumia Item!*\n"
                    f"*{current['title']}*\n"
                    f"💰 Initial Price: *EGP {new_price:,.2f}*\n"
                    f"📦 Status: {stock_badge}\n"
                    f"[View on Jumia Egypt]({url})"
                )
                continue

            old_price = saved["price"]
            was_in_stock = saved.get("in_stock", True)
            alerts = []

            if new_price < old_price:
                diff = old_price - new_price
                pct = (diff / old_price) * 100
                alerts.append(
                    f"📉 *PRICE DROPPED by EGP {diff:,.2f} (-{pct:.1f}%)*\n"
                    f"Old: EGP {old_price:,.2f} -> *New: EGP {new_price:,.2f}*"
                )
            elif new_price > old_price:
                diff = new_price - old_price
                pct = (diff / old_price) * 100
                alerts.append(
                    f"📈 *PRICE INCREASED by EGP {diff:,.2f} (+{pct:.1f}%)*\n"
                    f"Old: EGP {old_price:,.2f} -> *New: EGP {new_price:,.2f}*"
                )

            if not was_in_stock and now_in_stock:
                alerts.append(f"✅ *BACK IN STOCK!* (EGP {new_price:,.2f})")
            elif was_in_stock and not now_in_stock:
                alerts.append("⚠️ *Item just went OUT OF STOCK.*")

            if alerts:
                send_telegram(
                    f"🔔 *Jumia Alert: {current['title']}*\n\n"
                    + "\n\n".join(alerts)
                    + f"\n\n[Open on Jumia Egypt]({url})"
                )
                products[url] = current
                changed = True

        except Exception as e:
            print(f"Error checking {url}: {e}")

    if changed:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
