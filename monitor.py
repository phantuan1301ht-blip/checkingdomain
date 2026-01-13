import os, json, time, sys
import requests
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

DOMAINS_FILE = os.getenv("DOMAINS_FILE", "domains.txt")
STATE_FILE   = os.getenv("STATE_FILE", "state.json")

MODE = os.getenv("MODE", "check").strip().lower()  # "check" or "report"
CHECK_PATH = os.getenv("CHECK_PATH", "/")
TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "12000"))
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "3"))  # fail N lần liên tiếp mới tính down
MAX_REPORT_ITEMS = int(os.getenv("MAX_REPORT_ITEMS", "80"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Keywords to detect "soft down" even when HTTP 200
FAIL_KEYWORDS = [
    # Shopify
    "sorry, this shop is currently unavailable",
    "this store is unavailable",
    "shop is unavailable",
    "domain not configured",
    "enter using password",
    "password",
    # Wix / generic
    "site not found",
    "this domain is parked",
    "bad gateway",
    "service unavailable",
    "gateway time-out",
    "error 502",
    "error 503",
    "error 504",
]

def now_iso():
    # Local time on runner is UTC; message label can be adjusted if you want
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

def normalize_url(line: str) -> str:
    s = line.strip()
    if not s:
        return ""
    # allow comments
    if s.startswith("#"):
        return ""
    if not s.startswith("http://") and not s.startswith("https://"):
        s = "https://" + s
    u = urlparse(s)
    if not u.netloc:
        return ""
    return f"{u.scheme}://{u.netloc}{CHECK_PATH}"

def read_domains():
    if not os.path.exists(DOMAINS_FILE):
        raise FileNotFoundError(f"Missing {DOMAINS_FILE}. Current dir={os.getcwd()}")

    out = []
    # utf-8-sig handles BOM (Windows)
    with open(DOMAINS_FILE, "r", encoding="utf-8-sig") as f:
        for line in f:
            url = normalize_url(line)
            if url:
                out.append(url)

    if not out:
        raise ValueError(f"{DOMAINS_FILE} is empty or has no valid domains.")

    # de-duplicate, keep order
    seen = set()
    uniq = []
    for x in out:
        if x not in seen:
            uniq.append(x)
            seen.add(x)
    return uniq

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram env missing; skip send.")
        return
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(api, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }, timeout=25)
    r.raise_for_status()

def looks_down(html: str):
    if not html:
        return "EMPTY_HTML"
    s = html.lower()
    for kw in FAIL_KEYWORDS:
        if kw in s:
            return f"KEYWORD:{kw}"
    return None

def check_all(domains, state):
    """
    state[url] = {
      fail_count, last_status, last_reason, last_checked, last_ok, final_url
    }
    """
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # new_context() == incognito (no persisted cookies/storage)
        context = browser.new_context(ignore_https_errors=True)

        for url in domains:
            page = context.new_page()
            status = None
            final_url = url
            reason = None
            ok = False

            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                final_url = page.url
                status = resp.status if resp else None

                html = page.content()
                soft = looks_down(html)

                # Hard fail by HTTP status
                if status is None:
                    reason = "NO_RESPONSE"
                elif status >= 500:
                    reason = f"HTTP_{status}"
                elif status == 404:
                    reason = "HTTP_404"
                elif soft:
                    reason = soft

                ok = (reason is None)

            except PwTimeout:
                reason = "TIMEOUT"
            except Exception as e:
                reason = f"ERROR:{type(e).__name__}:{e}"
            finally:
                try:
                    page.close()
                except Exception:
                    pass

            prev = state.get(url, {})
            fail_count = int(prev.get("fail_count", 0))

            if ok:
                fail_count = 0
                state[url] = {
                    "fail_count": 0,
                    "last_status": status,
                    "last_reason": None,
                    "last_checked": now_iso(),
                    "last_ok": now_iso(),
                    "final_url": final_url,
                }
            else:
                fail_count += 1
                state[url] = {
                    "fail_count": fail_count,
                    "last_status": status,
                    "last_reason": reason,
                    "last_checked": now_iso(),
                    "last_ok": prev.get("last_ok"),
                    "final_url": final_url,
                }

            results.append((url, ok, status, reason, fail_count, final_url))

        try:
            context.close()
            browser.close()
        except Exception:
            pass

    return results, state

def report_and_reset(state):
    total = len(state.keys())
    down_list = []
    up_count = 0

    for url, st in state.items():
        fail_count = int(st.get("fail_count", 0))
        if fail_count >= FAIL_THRESHOLD:
            down_list.append((url, st))
        else:
            up_count += 1

    msg = []
    msg.append(f"🧾 Night Monitor Summary (UTC): {now_iso()}")
    msg.append(f"✅ UP: {up_count} | ❌ DOWN: {len(down_list)} | Total: {total}")
    msg.append(f"Rule: DOWN if fail_count ≥ {FAIL_THRESHOLD}")

    if down_list:
        msg.append("\n❌ DOWN LIST:")
        down_list = sorted(down_list, key=lambda x: int(x[1].get("fail_count", 0)), reverse=True)
        for (url, st) in down_list[:MAX_REPORT_ITEMS]:
            msg.append(
                f"- {url} | fail={st.get('fail_count')} | status={st.get('last_status')} | {st.get('last_reason')} | last_ok={st.get('last_ok')}"
            )
        if len(down_list) > MAX_REPORT_ITEMS:
            msg.append(f"... and {len(down_list)-MAX_REPORT_ITEMS} more")

    text = "\n".join(msg)
    print(text)

    # Always send summary at 08:00 VN (01:00 UTC)
    send_telegram(text)

    # reset state for next night window
    return {}

def main():
    if MODE == "check":
        domains = read_domains()
        state = load_state()
        results, state = check_all(domains, state)
        save_state(state)

        downs = [r for r in results if not r[1]]
        print(f"[CHECK] {now_iso()} | checked={len(results)} | down_now={len(downs)} | state_saved")

    elif MODE == "report":
        state = load_state()
        new_state = report_and_reset(state)
        save_state(new_state)
        print("[REPORT] sent + state reset")

    else:
        raise ValueError("MODE must be check or report")

if __name__ == "__main__":
    main()
