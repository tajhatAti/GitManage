import os, asyncio, threading, json
from http.server import HTTPServer, BaseHTTPRequestHandler
from telethon import TelegramClient, events
import aiohttp

API_ID          = int(os.environ.get("API_ID", 0))
API_HASH        = os.environ.get("API_HASH", "")
BOT_TOKEN       = os.environ.get("BOT_TOKEN", "")
OWNER_ID        = int(os.environ.get("OWNER_ID", 0))
RENDER_API_KEY  = os.environ.get("RENDER_API_KEY", "")
RENDER_OWNER_ID = os.environ.get("RENDER_OWNER_ID", "")

RENDER_BASE = "https://api.render.com/v1"

bot = TelegramClient("render_manager_bot", API_ID, API_HASH)

class _H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a): pass

def run_server():
    HTTPServer(('0.0.0.0', int(os.environ.get("PORT", 8080))), _H).serve_forever()

def render_headers():
    return {
        "Authorization": f"Bearer {RENDER_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

def owner(e):
    return e.sender_id == OWNER_ID

async def render_get(path: str):
    url = f"{RENDER_BASE}{path}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=render_headers(), timeout=aiohttp.ClientTimeout(total=30)) as r:
            return r.status, await r.json()

async def render_post(path: str, payload: dict):
    url = f"{RENDER_BASE}{path}"
    async with aiohttp.ClientSession() as s:
        async with s.post(url, headers=render_headers(), json=payload, timeout=aiohttp.ClientTimeout(total=30)) as r:
            return r.status, await r.json()

async def render_put(path: str, payload):
    url = f"{RENDER_BASE}{path}"
    async with aiohttp.ClientSession() as s:
        async with s.put(url, headers=render_headers(), json=payload, timeout=aiohttp.ClientTimeout(total=30)) as r:
            return r.status, await r.json()

# ── /start ────────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="^/start$"))
async def _(e):
    if not owner(e): return
    await e.reply(
        "**Render Deployment Manager**\n\n"
        "`/create_web <name> <github_url>` — নতুন web service তৈরি\n"
        "`/deploy <service_id>` — manual deploy trigger\n"
        "`/status <service_id>` — service status দেখো\n"
        "`/add_env <service_id> <KEY> <VALUE>` — env variable যোগ করো\n"
        "`/list_services` — সব service দেখো\n"
        "`/logs <service_id>` — latest deploy info\n"
        "`/suspend <service_id>` — service suspend\n"
        "`/resume <service_id>` — service resume\n"
        "`/delete <service_id>` — service delete"
    )

# ── /create_web ───────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern=r"^/create_web (\S+) (\S+)$"))
async def _(e):
    if not owner(e): return
    name     = e.pattern_match.group(1).strip()
    repo_url = e.pattern_match.group(2).strip()

    if not RENDER_OWNER_ID:
        return await e.reply("❌ `RENDER_OWNER_ID` environment variable সেট নেই।")

    msg = await e.reply(f"⏳ Creating service `{name}`...")

    payload = {
        "type": "web_service",
        "name": name,
        "ownerId": RENDER_OWNER_ID,
        "repo": repo_url,
        "autoDeploy": "yes",
        "branch": "main",
        "serviceDetails": {
            "env": "python",
            "region": "singapore",
            "plan": "free",
            "buildCommand": "pip install -r requirements.txt",
            "startCommand": "python main.py"
        }
    }

    try:
        status, data = await render_post("/services", payload)
        if status == 201:
            service = data.get("service", data)
            sid     = service.get("id", "—")
            sname   = service.get("name", name)
            surl    = service.get("serviceDetails", {}).get("url", "—")
            await msg.edit(
                f"✅ **Service Created!**\n\n"
                f"Name: `{sname}`\n"
                f"Service ID: `{sid}`\n"
                f"URL: `{surl}`\n\n"
                f"Deploy trigger হচ্ছে automatically (autoDeploy: yes)"
            )
        else:
            err = data.get("message", json.dumps(data, indent=2))
            await msg.edit(f"❌ **Create Failed ({status})**\n\n`{err}`")
    except asyncio.TimeoutError:
        await msg.edit("❌ Request timeout. Render API সাড়া দেয়নি।")
    except Exception as ex:
        await msg.edit(f"❌ Error: `{ex}`")

# ── /deploy ───────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern=r"^/deploy (\S+)$"))
async def _(e):
    if not owner(e): return
    sid = e.pattern_match.group(1).strip()
    msg = await e.reply(f"⏳ Triggering deploy for `{sid}`...")
    try:
        status, data = await render_post(f"/services/{sid}/deploys", {})
        if status in (200, 201):
            deploy_id  = data.get("id", "—")
            deploy_st  = data.get("status", "—")
            commit_msg = data.get("commit", {}).get("message", "—") if isinstance(data.get("commit"), dict) else "—"
            await msg.edit(
                f"✅ **Deploy Triggered!**\n\n"
                f"Service ID: `{sid}`\n"
                f"Deploy ID: `{deploy_id}`\n"
                f"Status: `{deploy_st}`\n"
                f"Commit: `{commit_msg}`"
            )
        else:
            err = data.get("message", json.dumps(data, indent=2))
            await msg.edit(f"❌ **Deploy Failed ({status})**\n\n`{err}`")
    except asyncio.TimeoutError:
        await msg.edit("❌ Request timeout.")
    except Exception as ex:
        await msg.edit(f"❌ Error: `{ex}`")

# ── /status ───────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern=r"^/status (\S+)$"))
async def _(e):
    if not owner(e): return
    sid = e.pattern_match.group(1).strip()
    msg = await e.reply(f"⏳ Fetching status for `{sid}`...")
    try:
        status, data = await render_get(f"/services/{sid}")
        if status == 200:
            service    = data.get("service", data)
            sname      = service.get("name", "—")
            sstate     = service.get("suspended", "—")
            details    = service.get("serviceDetails", {})
            surl       = details.get("url", "—")
            region     = details.get("region", "—")
            plan       = details.get("plan", "—")
            created_at = service.get("createdAt", "—")
            updated_at = service.get("updatedAt", "—")
            state_icon = "✅" if sstate == "not_suspended" else "⏸️"
            await msg.edit(
                f"**Service Status**\n\n"
                f"Name: `{sname}`\n"
                f"ID: `{sid}`\n"
                f"State: {state_icon} `{sstate}`\n"
                f"URL: `{surl}`\n"
                f"Region: `{region}`\n"
                f"Plan: `{plan}`\n"
                f"Created: `{created_at[:10] if len(str(created_at)) > 10 else created_at}`\n"
                f"Updated: `{updated_at[:10] if len(str(updated_at)) > 10 else updated_at}`"
            )
        else:
            err = data.get("message", json.dumps(data, indent=2))
            await msg.edit(f"❌ **Status Failed ({status})**\n\n`{err}`")
    except asyncio.TimeoutError:
        await msg.edit("❌ Request timeout.")
    except Exception as ex:
        await msg.edit(f"❌ Error: `{ex}`")

# ── /add_env ──────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern=r"^/add_env (\S+) (\S+) (.+)$"))
async def _(e):
    if not owner(e): return
    sid   = e.pattern_match.group(1).strip()
    key   = e.pattern_match.group(2).strip()
    value = e.pattern_match.group(3).strip()
    msg   = await e.reply(f"⏳ Fetching existing env vars for `{sid}`...")

    try:
        status, data = await render_get(f"/services/{sid}/env-vars")
        if status != 200:
            err = data.get("message", json.dumps(data, indent=2))
            return await msg.edit(f"❌ **Fetch Env Failed ({status})**\n\n`{err}`")

        existing = data if isinstance(data, list) else data.get("envVars", [])
        env_list = []
        for item in existing:
            env_list.append({
                "key":   item.get("key", ""),
                "value": item.get("value", "")
            })

        updated = False
        for item in env_list:
            if item["key"] == key:
                item["value"] = value
                updated = True
                break
        if not updated:
            env_list.append({"key": key, "value": value})

        await msg.edit(f"⏳ Updating env vars...")

        put_status, put_data = await render_put(f"/services/{sid}/env-vars", env_list)
        if put_status in (200, 201):
            action = "Updated" if updated else "Added"
            await msg.edit(
                f"✅ **Env Var {action}!**\n\n"
                f"Service ID: `{sid}`\n"
                f"Key: `{key}`\n"
                f"Value: `{value}`\n\n"
                f"Total env vars: `{len(env_list)}`"
            )
        else:
            err = put_data.get("message", json.dumps(put_data, indent=2))
            await msg.edit(f"❌ **Update Env Failed ({put_status})**\n\n`{err}`")

    except asyncio.TimeoutError:
        await msg.edit("❌ Request timeout.")
    except Exception as ex:
        await msg.edit(f"❌ Error: `{ex}`")

# ── /list_services ────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern="^/list_services$"))
async def _(e):
    if not owner(e): return
    msg = await e.reply("⏳ Fetching all services...")
    try:
        status, data = await render_get(f"/services?ownerId={RENDER_OWNER_ID}&limit=20")
        if status == 200:
            services = data if isinstance(data, list) else data.get("services", [])
            if not services:
                return await msg.edit("No services found.")
            lines = []
            for item in services:
                svc   = item.get("service", item)
                sname = svc.get("name", "—")
                sid   = svc.get("id", "—")
                susp  = svc.get("suspended", "—")
                icon  = "✅" if susp == "not_suspended" else "⏸️"
                lines.append(f"{icon} **{sname}**\n   ID: `{sid}`")
            await msg.edit(f"**All Services ({len(services)})**\n\n" + "\n\n".join(lines))
        else:
            err = data.get("message", json.dumps(data, indent=2))
            await msg.edit(f"❌ **List Failed ({status})**\n\n`{err}`")
    except asyncio.TimeoutError:
        await msg.edit("❌ Request timeout.")
    except Exception as ex:
        await msg.edit(f"❌ Error: `{ex}`")

# ── /logs ─────────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern=r"^/logs (\S+)$"))
async def _(e):
    if not owner(e): return
    sid = e.pattern_match.group(1).strip()
    msg = await e.reply(f"⏳ Fetching deploy logs for `{sid}`...")
    try:
        status, data = await render_get(f"/services/{sid}/deploys?limit=5")
        if status == 200:
            deploys = data if isinstance(data, list) else data.get("deploys", [])
            if not deploys:
                return await msg.edit("No deploys found.")
            lines = []
            for item in deploys:
                d      = item.get("deploy", item)
                did    = d.get("id", "—")
                dst    = d.get("status", "—")
                dtime  = d.get("createdAt", "—")
                commit = d.get("commit", {})
                cmsg   = commit.get("message", "—") if isinstance(commit, dict) else "—"
                icon   = "✅" if dst == "live" else ("❌" if dst == "failed" else "⏳")
                lines.append(
                    f"{icon} `{dst}`\n"
                    f"   ID: `{did}`\n"
                    f"   Commit: `{cmsg[:50]}`\n"
                    f"   Time: `{str(dtime)[:16]}`"
                )
            await msg.edit(f"**Last {len(lines)} Deploy(s) for `{sid}`**\n\n" + "\n\n".join(lines))
        else:
            err = data.get("message", json.dumps(data, indent=2))
            await msg.edit(f"❌ **Logs Failed ({status})**\n\n`{err}`")
    except asyncio.TimeoutError:
        await msg.edit("❌ Request timeout.")
    except Exception as ex:
        await msg.edit(f"❌ Error: `{ex}`")

# ── /suspend ──────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern=r"^/suspend (\S+)$"))
async def _(e):
    if not owner(e): return
    sid = e.pattern_match.group(1).strip()
    msg = await e.reply(f"⏳ Suspending `{sid}`...")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{RENDER_BASE}/services/{sid}/suspend",
                headers=render_headers(),
                timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                status = r.status
                text   = await r.text()
        if status in (200, 204):
            await msg.edit(f"✅ Service `{sid}` suspended.")
        else:
            await msg.edit(f"❌ **Suspend Failed ({status})**\n\n`{text[:200]}`")
    except asyncio.TimeoutError:
        await msg.edit("❌ Request timeout.")
    except Exception as ex:
        await msg.edit(f"❌ Error: `{ex}`")

# ── /resume ───────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern=r"^/resume (\S+)$"))
async def _(e):
    if not owner(e): return
    sid = e.pattern_match.group(1).strip()
    msg = await e.reply(f"⏳ Resuming `{sid}`...")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{RENDER_BASE}/services/{sid}/resume",
                headers=render_headers(),
                timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                status = r.status
                text   = await r.text()
        if status in (200, 204):
            await msg.edit(f"✅ Service `{sid}` resumed.")
        else:
            await msg.edit(f"❌ **Resume Failed ({status})**\n\n`{text[:200]}`")
    except asyncio.TimeoutError:
        await msg.edit("❌ Request timeout.")
    except Exception as ex:
        await msg.edit(f"❌ Error: `{ex}`")

# ── /delete ───────────────────────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern=r"^/delete (\S+)$"))
async def _(e):
    if not owner(e): return
    sid = e.pattern_match.group(1).strip()
    msg = await e.reply(f"⏳ Deleting `{sid}`...")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.delete(
                f"{RENDER_BASE}/services/{sid}",
                headers=render_headers(),
                timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                status = r.status
                text   = await r.text()
        if status in (200, 204):
            await msg.edit(f"✅ Service `{sid}` deleted.")
        else:
            await msg.edit(f"❌ **Delete Failed ({status})**\n\n`{text[:200]}`")
    except asyncio.TimeoutError:
        await msg.edit("❌ Request timeout.")
    except Exception as ex:
        await msg.edit(f"❌ Error: `{ex}`")

# ── BOOTSTRAP ─────────────────────────────────────────────────────────────────
async def main():
    threading.Thread(target=run_server, daemon=True).start()
    await bot.start(bot_token=BOT_TOKEN)
    print("[+] Render Manager Bot online.")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
