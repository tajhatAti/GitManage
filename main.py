import os, asyncio, threading, json, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from telethon import TelegramClient, events, Button
from telethon.errors import AlreadyInConversationError
import aiohttp

# ── ENV ───────────────────────────────────────────────────────────────────────
API_ID           = int(os.environ.get("API_ID", 0))
API_HASH         = os.environ.get("API_HASH", "")
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
OWNER_ID         = int(os.environ.get("OWNER_ID", 0))
DEFAULT_API_KEY  = os.environ.get("RENDER_API_KEY", "")
DEFAULT_OWNER_ID = os.environ.get("RENDER_OWNER_ID", "")
UPTIME_KEY       = os.environ.get("UPTIME_API_KEY", "")

# Env vars to auto-inject into every new service
AUTO_INJECT_ENVS = {
    "INJECT_KEY_1": os.environ.get("INJECT_KEY_1", ""),
    "INJECT_KEY_2": os.environ.get("INJECT_KEY_2", ""),
}

ACCOUNTS_FILE = "accounts.json"
SERVICES_FILE = "services.json"
RENDER_BASE   = "https://api.render.com/v1"
UPTIME_BASE   = "https://api.uptimerobot.com/v2"
CONV_TIMEOUT  = 120

bot = TelegramClient("render_v3_bot", API_ID, API_HASH)

# ── ACTIVE ACCOUNT STATE ──────────────────────────────────────────────────────
active = {
    "name":     "default",
    "api_key":  DEFAULT_API_KEY,
    "owner_id": DEFAULT_OWNER_ID
}

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

# ── JSON HELPERS ──────────────────────────────────────────────────────────────
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

# ── CURRENT KEY ───────────────────────────────────────────────────────────────
def current_key() -> str:
    return active.get("api_key") or DEFAULT_API_KEY

def current_owner_id() -> str:
    return active.get("owner_id") or DEFAULT_OWNER_ID

# ── RENDER HEADERS ────────────────────────────────────────────────────────────
def rh(api_key: str = None) -> dict:
    return {
        "Authorization": f"Bearer {api_key or current_key()}",
        "Accept":        "application/json",
        "Content-Type":  "application/json"
    }

def extract_err(data) -> str:
    if isinstance(data, dict):
        return data.get("message", json.dumps(data))[:300]
    return str(data)[:300]

# ── RENDER HTTP ───────────────────────────────────────────────────────────────
async def r_get(path: str, api_key: str = None):
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{RENDER_BASE}{path}", headers=rh(api_key),
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            try: data = await r.json(content_type=None)
            except: data = await r.text()
            return r.status, data

async def r_post(path: str, payload: dict = {}, api_key: str = None):
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{RENDER_BASE}{path}", headers=rh(api_key), json=payload,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            try: data = await r.json(content_type=None)
            except: data = await r.text()
            return r.status, data

async def r_put(path: str, payload, api_key: str = None):
    async with aiohttp.ClientSession() as s:
        async with s.put(
            f"{RENDER_BASE}{path}", headers=rh(api_key), json=payload,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            try: data = await r.json(content_type=None)
            except: data = await r.text()
            return r.status, data

async def r_delete(path: str, api_key: str = None):
    async with aiohttp.ClientSession() as s:
        async with s.delete(
            f"{RENDER_BASE}{path}", headers=rh(api_key),
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            try: data = await r.json(content_type=None)
            except: data = await r.text()
            return r.status, data

# ── UPTIMEROBOT ───────────────────────────────────────────────────────────────
async def ut_new_monitor(name: str, url: str) -> str | None:
    if not UPTIME_KEY: return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{UPTIME_BASE}/newMonitor",
                data={"api_key": UPTIME_KEY, "format": "json", "type": 1,
                      "url": url, "friendly_name": name},
                timeout=aiohttp.ClientTimeout(total=20)
            ) as r:
                data = await r.json(content_type=None)
        if data.get("stat") == "ok":
            return str(data.get("monitor", {}).get("id", ""))
    except: pass
    return None

async def ut_edit_monitor(monitor_id: str, status: int):
    if not UPTIME_KEY or not monitor_id: return
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"{UPTIME_BASE}/editMonitor",
                data={"api_key": UPTIME_KEY, "format": "json",
                      "id": monitor_id, "status": status},
                timeout=aiohttp.ClientTimeout(total=20)
            )
    except: pass

async def ut_delete_monitor(monitor_id: str):
    if not UPTIME_KEY or not monitor_id: return
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(
                f"{UPTIME_BASE}/deleteMonitor",
                data={"api_key": UPTIME_KEY, "format": "json", "id": monitor_id},
                timeout=aiohttp.ClientTimeout(total=20)
            )
    except: pass

# ── SERVICE ALIAS RESOLVER ────────────────────────────────────────────────────
async def resolve_service(identifier: str) -> tuple[str | None, str | None]:
    """Returns (service_id, service_name) or (None, None)"""
    identifier = identifier.strip()
    svcs = load_services()

    # Check if it's a saved name
    if identifier in svcs:
        sid  = svcs[identifier]["id"]
        return sid, identifier

    # Check if any service has this id saved
    for name, val in svcs.items():
        if isinstance(val, dict) and val.get("id") == identifier:
            return identifier, name

    # Try fetching from Render API directly
    if identifier.startswith("srv-"):
        status, data = await r_get(f"/services/{identifier}")
        if status == 200:
            svc   = data.get("service", data) if isinstance(data, dict) else {}
            sname = svc.get("name", identifier)
            svcs[sname] = {"id": identifier, "monitor_id": svcs.get(sname, {}).get("monitor_id")}
            save_services(svcs)
            return identifier, sname

    return None, None

# ── MANAGE KEYBOARD ───────────────────────────────────────────────────────────
def manage_kb(sid: str):
    return [
        [Button.inline("📊 Status",       f"st_{sid}"),
         Button.inline("📜 Logs",         f"lg_{sid}")],
        [Button.inline("➕ Add Env",      f"ev_{sid}"),
         Button.inline("🚀 Deploy",       f"dp_{sid}")],
        [Button.inline("⏸️ Suspend",      f"sp_{sid}"),
         Button.inline("▶️ Resume",       f"rs_{sid}")],
        [Button.inline("⏱️ Custom Sleep", f"sl_{sid}")],
        [Button.inline("🗑️ Delete",       f"dl_{sid}")]
    ]

def accounts_kb(accounts: dict, active_name: str):
    buttons = []
    for name, data in accounts.items():
        icon = "✅ " if name == active_name else ""
        label = f"{icon}{name} ({data.get('render_name','—')})"
        buttons.append([Button.inline(label, f"sw_{name}")])
    if DEFAULT_API_KEY:
        icon = "✅ " if active_name == "default" else ""
        buttons.append([Button.inline(f"{icon}default (env)", "sw_default")])
    buttons.append([Button.inline("➕ Add Account", "add_acct_btn")])
    return buttons

# ── DURATION PARSER ───────────────────────────────────────────────────────────
def parse_duration(text: str) -> tuple[int | None, str | None]:
    m = re.match(r'^(\d+)(h|m|s)$', text.strip().lower())
    if not m: return None, None
    val, unit = int(m.group(1)), m.group(2)
    secs  = val * (3600 if unit=='h' else 60 if unit=='m' else 1)
    label = f"{val} {'hour(s)' if unit=='h' else 'minute(s)' if unit=='m' else 'second(s)'}"
    return secs, label

# ── SLEEP TASK ────────────────────────────────────────────────────────────────
async def sleep_and_wake(sid: str, monitor_id: str, seconds: int, api_key: str, label: str):
    await bot.send_message(OWNER_ID,
        f"⏸️ **Sleeping for {label}**\nService: `{sid}`\nWill auto-wake.")
    await asyncio.sleep(seconds)
    await r_post(f"/services/{sid}/resume", api_key=api_key)
    if monitor_id: await ut_edit_monitor(monitor_id, 1)
    await bot.send_message(OWNER_ID,
        f"✅ **Woke Up!**\nService: `{sid}`\nMonitor resumed.")

# ── OWNER CHECK ───────────────────────────────────────────────────────────────
def own(e): return e.sender_id == OWNER_ID

# ── /start ────────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="^/start$"))
async def _(e):
    if not own(e): return
    acct = active.get("name","default")
    await e.reply(
        "**Render Smart Manager v3**\n\n"
        f"Active: `{acct}`\n\n"
        "`/create` — create web service\n"
        "`/deploy` — deploy service\n"
        "`/status` — service status\n"
        "`/manage <name_or_id>` — manage panel\n"
        "`/services` — list saved services\n"
        "`/list_render` — fetch from Render\n"
        "`/accounts` — manage accounts\n"
        "`/add_account` — add new account\n"
        "`/whoami` — current account info"
    )

# ── /accounts ─────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="^/accounts$"))
async def _(e):
    if not own(e): return
    accounts = load_accounts()
    if not accounts and not DEFAULT_API_KEY:
        return await e.reply("No accounts. Use `/add_account` or set `RENDER_API_KEY` env var.")
    await e.reply(
        "**Accounts**\nTap to switch:",
        buttons=accounts_kb(accounts, active.get("name","default"))
    )

# ── /add_account (conversational) ────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="^/add_account$"))
async def _(e):
    if not own(e): return
    async with bot.conversation(e.chat_id, timeout=CONV_TIMEOUT) as conv:
        try:
            await conv.send_message("**Add Account**\n\nSend your **Render API Key**:")
            r1 = await conv.get_response()
            api_key_input = r1.text.strip()
            if not api_key_input:
                return await conv.send_message("❌ Cancelled.")

            check_msg = await conv.send_message("⏳ Verifying API key and fetching owners...")

            # Test the key and get owners
            test_status, test_data = await r_get("/owners?limit=10", api_key=api_key_input)

            if test_status == 401:
                await bot.edit_message(e.chat_id, check_msg.id,
                    "❌ **Invalid API Key** (401 Unauthorized)\nCheck and try again.")
                return

            if test_status != 200:
                await bot.edit_message(e.chat_id, check_msg.id,
                    f"❌ API returned {test_status}\n`{extract_err(test_data)}`")
                return

            owners_list = test_data if isinstance(test_data, list) else []
            if not owners_list:
                await bot.edit_message(e.chat_id, check_msg.id, "❌ No owners found.")
                return

            # Build inline buttons for owner selection
            buttons = []
            for item in owners_list:
                o     = item.get("owner", item)
                oid   = o.get("id", "")
                oname = o.get("name", "—")
                otype = o.get("type", "—")
                buttons.append([Button.inline(
                    f"{oname} ({otype})",
                    f"sel_owner_{oid}|||{api_key_input}"
                )])

            await bot.edit_message(e.chat_id, check_msg.id,
                "✅ **API Key Valid!**\n\nSelect the **Owner** for this account:",
                buttons=buttons
            )

        except asyncio.TimeoutError:
            await e.reply("⏰ Timeout. Use `/add_account` again.")
# ── /create (conversational) ──────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="^/create$"))
async def _(e):
    if not own(e): return
    if not current_key():
        return await e.reply("❌ No API key. Use `/add_account` or set `RENDER_API_KEY`.")

    async with bot.conversation(e.chat_id, timeout=CONV_TIMEOUT) as conv:
        try:
            await conv.send_message("**Create Web Service**\n\nSend the **service name**:")
            r1   = await conv.get_response()
            name = r1.text.strip()
            if not name:
                return await conv.send_message("❌ Cancelled.")

            await conv.send_message(f"Name: `{name}`\n\nSend the **GitHub repo URL**:")
            r2   = await conv.get_response()
            repo = r2.text.strip()
            if not repo:
                return await conv.send_message("❌ Cancelled.")

            prog = await conv.send_message(f"⏳ Creating `{name}` on Render...")

            rid = current_owner_id()
            if not rid:
                await bot.edit_message(e.chat_id, prog.id,
                    "❌ No Render Owner ID. Set `RENDER_OWNER_ID` env or add an account.")
                return

            payload = {
                "type": "web_service", "name": name,
                "ownerId": rid, "repo": repo,
                "autoDeploy": "yes", "branch": "main",
                "serviceDetails": {
                    "env": "python", "region": "singapore",
                    "plan": "free",
                    "buildCommand": "pip install -r requirements.txt",
                    "startCommand": "python main.py"
                }
            }
            st, data = await r_post("/services", payload)

            if st != 201:
                await bot.edit_message(e.chat_id, prog.id,
                    f"❌ Create failed ({st})\n`{extract_err(data)}`")
                return

            svc   = data.get("service", data) if isinstance(data, dict) else {}
            sid   = svc.get("id", "—")
            sname = svc.get("name", name)
            det   = svc.get("serviceDetails", {})
            surl  = det.get("url", "")

            await bot.edit_message(e.chat_id, prog.id,
                f"✅ Service created!\n⏳ Injecting env vars...")

            # Auto-inject envs
            env_list = [{"key": k, "value": v}
                        for k, v in AUTO_INJECT_ENVS.items() if v]
            if env_list:
                await r_put(f"/services/{sid}/env-vars", env_list)

            # UptimeRobot
            monitor_id = None
            ut_txt     = "UptimeRobot: no URL yet"
            if surl:
                await bot.edit_message(e.chat_id, prog.id,
                    f"⏳ Setting up UptimeRobot for `{surl}`...")
                monitor_id = await ut_new_monitor(sname, surl)
                ut_txt = f"Monitor ID: `{monitor_id}`" if monitor_id else "UptimeRobot: failed"

            # Save alias
            svcs = load_services()
            svcs[sname] = {"id": sid, "monitor_id": monitor_id,
                           "url": surl, "account": active.get("name","default"),
                           "api_key": current_key()}
            save_services(svcs)

            await bot.edit_message(e.chat_id, prog.id,
                f"🎉 **Service Ready!**\n\n"
                f"Name: `{sname}`\n"
                f"ID: `{sid}`\n"
                f"URL: `{surl or 'Pending...'}`\n"
                f"Env vars injected: `{len(env_list)}`\n"
                f"{ut_txt}",
                buttons=manage_kb(sid)
            )

        except asyncio.TimeoutError:
            await e.reply("⏰ Timeout. Use `/create` again.")

# ── /deploy (conversational) ──────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="^/deploy$"))
async def _(e):
    if not own(e): return
    async with bot.conversation(e.chat_id, timeout=CONV_TIMEOUT) as conv:
        try:
            await conv.send_message("Send **service name or ID** to deploy:")
            r    = await conv.get_response()
            inp  = r.text.strip()
            sid, sname = await resolve_service(inp)
            if not sid:
                return await conv.send_message(f"❌ Could not resolve `{inp}`.")
            svcs    = load_services()
            svc     = svcs.get(sname or "", {})
            api_key = svc.get("api_key") or current_key()
            msg     = await conv.send_message(f"⏳ Deploying `{sname or sid}`...")
            st, data = await r_post(f"/services/{sid}/deploys", {}, api_key)
            if st in (200, 201):
                d   = data.get("deploy", data) if isinstance(data, dict) else {}
                did = d.get("id","—"); dst = d.get("status","—")
                await bot.edit_message(e.chat_id, msg.id,
                    f"✅ **Deploy Triggered!**\n\nID: `{did}`\nStatus: `{dst}`",
                    buttons=manage_kb(sid))
            else:
                await bot.edit_message(e.chat_id, msg.id,
                    f"❌ Deploy failed ({st})\n`{extract_err(data)}`")
        except asyncio.TimeoutError:
            await e.reply("⏰ Timeout.")

# ── /status (conversational) ──────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="^/status$"))
async def _(e):
    if not own(e): return
    async with bot.conversation(e.chat_id, timeout=CONV_TIMEOUT) as conv:
        try:
            await conv.send_message("Send **service name or ID**:")
            r   = await conv.get_response()
            inp = r.text.strip()
            sid, sname = await resolve_service(inp)
            if not sid:
                return await conv.send_message(f"❌ Could not resolve `{inp}`.")
            svcs    = load_services()
            svc     = svcs.get(sname or "", {})
            api_key = svc.get("api_key") or current_key()
            st, data = await r_get(f"/services/{sid}", api_key)
            if st == 200:
                s    = data.get("service", data) if isinstance(data, dict) else {}
                susp = s.get("suspended","—")
                det  = s.get("serviceDetails",{})
                icon = "✅" if susp=="not_suspended" else "⏸️"
                await conv.send_message(
                    f"**Status: {sname or sid}**\n\n"
                    f"State: {icon} `{susp}`\n"
                    f"URL: `{det.get('url','—')}`\n"
                    f"Region: `{det.get('region','—')}`\n"
                    f"Plan: `{det.get('plan','—')}`",
                    buttons=manage_kb(sid)
                )
            else:
                await conv.send_message(f"❌ Failed ({st})\n`{extract_err(data)}`")
        except asyncio.TimeoutError:
            await e.reply("⏰ Timeout.")

# ── /manage ───────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern=r"^/manage(?: (.+))?$"))
async def _(e):
    if not own(e): return
    inp = (e.pattern_match.group(1) or "").strip()

    if not inp:
        async with bot.conversation(e.chat_id, timeout=CONV_TIMEOUT) as conv:
            try:
                await conv.send_message("Send **service name or ID**:")
                r   = await conv.get_response()
                inp = r.text.strip()
            except asyncio.TimeoutError:
                return await e.reply("⏰ Timeout.")

    sid, sname = await resolve_service(inp)
    if not sid:
        return await e.reply(f"❌ Could not resolve `{inp}`.")

    svcs = load_services()
    svc  = svcs.get(sname or "", {})
    url  = svc.get("url","—"); mid = svc.get("monitor_id","—")
    await e.reply(
        f"**Managing: {sname or sid}**\n"
        f"ID: `{sid}`\nURL: `{url}`\nMonitor: `{mid}`",
        buttons=manage_kb(sid)
    )

# ── /services ─────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="^/services$"))
async def _(e):
    if not own(e): return
    svcs = load_services()
    if not svcs:
        return await e.reply("No saved services. Use `/create` to add one.")
    buttons = []
    for name, val in svcs.items():
        sid = val.get("id","—") if isinstance(val, dict) else str(val)
        buttons.append([Button.inline(f"⚙️ {name}", f"mg_{sid}")])
    await e.reply("**Saved Services**\nTap to manage:", buttons=buttons)

# ── /list_render ──────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="^/list_render$"))
async def _(e):
    if not own(e): return
    if not current_key(): return await e.reply("❌ No API key.")
    msg = await e.reply("⏳ Fetching from Render...")
    path = "/services?limit=20"
    if current_owner_id(): path += f"&ownerId={current_owner_id()}"
    st, data = await r_get(path)
    if st == 200:
        services = data if isinstance(data, list) else []
        if not services: return await msg.edit("No services found on Render.")
        buttons = []
        for item in services:
            svc   = item.get("service", item) if isinstance(item, dict) else {}
            sname = svc.get("name","—"); sid = svc.get("id","—")
            susp  = svc.get("suspended","not_suspended")
            icon  = "✅" if susp == "not_suspended" else "⏸️"
            buttons.append([Button.inline(f"{icon} {sname}", f"mg_{sid}")])
        await msg.edit(f"**Render Services ({len(services)})**", buttons=buttons)
    else:
        await msg.edit(f"❌ Failed ({st})\n`{extract_err(data)}`")

# ── /whoami ───────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="^/whoami$"))
async def _(e):
    if not own(e): return
    if not current_key(): return await e.reply("❌ No API key.")
    msg = await e.reply("⏳ Fetching...")
    st, data = await r_get("/owners?limit=10")
    if st == 200:
        owners = data if isinstance(data, list) else []
        lines  = []
        for item in owners:
            o = item.get("owner", item)
            lines.append(f"• **{o.get('name','—')}** (`{o.get('type','—')}`)\n  ID: `{o.get('id','—')}`")
        await msg.edit(
            f"**Active Account:** `{active.get('name','default')}`\n\n"
            "**Render Owner(s):**\n\n" + "\n\n".join(lines)
        )
    else:
        await msg.edit(f"❌ Failed ({st})\n`{extract_err(data)}`")

# ── CALLBACK QUERY ────────────────────────────────────────────────────────────
@bot.on(events.CallbackQuery)
async def _(e):
    if e.sender_id != OWNER_ID:
        return await e.answer("Access denied.", alert=True)

    data = e.data.decode()

    # ── quick manage from list ──
    if data.startswith("mg_"):
        sid = data[3:]
        sid_resolved, sname = await resolve_service(sid)
        if not sid_resolved: return await e.answer("Not found.", alert=True)
        svcs = load_services()
        svc  = svcs.get(sname or "", {})
        await e.edit(
            f"**Managing: {sname or sid}**\n"
            f"ID: `{sid_resolved}`\n"
            f"URL: `{svc.get('url','—')}`\n"
            f"Monitor: `{svc.get('monitor_id','—')}`",
            buttons=manage_kb(sid_resolved)
        )
        return

    # ── account switch ──
    if data.startswith("sw_"):
        name = data[3:]
        if name == "default":
            if not DEFAULT_API_KEY:
                return await e.answer("No default key in env.", alert=True)
            active["name"]     = "default"
            active["api_key"]  = DEFAULT_API_KEY
            active["owner_id"] = DEFAULT_OWNER_ID
        else:
            accounts = load_accounts()
            if name not in accounts:
                return await e.answer("Account not found.", alert=True)
            acct = accounts[name]
            active["name"]     = name
            active["api_key"]  = acct["api_key"]
            active["owner_id"] = acct["owner_id"]
        await e.answer(f"Switched to {name}!")
        await e.edit(
            f"**Accounts**\nActive: `{active['name']}`",
            buttons=accounts_kb(load_accounts(), active["name"])
        )
        return

    if data == "add_acct_btn":
        await e.answer()
        await bot.send_message(OWNER_ID, "Use `/add_account` command to add a new account.")
        return

    # ── resolve service id from callback ──
    prefixes = {"st_":8, "lg_":8, "ev_":8, "dp_":8, "sp_":8, "rs_":8, "sl_":8, "dl_":8}
    sid = None
    for pfx in ["st_","lg_","ev_","dp_","sp_","rs_","sl_","dl_","cdl_"]:
        if data.startswith(pfx):
            sid = data[len(pfx):]
            break

    if not sid: return

    svcs    = load_services()
    svc_key = next((k for k,v in svcs.items() if isinstance(v,dict) and v.get("id")==sid), None)
    svc     = svcs.get(svc_key, {})
    api_key = svc.get("api_key") or current_key()
    mid     = svc.get("monitor_id")

    # Status
    if data.startswith("st_"):
        await e.answer("Fetching...")
        st, resp = await r_get(f"/services/{sid}", api_key)
        if st == 200:
            s    = resp.get("service", resp) if isinstance(resp, dict) else {}
            susp = s.get("suspended","—")
            det  = s.get("serviceDetails",{})
            icon = "✅" if susp=="not_suspended" else "⏸️"
            await e.edit(
                f"**Status**\n\nState: {icon} `{susp}`\n"
                f"URL: `{det.get('url','—')}`\n"
                f"Region: `{det.get('region','—')}`\n"
                f"Plan: `{det.get('plan','—')}`",
                buttons=manage_kb(sid)
            )
        else:
            await e.edit(f"❌ ({st})\n`{extract_err(resp)}`", buttons=manage_kb(sid))
        return

    # Logs
    if data.startswith("lg_"):
        await e.answer("Fetching logs...")
        st, resp = await r_get(f"/services/{sid}/deploys?limit=5", api_key)
        if st == 200:
            deploys = resp if isinstance(resp, list) else []
            lines   = []
            for item in deploys:
                d    = item.get("deploy", item) if isinstance(item, dict) else {}
                dst  = d.get("status","—"); dtime = d.get("createdAt","—")
                cmit = d.get("commit",{}); cm = cmit.get("message","—") if isinstance(cmit,dict) else "—"
                icon = "✅" if dst=="live" else ("❌" if dst=="failed" else "⏳")
                lines.append(f"{icon} `{dst}` `{str(dtime)[:16]}`\n`{str(cm)[:60]}`")
            txt = "\n\n".join(lines) if lines else "No deploys found."
            await e.edit(f"**Deploy Logs**\n\n{txt}", buttons=manage_kb(sid))
        else:
            await e.edit(f"❌ ({st})\n`{extract_err(resp)}`", buttons=manage_kb(sid))
        return

    # Add Env
    if data.startswith("ev_"):
        await e.answer()
        await e.edit(
            f"**Add Env Var** — `{sid}`\n\n"
            "Reply with: `KEY VALUE`\n\nExample: `PORT 8080`",
            buttons=[[Button.inline("❌ Cancel", f"mg_{sid}")]]
        )
        try:
            async with bot.conversation(OWNER_ID, timeout=CONV_TIMEOUT) as conv:
                r = await conv.get_response()
                if r.text.strip().lower() == "/cancel":
                    await bot.send_message(OWNER_ID, "Cancelled.")
                    return
                parts = r.text.strip().split(None, 1)
                if len(parts) < 2:
                    return await bot.send_message(OWNER_ID, "❌ Format: `KEY VALUE`")
                key, val = parts[0], parts[1]
                st, envs = await r_get(f"/services/{sid}/env-vars", api_key)
                existing = envs if isinstance(envs, list) else []
                env_list = [{"key": i.get("key",""), "value": i.get("value","")} for i in existing]
                updated  = False
                for item in env_list:
                    if item["key"] == key:
                        item["value"] = val; updated = True; break
                if not updated: env_list.append({"key": key, "value": val})
                put_st, _ = await r_put(f"/services/{sid}/env-vars", env_list, api_key)
                word = "Updated" if updated else "Added"
                await bot.send_message(
                    OWNER_ID,
                    f"✅ **{word}:** `{key}` = `{val}`\nTotal: `{len(env_list)}`",
                    buttons=manage_kb(sid)
                )
        except asyncio.TimeoutError:
            await bot.send_message(OWNER_ID, "⏰ Timeout.")
        return

    # Deploy
    if data.startswith("dp_"):
        await e.answer("Deploying...")
        st, resp = await r_post(f"/services/{sid}/deploys", {}, api_key)
        if st in (200, 201):
            d = resp.get("deploy", resp) if isinstance(resp, dict) else {}
            await e.edit(
                f"✅ **Deploy Triggered!**\nID: `{d.get('id','—')}`\nStatus: `{d.get('status','—')}`",
                buttons=manage_kb(sid)
            )
        else:
            await e.edit(f"❌ Deploy failed ({st})\n`{extract_err(resp)}`", buttons=manage_kb(sid))
        return

    # Suspend
    if data.startswith("sp_"):
        await e.answer("Suspending...")
        st, resp = await r_post(f"/services/{sid}/suspend", {}, api_key)
        if st in (200, 204):
            if mid: await ut_edit_monitor(mid, 0)
            await e.edit(
                f"⏸️ **Suspended**\n`{sid}`\nMonitor: {'paused' if mid else 'n/a'}",
                buttons=manage_kb(sid)
            )
        else:
            await e.edit(f"❌ Suspend failed ({st})\n`{extract_err(resp)}`", buttons=manage_kb(sid))
        return

    # Resume
    if data.startswith("rs_"):
        await e.answer("Resuming...")
        st, resp = await r_post(f"/services/{sid}/resume", {}, api_key)
        if st in (200, 204):
            if mid: await ut_edit_monitor(mid, 1)
            await e.edit(
                f"✅ **Resumed**\n`{sid}`\nMonitor: {'resumed' if mid else 'n/a'}",
                buttons=manage_kb(sid)
            )
        elif st == 400:
            st2, r2 = await r_post(f"/services/{sid}/deploys", {}, api_key)
            if st2 in (200, 201):
                if mid: await ut_edit_monitor(mid, 1)
                await e.edit(f"✅ Deploy triggered (was auto-suspended)\n`{sid}`", buttons=manage_kb(sid))
            else:
                await e.edit("❌ Resume + Deploy both failed.", buttons=manage_kb(sid))
        else:
            await e.edit(f"❌ Resume failed ({st})\n`{extract_err(resp)}`", buttons=manage_kb(sid))
        return

    # Custom Sleep
    if data.startswith("sl_"):
        await e.answer()
        await e.edit(
            f"**Custom Sleep** — `{sid}`\n\n"
            "Send duration:\n• `2h` = 2 hours\n• `30m` = 30 minutes\n• `60s` = 60 seconds",
            buttons=[[Button.inline("❌ Cancel", f"mg_{sid}")]]
        )
        try:
            async with bot.conversation(OWNER_ID, timeout=CONV_TIMEOUT) as conv:
                r = await conv.get_response()
                secs, label = parse_duration(r.text.strip())
                if not secs:
                    return await bot.send_message(OWNER_ID, "❌ Invalid format. Use `2h`, `30m`, or `60s`.")
                st, _ = await r_post(f"/services/{sid}/suspend", {}, api_key)
                if st in (200, 204):
                    if mid: await ut_edit_monitor(mid, 0)
                    asyncio.create_task(sleep_and_wake(sid, mid, secs, api_key, label))
                    await bot.send_message(
                        OWNER_ID,
                        f"⏸️ **Sleeping for {label}**\n`{sid}`\nWill auto-wake.",
                        buttons=manage_kb(sid)
                    )
                else:
                    await bot.send_message(OWNER_ID, f"❌ Suspend failed ({st})", buttons=manage_kb(sid))
        except asyncio.TimeoutError:
            await bot.send_message(OWNER_ID, "⏰ Timeout.")
        return

    # Delete
    if data.startswith("dl_"):
        await e.answer()
        await e.edit(
            f"⚠️ **Confirm Delete**\n`{sid}`\n\nThis cannot be undone!",
            buttons=[
                [Button.inline("✅ Yes, Delete", f"cdl_{sid}"),
                 Button.inline("❌ Cancel",       f"mg_{sid}")]
            ]
        )
        return

    if data.startswith("cdl_"):
        await e.answer("Deleting...")
        st, resp = await r_delete(f"/services/{sid}", api_key)
        if st in (200, 204):
            if mid: await ut_delete_monitor(mid)
            if svc_key and svc_key in svcs:
                del svcs[svc_key]; save_services(svcs)
            await e.edit(f"🗑️ **Deleted**\n`{sid}`\nMonitor removed.")
        else:
            await e.edit(f"❌ Delete failed ({st})\n`{extract_err(resp)}`")
        return

# ── BOOTSTRAP ─────────────────────────────────────────────────────────────────
async def main():
    threading.Thread(target=run_server, daemon=True).start()

    # Auto-load first saved account if no default env key
    if not DEFAULT_API_KEY:
        accounts = load_accounts()
        if accounts:
            first = next(iter(accounts))
            active["name"]     = first
            active["api_key"]  = accounts[first]["api_key"]
            active["owner_id"] = accounts[first]["owner_id"]
            print(f"[+] Auto-loaded account: {first}")

    await bot.start(bot_token=BOT_TOKEN)
    print("[+] Render Smart Manager v3 online.")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
