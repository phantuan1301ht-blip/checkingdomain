import os, json, time, sys, asyncio
import requests
from urllib.parse import urlparse
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

DOMAINS_FILE = os.getenv("DOMAINS_FILE", "domains.txt")
STATE_FILE   = os.getenv("STATE_FILE", "state.json")

MODE = os.getenv("MODE", "check").strip().lower()  # check | report
CHECK_PATH = os.getenv("CHECK_PATH", "/")
TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "8000"))          # giảm để chạy nhanh
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "3"))
MAX_REPORT_ITEMS = int(os.getenv("MAX_REPORT_ITEMS", "120"))

CONCURRENCY = int(os.getenv("CONCURRENCY", "30"))          # 30-50 cho 1000 domain
BATCH_SIZE  = int(os.getenv("BATCH_SIZE", "250"))          # chia batch để ổn định

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

FAIL_KEYWORDS = [
    "sorry, this shop is currently unavailable",
    "this store is unavailable",
    "shop is unavailable",
    "domain not configured",
    "enter using password",
    "password",
    "site not found",
    "this domain is parked",
    "bad gateway",
    "service unavailable",
    "gateway time-out",
    "error 502",
    "error 503",
    "error 504",
]

def now_iso_utc():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

def normalize_url(line: str) -> str:
    s = line.strip()
    if not s or s.startswith("#"):
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
    with open(DOMAINS_FILE, "r", encoding="utf-8-sig") as f:
        for line in f:
            url = normalize_url(line)
            if url:
                out.append(url)
    if not out:
        raise ValueError(f"{DOMAINS_FILE} is empty or has no valid domains.")
    # de-dupe keep order
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

def looks_down(html: str):
    if not html:
        return "EMPTY_HTML"
    s = html.lower()
    for kw in FAIL_KEYWORDS:
        if kw in s:
            return f"KEYWORD:{kw}"
    return None

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

async def check_one(context, url: str):
    """
    returns: (url, ok, status, reason, final_url)
    """
    page = await context.new_page()
    status = None
    final_url = url
    reason = None
    ok = False

    try:
        # Block heavy resources to speed up
        await page.route("**/*", lambda route: route.abort()
                         if route.request.resource_type in ("image","media","font","stylesheet")
                         else route.continue_())

        resp = await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        final_url = page.url
        status = resp.status if resp else None

        html = await page.content()
        soft = looks_down(html)

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
            await page.close()
        except Exception:
            pass

    return (url, ok, status, reason, final_url)

async def run_checks_async(domains, state):
    results_all = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(ignore_https_errors=True)

        sem = asyncio.Semaphore(CONCURRENCY)

        async def worker(u):
            async with sem:
                return await check_one(context, u)

        # process in batches to avoid overloading
        for i in range(0, len(domains), BATCH_SIZE):
            batch = domains[i:i+BATCH_SIZE]
            tasks = [asyncio.create_task(worker(u)) for u in batch]
            results = await asyncio.gather(*tasks)
            results_all.extend(results)

        await context.close()
        await browser.close()

    # update state
    for (url, ok, status, reason, final_url) in results_all:
        prev = state.get(url, {})
        fail_count = int(prev.get("fail_count", 0))

        if ok:
            state[url] = {
                "fail_count": 0,
                "last_status": status,
                "last_reason": None,
                "last_checked": now_iso_utc(),
                "last_ok": now_iso_utc(),
                "final_url": final_url,
            }
        else:
            fail_count += 1
            state[url] = {
                "fail_count": fail_count,
                "last_status": status,
                "last_reason": reason,
                "last_checked": now_iso_utc(),
                "last_ok": prev.get("last_ok"),
                "final_url": final_url,
            }

    return results_all, state

def report_and_reset(state):
    total = len(state)
    down_list = []
    up_count = 0

    for url, st in state.items():
        if int(st.get("fail_count", 0)) >= FAIL_THRESHOLD:
            down_list.append((url, st))
        else:
            up_count += 1

    msg = []
    msg.append(f"🧾 Night Monitor Summary (UTC): {now_iso_utc()}")
    msg.append(f"✅ UP: {up_count} | ❌ DOWN: {len(down_list)} | Total: {total}")
    msg.append(f"Rule: DOWN if fail_count ≥ {FAIL_THRESHOLD}")

    if down_list:
        msg.append("\n❌ DOWN LIST:")
        down_list.sort(key=lambda x: int(x[1].get("fail_count", 0)), reverse=True)
        for (url, st) in down_list[:MAX_REPORT_ITEMS]:
            msg.append(
                f"- {url} | fail={st.get('fail_count')} | status={st.get('last_status')} | {st.get('last_reason')} | last_ok={st.get('last_ok')}"
            )
        if len(down_list) > MAX_REPORT_ITEMS:
            msg.append(f"... and {len(down_list)-MAX_REPORT_ITEMS} more")

    text = "\n".join(msg)
    print(text)
    send_telegram(text)
    return {}

def main():
    if MODE == "check":
        domains = read_domains()
        state = load_state()

        results, state = asyncio.run(run_checks_async(domains, state))
        save_state(state)

        down_now = sum(1 for r in results if not r[1])
        print(f"[CHECK] {now_iso_utc()} | checked={len(results)} | down_now={down_now} | concurrency={CONCURRENCY} | state_saved")

    elif MODE == "report":
        state = load_state()
        new_state = report_and_reset(state)
        save_state(new_state)
        print("[REPORT] sent + state reset")
    else:
        raise ValueError("MODE must be check or report")

if __name__ == "__main__":
    main()
