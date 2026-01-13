import os, json, time, asyncio, re
import requests
from urllib.parse import urlparse
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

DOMAINS_FILE = os.getenv("DOMAINS_FILE", "domains.txt")
STATE_FILE   = os.getenv("STATE_FILE", "state.json")

MODE = os.getenv("MODE", "check").strip().lower()  # check | report
CHECK_PATH = os.getenv("CHECK_PATH", "/")

# ✅ Timeout 30s (set from workflow TIMEOUT_MS="30000")
TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "30000"))

FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "3"))
MAX_REPORT_ITEMS = int(os.getenv("MAX_REPORT_ITEMS", "200"))

CONCURRENCY = int(os.getenv("CONCURRENCY", "30"))
BATCH_SIZE  = int(os.getenv("BATCH_SIZE", "250"))

# For Actions logs
LOG_SAMPLE_LIMIT = int(os.getenv("LOG_SAMPLE_LIMIT", "40"))

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

def short_reason(reason: str | None) -> str:
    if not reason:
        return ""
    # Remove extremely long playwright call log noise if any
    reason = reason.replace("\n", " ").strip()
    reason = re.sub(r"\s+", " ", reason)
    # Keep it short
    if len(reason) > 90:
        reason = reason[:87] + "..."
    return reason

def reason_group(reason: str | None, status: int | None) -> str:
    """
    Group for nicer reporting.
    """
    if reason is None:
        return "OK"
    if reason.startswith("HTTP_404"):
        return "HTTP 404"
    if reason.startswith("HTTP_5") or (status and status >= 500):
        return "HTTP 5xx"
    if reason == "TIMEOUT":
        return "TIMEOUT (30s)"
    if "ERR_NAME_NOT_RESOLVED" in reason:
        return "DNS / DOMAIN NOT FOUND"
    if "ERR_CONNECTION_REFUSED" in reason or "ERR_CONNECTION_RESET" in reason:
        return "CONNECTION ERROR"
    if "KEYWORD:enter using password" in reason:
        return "PASSWORD PAGE"
    if reason.startswith("KEYWORD:"):
        return "SOFT ERROR (keyword)"
    if reason.startswith("ERROR:"):
        return "BROWSER/NETWORK ERROR"
    return "OTHER"

async def check_one(context, url: str):
    page = await context.new_page()
    status = None
    final_url = url
    reason = None
    ok = False

    try:
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

    # update state
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

    non_up = []
    for r in results:
        st = state.get(r["url"], {})
        cls = classify_state(st)
        if cls == "UP":
            up += 1
        elif cls == "DOWN":
            down += 1
        else:
            fail_tmp += 1
        if cls != "UP":
            non_up.append((cls, r["url"], st.get("fail_count"), st.get("last_status"), st.get("last_reason")))

    print(
        f"[CHECK] {now_iso_utc()} | checked={checked} | "
        f"UP={up} | FAIL_TMP={fail_tmp} | DOWN_CONFIRMED={down} | "
        f"timeout_ms={TIMEOUT_MS} | conc={CONCURRENCY} | state_saved"
    )

    non_up.sort(key=lambda x: (0 if x[0] == "DOWN" else 1, -(int(x[2]) if x[2] else 0)))
    if non_up:
        print(f"Non-UP sample (max {LOG_SAMPLE_LIMIT}):")
        for (cls, url, fc, status, reason) in non_up[:LOG_SAMPLE_LIMIT]:
            print(f" - {cls} | fail={fc} | status={status} | {short_reason(reason)} | {url}")

def format_group_section(title, items, limit):
    """
    items: list of tuples (fail_count, status, reason, url)
    """
    if not items:
        return []
    lines = [f"\n{title} ({len(items)}):"]
    for (fc, status, reason, url) in items[:limit]:
        status_txt = str(status) if status is not None else "-"
        lines.append(f"• ({fc}) [{status_txt}] {short_reason(reason)} — {url}")
    if len(items) > limit:
        lines.append(f"… +{len(items) - limit} more")
    return lines

def build_pretty_summary(title_prefix, results, state):
    checked = len(results)
    up = fail_tmp = down = 0

    # Collect non-up items grouped by reason type
    groups = {}

    for r in results:
        url = r["url"]
        st = state.get(url, {})
        cls = classify_state(st)

        if cls == "UP":
            up += 1
            continue
        elif cls == "DOWN":
            down += 1
        else:
            fail_tmp += 1

        fc = int(st.get("fail_count", 0))
        status = st.get("last_status")
        reason = st.get("last_reason")

        g = reason_group(reason, status)
        groups.setdefault(g, []).append((fc, status, reason, url))

    # Sort inside each group by fail_count desc then status
    for g in groups:
        groups[g].sort(key=lambda x: (-x[0], (999 if x[1] is None else x[1])))

    # Overall ordering: most critical first
    group_order = [
        "DNS / DOMAIN NOT FOUND",
        "TIMEOUT (30s)",
        "HTTP 5xx",
        "CONNECTION ERROR",
        "HTTP 404",
        "PASSWORD PAGE",
        "SOFT ERROR (keyword)",
        "BROWSER/NETWORK ERROR",
        "OTHER",
    ]

    lines = []
    lines.append(f"{title_prefix} (UTC): {now_iso_utc()}")
    lines.append(f"📌 Checked: {checked} | ✅ UP: {up} | ⚠️ FAIL_TMP: {fail_tmp} | ❌ DOWN: {down}")
    lines.append(f"⚙️ Rule: DOWN if fail_count ≥ {FAIL_THRESHOLD} | Timeout: {TIMEOUT_MS//1000}s | Concurrency: {CONCURRENCY}")

    # If nothing wrong
    if not groups:
        lines.append("\n✅ All domains look OK.")
        return "\n".join(lines)

    # Decide per-group limit to keep message readable
    per_group_limit = 25 if checked <= 200 else 15

    # Put DOWN first section if any
    # We rebuild a "confirmed down" list regardless of group
    down_items = []
    for r in results:
        url = r["url"]
        st = state.get(url, {})
        if classify_state(st) == "DOWN":
            down_items.append((int(st.get("fail_count", 0)), st.get("last_status"), st.get("last_reason"), url))
    down_items.sort(key=lambda x: (-x[0], (999 if x[1] is None else x[1])))

    if down_items:
        lines += format_group_section("❌ DOWN (confirmed)", down_items, min(MAX_REPORT_ITEMS, 60))

    # FAIL_TMP groups
    for g in group_order:
        items = groups.get(g, [])
        # Only show FAIL_TMP here; skip items that are confirmed down if already shown above
        filtered = [it for it in items if it[0] < FAIL_THRESHOLD]
        if not filtered:
            continue
        lines += format_group_section(f"⚠️ FAIL_TMP — {g}", filtered, per_group_limit)

    return "\n".join(lines)

def report_and_reset(state):
    total = len(state)
    # build report from state (not from results)
    # We convert state into a pseudo-results list to reuse formatter
    pseudo_results = []
    for url, st in state.items():
        pseudo_results.append({
            "url": url,
            "ok": (int(st.get("fail_count", 0)) == 0),
            "status": st.get("last_status"),
            "reason": st.get("last_reason"),
            "final_url": st.get("final_url", url),
        })

    text = build_pretty_summary("🧾 Night Monitor Summary", pseudo_results, state)
    print(text)
    send_telegram(text)
    return {}  # reset

def main():
    if MODE == "check":
        domains = read_domains()
        state = load_state()
        results, state = asyncio.run(run_checks_async(domains, state))
        save_state(state)

        build_check_log(results, state)

        # ✅ Manual test run => send immediately
        if FORCE_SEND:
            text = build_pretty_summary("🧪 Test Run Result", results, state)
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
