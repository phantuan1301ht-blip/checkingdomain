import os, json, time, asyncio
import requests
from urllib.parse import urlparse
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

DOMAINS_FILE = os.getenv("DOMAINS_FILE", "domains.txt")
STATE_FILE   = os.getenv("STATE_FILE", "state.json")

MODE = os.getenv("MODE", "check").strip().lower()  # check | report
CHECK_PATH = os.getenv("CHECK_PATH", "/")

# ✅ timeout 30s (set from workflow)
TIMEOUT_MS = int(os.getenv("TIMEOUT_MS", "30000"))

# only used for TIMEOUT/HTTP5xx threshold
FAIL_THRESHOLD = int(os.getenv("FAIL_THRESHOLD", "3"))

CONCURRENCY = int(os.getenv("CONCURRENCY", "30"))
BATCH_SIZE  = int(os.getenv("BATCH_SIZE", "250"))

# manual run -> send telegram immediately
FORCE_SEND = os.getenv("FORCE_SEND", "0").strip() == "1"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

FAIL_KEYWORDS = [
    "enter using password",
    "domain not configured",
    "sorry, this shop is currently unavailable",
    "this store is unavailable",
]

# ---------------- Utils ----------------

def now_utc():
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

def only_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return url.lower()

def read_domains():
    if not os.path.exists(DOMAINS_FILE):
        raise FileNotFoundError(f"Missing {DOMAINS_FILE}")

    urls = []
    with open(DOMAINS_FILE, "r", encoding="utf-8-sig") as f:
        for line in f:
            u = normalize_url(line)
            if u:
                urls.append(u)

    if not urls:
        raise ValueError("domains.txt is empty")

    # dedupe keep order
    seen = set()
    uniq = []
    for u in urls:
        if u not in seen:
            uniq.append(u)
            seen.add(u)
    return uniq

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets missing; skip sending.")
        return
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(api, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }, timeout=25)
    r.raise_for_status()

# ---------------- Rules ----------------

def is_instant_down(reason: str | None, status: int | None) -> bool:
    # ✅ ERROR & 404 = DOWN ngay
    if reason and reason.startswith("ERROR:"):
        return True
    if status == 404:
        return True
    return False

def classify(st: dict) -> str:
    # instant down wins
    if st.get("instant_down"):
        return "DOWN"

    fc = int(st.get("fail_count", 0))
    if fc == 0:
        return "UP"
    if fc >= FAIL_THRESHOLD:
        return "DOWN"
    return "FAIL_TMP"

def reason_group(st: dict) -> str:
    status = st.get("last_status")
    reason = st.get("last_reason") or ""

    if st.get("instant_down"):
        if status == 404:
            return "DOWN — HTTP 404"
        return "DOWN — ERROR/DNS"

    if reason == "TIMEOUT":
        return f"FAIL_TMP — TIMEOUT ({TIMEOUT_MS//1000}s)"
    if isinstance(status, int) and status >= 500:
        return "FAIL_TMP — HTTP 5xx"
    if reason.startswith("KEYWORD:enter using password"):
        return "FAIL_TMP — PASSWORD PAGE"
    if reason.startswith("KEYWORD:"):
        return "FAIL_TMP — SOFT ERROR (keyword)"
    return "FAIL_TMP — OTHER"

# ---------------- Check ----------------

async def check_one(context, url: str):
    page = await context.new_page()
    status = None
    reason = None

    try:
        async def block(route):
            if route.request.resource_type in ("image", "font", "media", "stylesheet"):
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", block)

        resp = await page.goto(url, timeout=TIMEOUT_MS, wait_until="domcontentloaded")
        status = resp.status if resp else None

        # HTML keyword checks (soft errors)
        html = await page.content()
        low = html.lower()
        for k in FAIL_KEYWORDS:
            if k in low:
                reason = f"KEYWORD:{k}"
                break

        # status-based
        if reason is None:
            if status == 404:
                reason = "HTTP_404"
            elif isinstance(status, int) and status >= 500:
                reason = f"HTTP_{status}"

    except PwTimeout:
        reason = "TIMEOUT"
    except Exception as e:
        # keep short
        reason = f"ERROR:{type(e).__name__}"

    finally:
        try:
            await page.close()
        except Exception:
            pass

    return url, status, reason

async def run_checks(domains, state):
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(ignore_https_errors=True)
        sem = asyncio.Semaphore(CONCURRENCY)

        async def worker(u):
            async with sem:
                return await check_one(ctx, u)

        for i in range(0, len(domains), BATCH_SIZE):
            batch = domains[i:i+BATCH_SIZE]
            tasks = [asyncio.create_task(worker(u)) for u in batch]
            results.extend(await asyncio.gather(*tasks))

        await ctx.close()
        await browser.close()

    # update state
    for url, status, reason in results:
        prev = state.get(url, {})
        instant = is_instant_down(reason, status)

        if reason is None:
            state[url] = {
                "fail_count": 0,
                "last_status": status,
                "last_reason": None,
                "instant_down": False,
                "last_ok": now_utc(),
                "last_checked": now_utc(),
            }
        else:
            state[url] = {
                "fail_count": int(prev.get("fail_count", 0)) + 1,
                "last_status": status,
                "last_reason": reason,
                "instant_down": instant,
                "last_ok": prev.get("last_ok"),
                "last_checked": now_utc(),
            }

    return state

# ---------------- Message formatting (Domains only) ----------------

def build_summary(title: str, state: dict) -> str:
    total = len(state)
    up = fail_tmp = down = 0

    # group -> set(domains)
    groups = {}

    for url, st in state.items():
        dom = only_domain(url)
        cls = classify(st)

        if cls == "UP":
            up += 1
            continue

        if cls == "DOWN":
            down += 1
        else:
            fail_tmp += 1

        gname = reason_group(st)
        groups.setdefault(gname, set()).add(dom)

    lines = [
        f"{title} (UTC): {now_utc()}",
        f"Checked: {total} | ✅ UP: {up} | ⚠️ FAIL_TMP: {fail_tmp} | ❌ DOWN: {down}",
        f"Rule: ERROR & 404 = DOWN | Timeout={TIMEOUT_MS//1000}s | Threshold={FAIL_THRESHOLD}",
    ]

    # order: down groups first, then fail_tmp groups
    order = [
        "DOWN — ERROR/DNS",
        "DOWN — HTTP 404",
        f"FAIL_TMP — TIMEOUT ({TIMEOUT_MS//1000}s)",
        "FAIL_TMP — HTTP 5xx",
        "FAIL_TMP — PASSWORD PAGE",
        "FAIL_TMP — SOFT ERROR (keyword)",
        "FAIL_TMP — OTHER",
    ]

    for key in order:
        doms = sorted(groups.get(key, []))
        if not doms:
            continue
        lines.append(f"\n{key} ({len(doms)}):")
        for d in doms:
            lines.append(f"- {d}")

    if len(lines) <= 3:
        lines.append("\n✅ All domains look OK.")

    return "\n".join(lines)

# ---------------- Main ----------------

def main():
    domains = read_domains()
    state = load_state()

    if MODE == "check":
        state = asyncio.run(run_checks(domains, state))
        save_state(state)

        # Manual run: send immediately for test
        if FORCE_SEND:
            msg = build_summary("🧪 Test Run Result", state)
            send_telegram(msg)

        # Always print a short log for Actions
        up = sum(1 for st in state.values() if classify(st) == "UP")
        down = sum(1 for st in state.values() if classify(st) == "DOWN")
        fail_tmp = len(state) - up - down
        print(f"[CHECK] {now_utc()} | total={len(state)} | UP={up} | FAIL_TMP={fail_tmp} | DOWN={down}")

    elif MODE == "report":
        msg = build_summary("🧾 Night Monitor Summary", state)
        send_telegram(msg)
        # reset after report
        save_state({})
        print("[REPORT] sent + state reset")

    else:
        raise ValueError("MODE must be check or report")

if __name__ == "__main__":
    main()
