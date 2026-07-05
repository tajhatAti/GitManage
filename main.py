import os, asyncio, threading, json, time, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
import aiohttp

# ── ENV ───────────────────────────────────────────────────────────────────────
API_ID   = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID  = int(os.environ.get("OWNER_ID", 0))
UPTIME_KEY = os.environ.get("UPTIME_API_KEY", "U3590006-1ec798164e91a51e56f8cdf0")

ACCOUNTS_FILE = "accounts.json"
SERVICES_FILE = "services.json"
RENDER_BASE   = "https://api.render.com/v1"
UPTIME_BASE   = "https://api.uptimerobot.com/v2"

# ── STATE ─────────────────────────────────────────────────────────────────────
active_account = {"name": None, "api_key": None, "owner_id": None}
waiting_state  = {}   # uid -> {"action": ..., "service_id": ..., "monitor_id": ...}

bot = TelegramClient("render_smart_bot", API_ID, API_HASH)

# ── WEB SERVER ────────────────────────────────────────────────────────────────
class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a): pass

def run_server():
    HTTPServer(('0.0.0.0', int(os.environ.get("PORT", 8080))), _H).serve_forever()

# ── PERSISTENCE ───────────────────────────────────────────────────────────────
def load_accounts() -> dict:
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE) as f: return json.load(f)
        except: pass
    return {}

def save_accounts(data: dict):
    with open(ACCOUNTS_FILE, "w") as f: json.dump(data, f, indent=2)

def load_services() -> dict:
    if os.path.exists(SERVICES_FILE):
        try:
            with open(SERVICES_FILE) as f: return json.load(f)
        except: pass
    return {}

def save_services(data: dict):
    with open(SERVICES_FILE, "w") as f: json.dump(data, f, indent=2)

# ── RENDER API ────────────────────────────────────────────────────────────────
def rh(api_key: str = None) -> dict:
    key = api_key or active_account.get("api_key", "")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        "Accept":        "application/json"
    }

def extract_err(data) -> str:
    if isinstance(data, dict):
        return data.get("message", json.dumps(data))[:300]
    return str(data)[:300]

async def r_get(path: str, api_key: str = None):
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{RENDER_BASE}{path}",
            headers=rh(api_key),
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            try: data = await r.json(content_type=None)
            except: data = await r.text()
            return r.status, data

async def r_post(path: str, payload: dict = {}, api_key: str = None):
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{RENDER_BASE}{path}",
            headers=rh(api_key),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            try: data = await r.json(content_type=None)
            except: data = await r.text()
            return r.status, data

async def r_put(path: str, payload, api_key: str = None):
    async with aiohttp.ClientSession() as s:
        async with s.put(
            f"{RENDER_BASE}{path}",
            headers=rh(api_key),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            try: data = await r.json(content_type=None)
            except: data = await r.text()
            return r.status, data

async def r_delete(path: str, api_key: str = None):
    async with aiohttp.ClientSession() as s:
        async with s.delete(
            f"{RENDER_BASE}{path}",
            headers=rh(api_key),
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            try: data = await r.json(content_type=None)
            except: data = await r.text()
            return r.status, data

# ── UPTIMEROBOT API ───────────────────────────────────────────────────────────
async def ut_new_monitor(name: str, url: str) -> str | None:
    payload = {
        "api_key":       UPTIME_KEY,
        "format":        "json",
        "type":          1,
        "url":           url,
        "friendly_name": name
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{UPTIME_BASE}/newMonitor",
                data=payload,
                timeout=aiohttp.ClientTimeout(total=20)
            ) as r:
                data = await r.json(content_type=None)
        if data.get("stat") == "ok":
            return str(data.get("monitor", {}).get("id", ""))
        return None
    except: return None

async def ut_edit_monitor(monitor_id: str, status: int):
    payload = {
        "api_key":    UPTIME_KEY,
        "format":     "json",
        "id":         monitor_id,
        "status":     status
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{UPTIME_BASE}/editMonitor",
                data=payload,
                timeout=aiohttp.ClientTimeout(total=20)
            ) as r:
                return await r.json(content_type=None)
    except: return {}

async def ut_delete_monitor(monitor_id: str):
    payload = {"api_key": UPTIME_KEY, "format": "json", "id": monitor_id}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{UPTIME_BASE}/deleteMonitor",
                data=payload,
                timeout=aiohttp.ClientTimeout(total=20)
            ) as r:
                return await r.json(content_type=None)
    except: return {}

# ── SLEEP TASK ────────────────────────────────────────────────────────────────
async def sleep_and_wake(service_id: str, monitor_id: str, seconds: int, api_key: str, label: str):
    await bot.send_message(
        OWNER_ID,
        f"⏸️ **Sleep Started**\n\n"
        f"Service: `{service_id}`\n"
        f"Duration: `{label}`\n"
        f"Will wake up automatically."
    )
    await asyncio.sleep(seconds)
    await r_post(f"/services/{service_id}/resume", api_key=api_key)
    if monitor_id:
        await ut_edit_monitor(monitor_id, 1)
    await bot.send_message(
        OWNER_ID,
        f"✅ **Service Woke Up!**\n\n"
        f"Service: `{service_id}`\n"
        f"UptimeRobot monitor resumed."
    )

def parse_duration(text: str):
    text = text.strip().lower()
    m = re.match(r'^(\d+)(h|m|s)$', text)
    if not m: return None, None
    val  = int(m.group(1))
    unit = m.group(2)
    if unit == 'h': return val * 3600, f"{val} hour(s)"
    if unit == 'm': return val * 60,   f"{val} minute(s)"
    if unit == 's': return val,         f"{val} second(s)"
    return None, None

# ── INLINE KEYBOARDS ──────────────────────────────────────────────────────────
def manage_keyboard(service_id: str):
    return [
        [Button.inline("📊 Status",     f"status_{service_id}"),
         Button.inline("📜 Logs",       f"logs_{service_id}")],
        [Button.inline("➕ Add Env",    f"env_{service_id}"),
         Button.inline("🚀 Deploy",     f"deploy_{service_id}")],
        [Button.inline("⏸️ Suspend",    f"suspend_{service_id}"),
         Button.inline("▶️ Resume",     f"resume_{service_id}")],
        [Button.inline("⏱️ Custom Sleep", f"sleep_{service_id}")],
        [Button.inline("🗑️ Delete",     f"delete_{service_id}")]
    ]

def accounts_keyboard(accounts: dict, active_name: str):
    buttons = []
    for name in accounts:
        icon = "✅ " if name == active_name else ""
        buttons.append([Button.inline(f"{icon}{name}", f"acct_{name}")])
    buttons.append([Button.inline("➕ Add Account", "acct_help")])
    return buttons

def owner(e): return e.sender_id == OWNER_ID

# ── /start ────────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="^/start$"))
async def _(e):
    if not owner(e): return
    acct = active_account.get("name") or "None"
    await e.reply(
        "**Render & UptimeRobot Smart Manager**\n\n"
        f"Active Account: `{acct}`\n\n"
        "`/accounts` — manage accounts\n"
        "`/add_account <name> <api_key> <owner_id>` — add account\n"
        "`/create_web <name> <github_url>` — create service\n"
        "`/manage <service_id>` — manage service\n"
        "`/list_services` — list all services\n"
        "`/whoami` — render account info"
    )

# ── /add_account ──────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern=r"^/add_account (\S+) (\S+) (\S+)$"))
async def _(e):
    if not owner(e): return
    name     = e.pattern_match.group(1).strip()
    api_key  = e.pattern_match.group(2).strip()
    owner_id = e.pattern_match.group(3).strip()

    accounts = load_accounts()
    accounts[name] = {"api_key": api_key, "owner_id": owner_id}
    save_accounts(accounts)

    if not active_account.get("name"):
        active_account["name"]     = name
        active_account["api_key"]  = api_key
        active_account["owner_id"] = owner_id

    await e.reply(
        f"✅ Account `{name}` added!\n\n"
        f"Use `/accounts` to switch accounts.",
        buttons=[[Button.inline(f"🔄 Switch to {name}", f"acct_{name}")]]
    )

# ── /accounts ─────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="^/accounts$"))
async def _(e):
    if not owner(e): return
    accounts = load_accounts()
    if not accounts:
        return await e.reply(
            "No accounts added yet.\n\n"
            "Use `/add_account <name> <api_key> <owner_id>` to add one."
        )
    active = active_account.get("name", "")
    await e.reply(
        "**Accounts**\nSelect to activate:",
        buttons=accounts_keyboard(accounts, active)
    )

# ── /whoami ───────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="^/whoami$"))
async def _(e):
    if not owner(e): return
    if not active_account.get("api_key"):
        return await e.reply("❌ No active account. Use `/add_account` first.")
    msg = await e.reply("⏳ Fetching account info...")
    try:
        status, data = await r_get("/owners?limit=10")
        if status == 200:
            owners = data if isinstance(data, list) else []
            if not owners:
                return await msg.edit("No owners found.")
            lines = []
            for item in owners:
                o = item.get("owner", item)
                lines.append(
                    f"• **{o.get('name','—')}** (`{o.get('type','—')}`)\n"
                    f"  ID: `{o.get('id','—')}`"
                )
            await msg.edit("**Render Account(s)**\n\n" + "\n\n".join(lines))
        else:
            await msg.edit(f"❌ Failed ({status})\n\n`{extract_err(data)}`")
    except Exception as ex:
        await msg.edit(f"❌ Error: `{ex}`")

# ── /create_web ───────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern=r"^/create_web (\S+) (\S+)$"))
async def _(e):
    if not owner(e): return
    if not active_account.get("api_key"):
        return await e.reply("❌ No active account. Use `/add_account` first.")

    name     = e.pattern_match.group(1).strip()
    repo_url = e.pattern_match.group(2).strip()
    rid      = active_account["owner_id"]

    if not rid:
        return await e.reply("❌ Active account has no `owner_id`. Re-add account with owner ID.")

    msg = await e.reply(f"⏳ Creating service `{name}` on Render...")
    payload = {
        "type":       "web_service",
        "name":       name,
        "ownerId":    rid,
        "repo":       repo_url,
        "autoDeploy": "yes",
        "branch":     "main",
        "serviceDetails": {
            "env":          "python",
            "region":       "singapore",
            "plan":         "free",
            "buildCommand": "pip install -r requirements.txt",
            "startCommand": "python main.py"
        }
    }
    try:
        status, data = await r_post("/services", payload)
        if status == 201:
            svc   = data.get("service", data) if isinstance(data, dict) else {}
            sid   = svc.get("id", "—")
            sname = svc.get("name", name)
            det   = svc.get("serviceDetails", {})
            surl  = det.get("url", "")

            monitor_id = None
            ut_msg = "UptimeRobot: skipped (no URL yet)"
            if surl:
                await msg.edit(f"✅ Service created!\n⏳ Setting up UptimeRobot monitor for `{surl}`...")
                monitor_id = await ut_new_monitor(sname, surl)
                ut_msg = f"UptimeRobot Monitor ID: `{monitor_id}`" if monitor_id else "UptimeRobot: failed to create monitor"

            svcs = load_services()
            svcs[sid] = {
                "name":       sname,
                "url":        surl,
                "monitor_id": monitor_id,
                "account":    active_account["name"],
                "api_key":    active_account["api_key"]
            }
            save_services(svcs)

            await msg.edit(
                f"✅ **Service Created!**\n\n"
                f"Name: `{sname}`\n"
                f"ID: `{sid}`\n"
                f"URL: `{surl or 'Pending...'}`\n"
                f"{ut_msg}",
                buttons=manage_keyboard(sid)
            )
        else:
            await msg.edit(f"❌ **Create Failed ({status})**\n\n`{extract_err(data)}`")
    except asyncio.TimeoutError:
        await msg.edit("❌ Request timeout.")
    except Exception as ex:
        await msg.edit(f"❌ Error: `{ex}`")

# ── /manage ───────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern=r"^/manage (\S+)$"))
async def _(e):
    if not owner(e): return
    sid  = e.pattern_match.group(1).strip()
    svcs = load_services()
    svc  = svcs.get(sid, {})
    name = svc.get("name", sid)
    url  = svc.get("url", "—")
    mid  = svc.get("monitor_id", "—")
    await e.reply(
        f"**Managing: {name}**\n"
        f"ID: `{sid}`\n"
        f"URL: `{url}`\n"
        f"Monitor ID: `{mid}`",
        buttons=manage_keyboard(sid)
    )

# ── /list_services ────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="^/list_services$"))
async def _(e):
    if not owner(e): return
    if not active_account.get("api_key"):
        return await e.reply("❌ No active account.")
    msg = await e.reply("⏳ Fetching services...")
    try:
        path = "/services?limit=20"
        if active_account.get("owner_id"):
            path += f"&ownerId={active_account['owner_id']}"
        status, data = await r_get(path)
        if status == 200:
            services = data if isinstance(data, list) else []
            if not services:
                return await msg.edit("No services found.")
            buttons = []
            for item in services:
                svc   = item.get("service", item) if isinstance(item, dict) else {}
                sname = svc.get("name", "—")
                sid   = svc.get("id", "—")
                susp  = svc.get("suspended", "not_suspended")
                icon  = "✅" if susp == "not_suspended" else "⏸️"
                buttons.append([Button.inline(f"{icon} {sname}", f"manage_btn_{sid}")])
            await msg.edit(
                f"**Services ({len(services)})**\nSelect to manage:",
                buttons=buttons
            )
        else:
            await msg.edit(f"❌ Failed ({status})\n\n`{extract_err(data)}`")
    except Exception as ex:
        await msg.edit(f"❌ Error: `{ex}`")

# ── CALLBACK QUERY HANDLER ────────────────────────────────────────────────────
@bot.on(events.CallbackQuery)
async def _(e):
    if e.sender_id != OWNER_ID:
        return await e.answer("Access denied.", alert=True)

    data = e.data.decode()

    # ── Account switch ──
    if data.startswith("acct_"):
        if data == "acct_help":
            await e.answer()
            return await bot.send_message(
                OWNER_ID,
                "To add account:\n`/add_account <name> <api_key> <owner_id>`"
            )
        name     = data[5:]
        accounts = load_accounts()
        if name not in accounts:
            return await e.answer("Account not found.", alert=True)
        acct = accounts[name]
        active_account["name"]     = name
        active_account["api_key"]  = acct["api_key"]
        active_account["owner_id"] = acct["owner_id"]
        await e.answer(f"Switched to {name}!", alert=False)
        active = name
        await e.edit(
            f"**Accounts**\nActive: `{name}`\nSelect to activate:",
            buttons=accounts_keyboard(accounts, active)
        )
        return

    # ── manage from list ──
    if data.startswith("manage_btn_"):
        sid  = data[11:]
        svcs = load_services()
        svc  = svcs.get(sid, {})
        name = svc.get("name", sid)
        url  = svc.get("url", "—")
        mid  = svc.get("monitor_id", "—")
        await e.edit(
            f"**Managing: {name}**\n"
            f"ID: `{sid}`\n"
            f"URL: `{url}`\n"
            f"Monitor: `{mid}`",
            buttons=manage_keyboard(sid)
        )
        return

    # ── Status ──
    if data.startswith("status_"):
        sid = data[7:]
        await e.answer("Fetching status...")
        svcs    = load_services()
        svc     = svcs.get(sid, {})
        api_key = svc.get("api_key") or active_account.get("api_key")
        try:
            status, resp = await r_get(f"/services/{sid}", api_key)
            if status == 200:
                s     = resp.get("service", resp) if isinstance(resp, dict) else {}
                sname = s.get("name", "—")
                susp  = s.get("suspended", "—")
                det   = s.get("serviceDetails", {})
                surl  = det.get("url", "—")
                icon  = "✅" if susp == "not_suspended" else "⏸️"
                await e.edit(
                    f"**Status: {sname}**\n\n"
                    f"State: {icon} `{susp}`\n"
                    f"URL: `{surl}`\n"
                    f"Region: `{det.get('region','—')}`\n"
                    f"Plan: `{det.get('plan','—')}`",
                    buttons=manage_keyboard(sid)
                )
            else:
                await e.edit(f"❌ Status failed ({status})\n`{extract_err(resp)}`", buttons=manage_keyboard(sid))
        except Exception as ex:
            await e.edit(f"❌ Error: `{ex}`", buttons=manage_keyboard(sid))
        return

    # ── Logs ──
    if data.startswith("logs_"):
        sid = data[5:]
        await e.answer("Fetching logs...")
        svcs    = load_services()
        svc     = svcs.get(sid, {})
        api_key = svc.get("api_key") or active_account.get("api_key")
        try:
            status, resp = await r_get(f"/services/{sid}/deploys?limit=5", api_key)
            if status == 200:
                deploys = resp if isinstance(resp, list) else []
                if not deploys:
                    return await e.edit("No deploys found.", buttons=manage_keyboard(sid))
                lines = []
                for item in deploys:
                    d     = item.get("deploy", item) if isinstance(item, dict) else {}
                    dst   = d.get("status", "—")
                    dtime = d.get("createdAt", "—")
                    did   = d.get("id", "—")
                    cmsg  = d.get("commit", {})
                    cmsg  = cmsg.get("message", "—") if isinstance(cmsg, dict) else "—"
                    icon  = "✅" if dst == "live" else ("❌" if dst == "failed" else "⏳")
                    lines.append(f"{icon} `{dst}` — `{str(dtime)[:16]}`\n   `{str(cmsg)[:60]}`")
                await e.edit(
                    f"**Deploy Logs ({len(lines)})**\n\n" + "\n\n".join(lines),
                    buttons=manage_keyboard(sid)
                )
            else:
                await e.edit(f"❌ Logs failed ({status})", buttons=manage_keyboard(sid))
        except Exception as ex:
            await e.edit(f"❌ Error: `{ex}`", buttons=manage_keyboard(sid))
        return

    # ── Add Env ──
    if data.startswith("env_"):
        sid = data[4:]
        await e.answer()
        waiting_state[e.sender_id] = {"action": "env", "service_id": sid}
        await e.edit(
            f"**Add Env Var**\nService: `{sid}`\n\n"
            f"Send in format:\n`KEY VALUE`\n\n"
            f"Example: `PORT 8080`\n\n"
            f"Or send /cancel to cancel.",
            buttons=[[Button.inline("❌ Cancel", f"cancel_wait")]]
        )
        return

    # ── Deploy ──
    if data.startswith("deploy_"):
        sid = data[7:]
        await e.answer("Triggering deploy...")
        svcs    = load_services()
        svc     = svcs.get(sid, {})
        api_key = svc.get("api_key") or active_account.get("api_key")
        try:
            status, resp = await r_post(f"/services/{sid}/deploys", {}, api_key)
            if status in (200, 201):
                d   = resp.get("deploy", resp) if isinstance(resp, dict) else {}
                did = d.get("id", "—")
                dst = d.get("status", "—")
                await e.edit(
                    f"✅ **Deploy Triggered!**\n\n"
                    f"Deploy ID: `{did}`\n"
                    f"Status: `{dst}`",
                    buttons=manage_keyboard(sid)
                )
            else:
                await e.edit(f"❌ Deploy failed ({status})\n`{extract_err(resp)}`", buttons=manage_keyboard(sid))
        except Exception as ex:
            await e.edit(f"❌ Error: `{ex}`", buttons=manage_keyboard(sid))
        return

    # ── Suspend ──
    if data.startswith("suspend_"):
        sid = data[8:]
        await e.answer("Suspending...")
        svcs    = load_services()
        svc     = svcs.get(sid, {})
        api_key = svc.get("api_key") or active_account.get("api_key")
        mid     = svc.get("monitor_id")
        try:
            status, resp = await r_post(f"/services/{sid}/suspend", {}, api_key)
            if status in (200, 204):
                if mid: await ut_edit_monitor(mid, 0)
                await e.edit(
                    f"⏸️ **Suspended**\nService: `{sid}`\n"
                    f"UptimeRobot: {'paused' if mid else 'no monitor'}",
                    buttons=manage_keyboard(sid)
                )
            else:
                await e.edit(f"❌ Suspend failed ({status})\n`{extract_err(resp)}`", buttons=manage_keyboard(sid))
        except Exception as ex:
            await e.edit(f"❌ Error: `{ex}`", buttons=manage_keyboard(sid))
        return

    # ── Resume ──
    if data.startswith("resume_"):
        sid = data[7:]
        await e.answer("Resuming...")
        svcs    = load_services()
        svc     = svcs.get(sid, {})
        api_key = svc.get("api_key") or active_account.get("api_key")
        mid     = svc.get("monitor_id")
        try:
            status, resp = await r_post(f"/services/{sid}/resume", {}, api_key)
            if status in (200, 204):
                if mid: await ut_edit_monitor(mid, 1)
                await e.edit(
                    f"✅ **Resumed**\nService: `{sid}`\n"
                    f"UptimeRobot: {'resumed' if mid else 'no monitor'}",
                    buttons=manage_keyboard(sid)
                )
            elif status == 400:
                st2, r2 = await r_post(f"/services/{sid}/deploys", {}, api_key)
                if st2 in (200, 201):
                    if mid: await ut_edit_monitor(mid, 1)
                    await e.edit(
                        f"✅ Deploy triggered (service was auto-suspended)\n`{sid}`",
                        buttons=manage_keyboard(sid)
                    )
                else:
                    await e.edit(f"❌ Resume + Deploy both failed.", buttons=manage_keyboard(sid))
            else:
                await e.edit(f"❌ Resume failed ({status})\n`{extract_err(resp)}`", buttons=manage_keyboard(sid))
        except Exception as ex:
            await e.edit(f"❌ Error: `{ex}`", buttons=manage_keyboard(sid))
        return

    # ── Custom Sleep ──
    if data.startswith("sleep_"):
        sid = data[6:]
        await e.answer()
        waiting_state[e.sender_id] = {"action": "sleep", "service_id": sid}
        await e.edit(
            f"**Custom Sleep**\nService: `{sid}`\n\n"
            f"Enter duration:\n"
            f"• `2h` = 2 hours\n"
            f"• `30m` = 30 minutes\n"
            f"• `3600s` = 3600 seconds\n\n"
            f"Send the duration now:",
            buttons=[[Button.inline("❌ Cancel", "cancel_wait")]]
        )
        return

    # ── Delete ──
    if data.startswith("delete_"):
        sid = data[7:]
        await e.answer()
        await e.edit(
            f"⚠️ **Confirm Delete**\nService: `{sid}`\n\nThis cannot be undone!",
            buttons=[
                [Button.inline("✅ Yes, Delete", f"confirm_del_{sid}"),
                 Button.inline("❌ Cancel",       f"manage_btn_{sid}")]
            ]
        )
        return

    if data.startswith("confirm_del_"):
        sid = data[12:]
        await e.answer("Deleting...")
        svcs    = load_services()
        svc     = svcs.get(sid, {})
        api_key = svc.get("api_key") or active_account.get("api_key")
        mid     = svc.get("monitor_id")
        try:
            status, resp = await r_delete(f"/services/{sid}", api_key)
            if status in (200, 204):
                if mid: await ut_delete_monitor(mid)
                if sid in svcs:
                    del svcs[sid]
                    save_services(svcs)
                await e.edit(f"🗑️ **Deleted**\nService `{sid}` removed.\nUptimeRobot monitor deleted.")
            else:
                await e.edit(f"❌ Delete failed ({status})\n`{extract_err(resp)}`")
        except Exception as ex:
            await e.edit(f"❌ Error: `{ex}`")
        return

    if data == "cancel_wait":
        waiting_state.pop(e.sender_id, None)
        await e.edit("❌ Cancelled.")
        return

# ── TEXT STATE HANDLER (env / sleep input) ────────────────────────────────────
@bot.on(events.NewMessage(func=lambda e: e.sender_id == OWNER_ID and bool(e.text) and not e.text.startswith("/")))
async def _(e):
    uid  = e.sender_id
    text = e.text.strip()

    if uid not in waiting_state:
        return

    ws     = waiting_state.pop(uid)
    action = ws.get("action")
    sid    = ws.get("service_id")

    svcs    = load_services()
    svc     = svcs.get(sid, {})
    api_key = svc.get("api_key") or active_account.get("api_key")
    mid     = svc.get("monitor_id")

    # ── ENV input ──
    if action == "env":
        parts = text.split(None, 1)
        if len(parts) < 2:
            return await e.reply("❌ Invalid format. Use `KEY VALUE`\nTry again: press Add Env button.")
        key, value = parts[0], parts[1]
        msg = await e.reply(f"⏳ Fetching existing env vars for `{sid}`...")
        try:
            status, data = await r_get(f"/services/{sid}/env-vars", api_key)
            if status != 200:
                return await msg.edit(f"❌ Fetch failed ({status})\n`{extract_err(data)}`")
            existing = data if isinstance(data, list) else []
            env_list = [{"key": i.get("key",""), "value": i.get("value","")} for i in existing]
            updated  = False
            for item in env_list:
                if item["key"] == key:
                    item["value"] = value; updated = True; break
            if not updated:
                env_list.append({"key": key, "value": value})
            put_st, put_data = await r_put(f"/services/{sid}/env-vars", env_list, api_key)
            if put_st in (200, 201):
                action_word = "Updated" if updated else "Added"
                await msg.edit(
                    f"✅ **Env Var {action_word}!**\n\n"
                    f"`{key}` = `{value}`\n"
                    f"Total vars: `{len(env_list)}`",
                    buttons=manage_keyboard(sid)
                )
            else:
                await msg.edit(f"❌ Update failed ({put_st})\n`{extract_err(put_data)}`", buttons=manage_keyboard(sid))
        except Exception as ex:
            await msg.edit(f"❌ Error: `{ex}`")
        return

    # ── SLEEP input ──
    if action == "sleep":
        seconds, label = parse_duration(text)
        if not seconds:
            return await e.reply(
                "❌ Invalid format.\nUse `2h`, `30m`, or `60s`\n\nPress Custom Sleep button again."
            )
        msg = await e.reply(f"⏳ Suspending service and pausing monitor for `{label}`...")
        try:
            st, _ = await r_post(f"/services/{sid}/suspend", {}, api_key)
            if st in (200, 204):
                if mid: await ut_edit_monitor(mid, 0)
                asyncio.create_task(sleep_and_wake(sid, mid, seconds, api_key, label))
                await msg.edit(
                    f"⏸️ **Sleeping for {label}**\n\n"
                    f"Service: `{sid}`\n"
                    f"UptimeRobot: {'paused' if mid else 'no monitor'}\n\n"
                    f"Will auto-wake after `{label}` 🔔",
                    buttons=manage_keyboard(sid)
                )
            else:
                await msg.edit(f"❌ Suspend failed ({st})", buttons=manage_keyboard(sid))
        except Exception as ex:
            await msg.edit(f"❌ Error: `{ex}`")
        return

# ── /cancel ───────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="^/cancel$"))
async def _(e):
    if not owner(e): return
    if e.sender_id in waiting_state:
        waiting_state.pop(e.sender_id)
        await e.reply("❌ Cancelled.")
    else:
        await e.reply("Nothing to cancel.")

# ── BOOTSTRAP ─────────────────────────────────────────────────────────────────
async def main():
    threading.Thread(target=run_server, daemon=True).start()

    accounts = load_accounts()
    if accounts:
        first = next(iter(accounts))
        active_account["name"]     = first
        active_account["api_key"]  = accounts[first]["api_key"]
        active_account["owner_id"] = accounts[first]["owner_id"]
        print(f"[+] Auto-loaded account: {first}")

    await bot.start(bot_token=BOT_TOKEN)
    print("[+] Render Smart Manager Bot online.")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
