import os, json, time, asyncio
import requests
from urllib.parse import urlparse
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

DOMAINS_FILE = os.getenv("DOMAINS_FILE", "domains.txt")
STATE_FILE   = os.getenv("STATE_FILE", "state.json")

MODE = os.getenv("MODE", "check").strip().lower()  # check | report
CHECK_PATH = os.getenv("CHECK_PATH", "/")

TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "8000"))
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "3"))
MAX_REPORT_ITEMS = int(os.getenv("MAX_REPORT_ITEMS", "150"))

CONCURRENCY = int(os.getenv("CONCURRENCY", "30"))
BATCH_SIZE  = int(os.getenv("BATCH_SIZE", "250"))

LOG_SAMPLE_LIMIT = int(os.getenv("LOG_SAMPLE_LIMIT", "60"))

# ✅ Manual run test => send telegram immediately after check
FORCE_SEND = os.getenv("FORCE_SEND", "0").strip() == "1"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

FAIL_KEYWORDS = [
    # Shopify
    "sorry, this shop is currently unavailable",
    "this store is unavailable",
    "shop is unavailable",
    "domain not configured",
    "enter using password",
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

def classify_state(st: dict) -> str:
    fc = int(st.get("fail_count", 0))
    if fc == 0:
        return "UP"
    if fc >= FAIL_THRESHOLD:
        return "DOWN"
    return "FAIL_TMP"

async def check_one(context, url: str):
    """
    returns dict: {url, ok, status, reason, final_url}
    """
    page = await context.new_page()
    status = None
    final_url = url
    reason = None
    ok = False

    try:
        # block heavy resources for speed
        async def route_handler(route):
            if route.request.resource_type in ("image", "media", "font", "stylesheet"):
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", route_handler)

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

    return {"url": url, "ok": ok, "status": status, "reason": reason, "final_url": final_url}

async def run_checks_async(domains, state):
    results_all = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(ignore_https_errors=True)

        sem = asyncio.Semaphore(CONCURRENCY)

        async def worker(u):
            async with sem:
                return await check_one(context, u)

        for i in range(0, len(domains), BATCH_SIZE):
            batch = domains[i:i+BATCH_SIZE]
            tasks = [asyncio.create_task(worker(u)) for u in batch]
            results = await asyncio.gather(*tasks)
            results_all.extend(results)

        await context.close()
        await browser.close()

    # update state from this round
    for r in results_all:
        url = r["url"]
        ok = r["ok"]
        status = r["status"]
        reason = r["reason"]
        final_url = r["final_url"]

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

def build_check_log(results, state):
    checked = len(results)
    up = fail_tmp = down = 0

    for r in results:
        st = state.get(r["url"], {})
        cls = classify_state(st)
        if cls == "UP":
            up += 1
        elif cls == "DOWN":
            down += 1
        else:
            fail_tmp += 1

    print(
        f"[CHECK] {now_iso_utc()} | checked={checked} | "
        f"UP={up} | FAIL_TMP={fail_tmp} | DOWN_CONFIRMED={down} | "
        f"conc={CONCURRENCY} | state_saved"
    )

    # sample non-UP for debugging
    samples = []
    for r in results:
        st = state.get(r["url"], {})
        cls = classify_state(st)
        if cls != "UP":
            samples.append((cls, r["url"], st.get("fail_count"), st.get("last_status"), st.get("last_reason")))

    samples.sort(key=lambda x: (0 if x[0] == "DOWN" else 1, -(int(x[2]) if x[2] else 0)))

    if samples:
        print(f"Non-UP sample (max {LOG_SAMPLE_LIMIT}):")
        for (cls, url, fc, status, reason) in samples[:LOG_SAMPLE_LIMIT]:
            print(f" - {cls} | fail={fc} | status={status} | {reason} | {url}")

def build_instant_summary(results, state):
    checked = len(results)
    up = fail_tmp = down = 0
    down_list = []
    fail_tmp_list = []

    for r in results:
        st = state.get(r["url"], {})
        cls = classify_state(st)
        if cls == "UP":
            up += 1
        elif cls == "DOWN":
            down += 1
            down_list.append((r["url"], st))
        else:
            fail_tmp += 1
            fail_tmp_list.append((r["url"], st))

    msg = []
    msg.append(f"🧪 Test Run Result (UTC): {now_iso_utc()}")
    msg.append(f"checked={checked} | ✅UP={up} | ⚠️FAIL_TMP={fail_tmp} | ❌DOWN={down}")
    msg.append(f"Rule: DOWN if fail_count ≥ {FAIL_THRESHOLD}")

    if down_list:
        msg.append("\n❌ DOWN (confirmed):")
        down_list.sort(key=lambda x: int(x[1].get("fail_count", 0)), reverse=True)
        for (url, st) in down_list[:min(50, MAX_REPORT_ITEMS)]:
            msg.append(f"- fail={st.get('fail_count')} | status={st.get('last_status')} | {st.get('last_reason')} | {url}")

    if fail_tmp_list:
        msg.append("\n⚠️ FAIL_TMP (not confirmed yet):")
        fail_tmp_list.sort(key=lambda x: int(x[1].get("fail_count", 0)), reverse=True)
        for (url, st) in fail_tmp_list[:30]:
            msg.append(f"- fail={st.get('fail_count')} | status={st.get('last_status')} | {st.get('last_reason')} | {url}")

    return "\n".join(msg)

def report_and_reset(state):
    total = len(state)
    up = fail_tmp = down = 0
    down_list = []
    fail_tmp_list = []

    for url, st in state.items():
        cls = classify_state(st)
        if cls == "UP":
            up += 1
        elif cls == "DOWN":
            down += 1
            down_list.append((url, st))
        else:
            fail_tmp += 1
            fail_tmp_list.append((url, st))

    msg = []
    msg.append(f"🧾 Night Monitor Summary (UTC): {now_iso_utc()}")
    msg.append(f"Total: {total} | ✅ UP: {up} | ⚠️ FAIL_TMP: {fail_tmp} | ❌ DOWN: {down}")
    msg.append(f"Rule: DOWN if fail_count ≥ {FAIL_THRESHOLD}")

    if down_list:
        msg.append("\n❌ DOWN (confirmed):")
        down_list.sort(key=lambda x: int(x[1].get("fail_count", 0)), reverse=True)
        for (url, st) in down_list[:MAX_REPORT_ITEMS]:
            msg.append(
                f"- fail={st.get('fail_count')} | status={st.get('last_status')} | {st.get('last_reason')} | last_ok={st.get('last_ok')} | {url}"
            )
        if len(down_list) > MAX_REPORT_ITEMS:
            msg.append(f"... and {len(down_list)-MAX_REPORT_ITEMS} more")

    if fail_tmp_list:
        msg.append("\n⚠️ FAIL_TMP (not confirmed yet):")
        fail_tmp_list.sort(key=lambda x: int(x[1].get("fail_count", 0)), reverse=True)
        for (url, st) in fail_tmp_list[:30]:
            msg.append(f"- fail={st.get('fail_count')} | status={st.get('last_status')} | {st.get('last_reason')} | {url}")

    text = "\n".join(msg)
    print(text)
    send_telegram(text)

    # reset for next night
    return {}

def main():
    if MODE == "check":
        domains = read_domains()
        state = load_state()
        results, state = asyncio.run(run_checks_async(domains, state))
        save_state(state)

        build_check_log(results, state)

        # ✅ Manual test run => send immediately
        if FORCE_SEND:
            text = build_instant_summary(results, state)
            print("\n[SEND_NOW] Manual test run => sending Telegram summary...")
            send_telegram(text)

    elif MODE == "report":
        state = load_state()
        new_state = report_and_reset(state)
        save_state(new_state)
        print("[REPORT] sent + state reset")
    else:
        raise ValueError("MODE must be check or report")

if __name__ == "__main__":
    main()
