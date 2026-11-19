"""本机真实 Chrome（有头）添加账号：窗口可见，短信码走 sms_code.txt，滑块由用户手动拖。

用法:
  1) 面板先拿 state:  curl -X POST http://127.0.0.1:7863/panel/api/login/start \
         -H "Authorization: Bearer <api_key>" -H "Content-Type: application/json" \
         -d '{"realm":"cn"}' > login_state.json
  2) python scripts/login_account.py --phone 139xxxxxxxx [--tag acc1] [--state login_state.json]
  3) 短信验证码到达后: printf '<code>' > sms_code.txt （脚本读后即删）
     若触发「安全验证」第二步会有第二条短信，同样写入即可；滑块在窗口手动拖。

流程: 打开授权页 → 手机号tab → 填号 → 协议 → 获取验证码 → 等码 → 填码 → 登录
      → (若出现)安全验证再收一条码 → 完成验证并登录 → 轮询面板确认入池。
"""
import json, time, os, sys, io, urllib.request, argparse

ap = argparse.ArgumentParser()
ap.add_argument("--phone", required=True)
ap.add_argument("--state", default=r"D:\ps\wb2api\login_state.json")
ap.add_argument("--tag", default="acc")
ap.add_argument("--api-key", default=os.environ.get("WB2A_API_KEY", ""))
ap.add_argument("--panel", default="http://127.0.0.1:7863")
a = ap.parse_args()

BASE = os.path.dirname(os.path.abspath(__file__)) or r"D:\ps\wb2api"
sys.stdout = sys.stderr = open(os.path.join(BASE, f"login_{a.tag}.log"), "w", buffering=1, encoding="utf-8")
from playwright.sync_api import sync_playwright

def log(*a): print(*a, flush=True)

PHONE = a.phone
CODE_FILE = os.path.join(BASE, "sms_code.txt")
if os.path.exists(CODE_FILE): os.remove(CODE_FILE)
st = json.load(open(a.state))
url, state = st["url"], st["state"]
API_KEY = a.api_key
log("state:", state, "url:", url)

def dump(page, tag):
    try:
        page.screenshot(path=os.path.join(BASE, f"chrome_{a.tag}_{tag}.png"))
    except Exception as e:
        log("shot err:", repr(e))
    try:
        txt = page.evaluate("() => (document.body.innerText||'').slice(0,1500)")
        log(f"[{tag}]", txt.replace("\n", " | ")[:1500])
    except Exception as e:
        log("eval err:", repr(e))

def wait_code(maxsec=600):
    log(f"waiting code file (max {maxsec}s) ...")
    for i in range(maxsec // 2):
        if os.path.exists(CODE_FILE):
            c = open(CODE_FILE, encoding="utf-8").read().strip()
            os.remove(CODE_FILE)
            if c: return c
        time.sleep(2)
    return None

def panel_poll():
    req = urllib.request.Request(
        f"{a.panel}/panel/api/login/poll?state={state}",
        headers={"Authorization": "Bearer " + API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code}

with sync_playwright() as p:
    # 真实 Chrome，有头窗口（用户可见、可手动拖滑块）
    browser = p.chromium.launch(channel="chrome", headless=False,
                                args=["--start-maximized", "--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context(no_viewport=True, locale="zh-CN")
    # 抹掉 webdriver 痕迹
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    page = ctx.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    time.sleep(5)
    dump(page, "open")
    log("page url:", page.url)

    # 手机号 tab
    page.get_by_role("button", name="手机号", exact=True).first.click()
    log("clicked 手机号 tab")
    time.sleep(2)
    pi = page.locator('input[placeholder="请输入手机号"]')
    pi.click(); pi.fill(PHONE)
    log("phone filled:", pi.input_value())
    cb = page.locator("input.wb-login-v3-checkbox__input")
    if cb.count() and not cb.first.is_checked():
        cb.first.check(force=True); log("agreement checked")
    time.sleep(1)

    done = False
    for attempt in range(1, 4):
        try:
            page.get_by_text("获取验证码", exact=True).first.click()
        except Exception:
            page.get_by_text("重新获取", exact=True).first.click()
        log(f"== SMS requested (attempt {attempt}) ==")
        time.sleep(3)
        dump(page, f"s1_send_{attempt}")

        # 等待：短信码（sms_code.txt）或用户手动过滑块（页面变化）
        c = wait_code(600)
        if not c:
            log("no code, retry")
            continue
        log("code1:", c)
        ci = page.locator('input[placeholder="请输入验证码"]')
        ci.click(); ci.fill(c)
        time.sleep(1)
        page.locator("button:has-text('登录')").first.click()
        log("clicked 登录")
        time.sleep(6)
        dump(page, f"s1_after_{attempt}")
        b = page.evaluate("() => (document.body.innerText||'')")
        if "验证码过期" in b or "请重新获取" in b:
            log("code expired; resend")
            continue
        if "安全验证" in b or "完成验证并登录" in b:
            log("== step2 安全验证（窗口可见，如出现滑块请手动拖） ==")
            for a2 in range(1, 4):
                try:
                    page.get_by_text("获取验证码", exact=True).first.click()
                except Exception:
                    page.get_by_text("重新获取", exact=True).first.click()
                log(f"== SMS#2 requested (attempt {a2}) ==")
                time.sleep(3)
                dump(page, f"s2_send_{a2}")
                c2 = wait_code(600)
                if not c2:
                    log("no code step2", a2); continue
                log("code2:", c2)
                filled = False
                for ph in ["请输入验证码", "请输入短信验证码", "验证码"]:
                    loc = page.locator(f'input[placeholder*="{ph}"]')
                    if loc.count():
                        loc.first.click(); loc.first.fill(c2); filled = True
                        log("code2 filled via", ph)
                        break
                if not filled:
                    ins = page.locator("input:visible")
                    ins.last.click(); ins.last.fill(c2)
                    log("code2 filled into last visible input")
                time.sleep(1)
                clicked = False
                for sel in ["text=完成验证并登录", "button:has-text('完成验证')"]:
                    try:
                        page.locator(sel).first.click(timeout=4000); clicked = True
                        log("clicked:", sel)
                        break
                    except Exception:
                        continue
                if not clicked:
                    log("WARN step2 button not found")
                time.sleep(6)
                dump(page, f"s2_after_{a2}")
                b2 = page.evaluate("() => (document.body.innerText||'')")
                if "验证码过期" in b2:
                    log("code2 expired; resend")
                    continue
                break
            break
        ok = True
        break

    log("page url now:", page.url)
    for i in range(60):
        r = panel_poll()
        log("poll:", json.dumps(r, ensure_ascii=False)[:300])
        if r.get("done"):
            log("LOGIN DONE ✓")
            break
        if r.get("http_error"):
            log("state unknown/expired; stop")
            break
        time.sleep(3)
    log("browser stays open 60s for inspection ...")
    time.sleep(60)
    browser.close()
log("script end")
