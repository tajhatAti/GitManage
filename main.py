import os
import json
import asyncio
import logging
from aiohttp import web, ClientSession, ClientTimeout
from telethon import TelegramClient, events, Button

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("render-manager")

# ---------------- CONFIG (all from environment, no hardcoded secrets) ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
OWNER_IDS = {int(x) for x in os.environ.get("OWNER_IDS", "").split(",") if x.strip()}
UPTIMEROBOT_API_KEY = os.environ.get("UPTIMEROBOT_API_KEY", "")
PORT = int(os.environ.get("PORT", 8080))

ACCOUNTS_FILE = "accounts.json"
SERVICES_FILE = "services.json"

RENDER_BASE = "https://api.render.com/v1"
UR_BASE = "https://api.uptimerobot.com/v2"

# Client is created inside main(), after the event loop exists.
# Do NOT instantiate TelegramClient at module level — that's what caused
# "RuntimeError: no running event loop" on Python 3.14.
client: TelegramClient = None

# in-memory conversation state and active-account selection per user
user_state = {}      # user_id -> {"step": str, "data": dict}
active_account = {}  # user_id -> account_tag

# ---------------- JSON helpers ----------------
def load_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def get_accounts():
    return load_json(ACCOUNTS_FILE)

def get_services():
    return load_json(SERVICES_FILE)

# ---------------- Access control ----------------
def is_owner(uid):
    return uid in OWNER_IDS

def get_active_key(uid):
    tag = active_account.get(uid)
    accounts = get_accounts()
    if not tag or tag not in accounts:
        return None, None
    return tag, accounts[tag]["api_key"]

# ---------------- Render API helpers ----------------
async def render_request(method, path, api_key, json_body=None, params=None):
    url = f"{RENDER_BASE}{path}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    try:
        async with asyncio.timeout(10):
            async with ClientSession(timeout=ClientTimeout(total=10)) as session:
                async with session.request(method, url, headers=headers, json=json_body, params=params) as resp:
                    text = await resp.text()
                    try:
                        data = json.loads(text) if text else None
                    except json.JSONDecodeError:
                        data = text
                    return resp.status, data
    except asyncio.TimeoutError:
        return 408, "Request timed out after 10 seconds"
    except Exception as e:
        return 599, str(e)

async def verify_render_key(api_key):
    status, data = await render_request("GET", "/owners", api_key)
    return status, data

async def render_list_services(api_key, owner_id=None):
    params = {"limit": 100}
    if owner_id:
        params["ownerId"] = owner_id
    return await render_request("GET", "/services", api_key, params=params)

async def render_get_service(api_key, service_id):
    return await render_request("GET", f"/services/{service_id}", api_key)

async def render_create_service(api_key, name, repo_url, owner_id, env_vars):
    body = {
        "type": "web_service",
        "name": name,
        "ownerId": owner_id,
        "repo": repo_url,
        "branch": "main",
        "autoDeploy": "yes",
        "serviceDetails": {
            "envSpecificDetails": {"buildCommand": "pip install -r requirements.txt", "startCommand": "python main.py"},
            "plan": "free",
            "envVars": env_vars
        }
    }
    return await render_request("POST", "/services", api_key, json_body=body)

async def render_deploy(api_key, service_id):
    return await render_request("POST", f"/services/{service_id}/deploys", api_key, json_body={})

async def render_suspend(api_key, service_id):
    return await render_request("POST", f"/services/{service_id}/suspend", api_key, json_body={})

async def render_resume(api_key, service_id):
    return await render_request("POST", f"/services/{service_id}/resume", api_key, json_body={})

async def render_get_env(api_key, service_id):
    status, data = await render_request("GET", f"/services/{service_id}/env-vars", api_key)
    if status != 200 or not isinstance(data, list):
        return status, []
    normalized = []
    for item in data:
        if "envVar" in item:
            normalized.append(item["envVar"])
        else:
            normalized.append(item)
    return status, normalized

async def render_put_env(api_key, service_id, env_list):
    body = [{"key": e["key"], "value": e["value"]} for e in env_list]
    return await render_request("PUT", f"/services/{service_id}/env-vars", api_key, json_body=body)

# ---------------- UptimeRobot helper ----------------
async def uptimerobot_create_monitor(url, friendly_name):
    if not UPTIMEROBOT_API_KEY:
        return None
    body = {
        "api_key": UPTIMEROBOT_API_KEY,
        "format": "json",
        "type": 1,
        "url": url,
        "friendly_name": friendly_name,
        "interval": 300
    }
    try:
        async with asyncio.timeout(10):
            async with ClientSession(timeout=ClientTimeout(total=10)) as session:
                async with session.post(f"{UR_BASE}/newMonitor", data=body) as resp:
                    return await resp.json()
    except Exception as e:
        return {"stat": "fail", "error": str(e)}

# ---------------- Access filter ----------------
def owner_only(func):
    async def wrapper(event):
        if not is_owner(event.sender_id):
            await event.respond("🚫 Access denied.")
            return
        return await func(event)
    return wrapper

# ---------------- Handler registration ----------------
# All @client.on(...) decorators live inside this function so they only run
# AFTER client has been created in main(), once the event loop is running.
def register_handlers():

    @client.on(events.NewMessage(pattern="/start"))
    @owner_only
    async def start_handler(event):
        await event.respond(
            "👋 Render & UptimeRobot Smart Manager v5.0\n\n"
            "/add_account - link a Render account\n"
            "/accounts - list & switch active account\n"
            "/env - manage environment variables\n"
            "/details - account analytics\n"
            "/create - create a new service\n"
            "/deploy /suspend /resume - service ops\n"
            "/manage <name_or_id> - control panel"
        )

    @client.on(events.NewMessage(pattern="/add_account"))
    @owner_only
    async def add_account_start(event):
        user_state[event.sender_id] = {"step": "await_tag", "data": {}}
        await event.respond("Send a short tag/name for this Render account (e.g. `main`, `client1`):")

    @client.on(events.NewMessage(pattern="/env$"))
    @owner_only
    async def env_start(event):
        user_state[event.sender_id] = {"step": "env_await_service", "data": {}}
        await event.respond("Send the service ID or saved service name:")

    @client.on(events.NewMessage(pattern="/details"))
    @owner_only
    async def details_handler(event):
        uid = event.sender_id
        tag, api_key = get_active_key(uid)
        if not api_key:
            await event.respond("⚠️ No active account selected. Use /accounts first.")
            return
        accounts = get_accounts()
        owner_id = accounts[tag]["owner_id"]
        status, services = await render_list_services(api_key, owner_id)
        if status != 200 or not isinstance(services, list):
            await event.respond(f"❌ Failed to fetch services. Status: {status}\n{services}")
            return

        total = len(services)
        active, suspended, susp_by_render = 0, 0, 0
        running_names = []
        for s in services:
            svc = s.get("service", s)
            state = (svc.get("suspended") or "").lower()
            if state == "not_suspended":
                active += 1
                running_names.append(svc.get("name", "unknown"))
            elif state == "suspended":
                suspended += 1
            elif state == "suspended_by_render":
                susp_by_render += 1

        msg = (
            f"📊 Account: `{tag}`\n\n"
            f"Total services: {total}\n"
            f"✅ Active: {active}\n"
            f"⏸ Suspended (manual): {suspended}\n"
            f"⛔ Suspended by Render: {susp_by_render}\n\n"
            f"Running services:\n" + ("\n".join(f"• {n}" for n in running_names) if running_names else "None")
        )
        await event.respond(msg)

    @client.on(events.NewMessage(pattern="/accounts"))
    @owner_only
    async def accounts_handler(event):
        accounts = get_accounts()
        if not accounts:
            await event.respond("No accounts saved yet. Use /add_account.")
            return
        buttons = [[Button.inline(f"{'✅ ' if active_account.get(event.sender_id)==tag else ''}{tag}", f"switch_{tag}")] for tag in accounts]
        await event.respond("Your saved Render accounts:", buttons=buttons)

    @client.on(events.CallbackQuery(pattern=b"switch_(.+)"))
    @owner_only
    async def switch_account_cb(event):
        tag = event.pattern_match.group(1).decode()
        active_account[event.sender_id] = tag
        await event.answer(f"Switched to account: {tag}")
        await event.edit(f"✅ Active account is now: `{tag}`")

    @client.on(events.NewMessage(pattern="/create$"))
    @owner_only
    async def create_start(event):
        tag, api_key = get_active_key(event.sender_id)
        if not api_key:
            await event.respond("⚠️ No active account selected. Use /accounts first.")
            return
        user_state[event.sender_id] = {"step": "create_await_name", "data": {}}
        await event.respond("Send the new service name:")

    @client.on(events.NewMessage(pattern="/deploy(?: (.+))?"))
    @owner_only
    async def deploy_handler(event):
        await resolve_and_run(event, render_deploy, "deployed")

    @client.on(events.NewMessage(pattern="/suspend(?: (.+))?"))
    @owner_only
    async def suspend_handler(event):
        await resolve_and_run(event, render_suspend, "suspended")

    @client.on(events.NewMessage(pattern="/resume(?: (.+))?"))
    @owner_only
    async def resume_handler(event):
        await resolve_and_run(event, render_resume, "resumed")

    @client.on(events.NewMessage(pattern="/manage(?: (.+))?"))
    @owner_only
    async def manage_handler(event):
        identifier = event.pattern_match.group(1)
        if not identifier:
            await event.respond("Usage: /manage <name_or_id>")
            return
        identifier = identifier.strip()
        buttons = [
            [Button.inline("📈 Status", f"m_status_{identifier}"), Button.inline("🚀 Deploy", f"m_deploy_{identifier}")],
            [Button.inline("⏸ Suspend", f"m_suspend_{identifier}"), Button.inline("▶️ Resume", f"m_resume_{identifier}")],
        ]
        await event.respond(f"Manage `{identifier}`:", buttons=buttons)

    @client.on(events.CallbackQuery(pattern=b"m_(status|deploy|suspend|resume)_(.+)"))
    @owner_only
    async def manage_cb(event):
        action = event.pattern_match.group(1).decode()
        identifier = event.pattern_match.group(2).decode()
        tag, api_key = get_active_key(event.sender_id)
        if not api_key:
            await event.answer("No active account.", alert=True)
            return
        services = get_services()
        service_id = services.get(identifier, {}).get("service_id", identifier)

        if action == "status":
            status, data = await render_get_service(api_key, service_id)
            if status == 200 and isinstance(data, dict):
                await event.answer()
                await event.respond(f"Status of `{identifier}`: {data.get('suspended', 'unknown')}")
            else:
                await event.answer(f"Error {status}", alert=True)
            return

        fn_map = {"deploy": render_deploy, "suspend": render_suspend, "resume": render_resume}
        status, data = await fn_map[action](api_key, service_id)
        if status in (200, 201, 202):
            await event.answer(f"{action.capitalize()} triggered ✅")
        else:
            await event.answer(f"Failed: {status}", alert=True)

    @client.on(events.CallbackQuery(pattern=b"env_(view|add|edit)_(.+)"))
    @owner_only
    async def env_action_cb(event):
        action = event.pattern_match.group(1).decode()
        service_id = event.pattern_match.group(2).decode()
        tag, api_key = get_active_key(event.sender_id)
        if not api_key:
            await event.answer("No active account.", alert=True)
            return

        if action == "view":
            status, envs = await render_get_env(api_key, service_id)
            await event.answer()
            if status != 200:
                await event.respond(f"❌ Error fetching env vars. Status: {status}")
                return
            if not envs:
                await event.respond("No environment variables set.")
                return
            lines = "\n".join(f"`{e['key']}` = `{e.get('value','')}`" for e in envs)
            await event.respond(f"📄 Environment Variables:\n{lines}")

        elif action == "add":
            user_state[event.sender_id] = {"step": "env_add_key", "data": {"service_id": service_id}}
            await event.answer()
            await event.respond("Send the new variable KEY:")

        elif action == "edit":
            status, envs = await render_get_env(api_key, service_id)
            await event.answer()
            if status != 200 or not envs:
                await event.respond("No variables to edit.")
                return
            buttons = [[Button.inline(e["key"], f"editkey_{service_id}_{e['key']}")] for e in envs]
            await event.respond("Select a key to edit:", buttons=buttons)

    @client.on(events.CallbackQuery(pattern=b"editkey_(.+?)_(.+)"))
    @owner_only
    async def edit_key_cb(event):
        service_id = event.pattern_match.group(1).decode()
        key = event.pattern_match.group(2).decode()
        user_state[event.sender_id] = {"step": "env_edit_value", "data": {"service_id": service_id, "key": key}}
        await event.answer()
        await event.respond(f"Send the new value for `{key}`:")

    @client.on(events.NewMessage())
    @owner_only
    async def text_router(event):
        uid = event.sender_id
        if uid not in user_state:
            return
        if event.raw_text.startswith("/"):
            return

        state = user_state[uid]
        step = state["step"]
        text = event.raw_text.strip()

        if step == "await_tag":
            state["data"]["tag"] = text
            state["step"] = "await_owner_id"
            await event.respond("Send the Render owner ID (starts with `usr-` or `tea-`):")

        elif step == "await_owner_id":
            state["data"]["owner_id"] = text
            state["step"] = "await_api_key"
            await event.respond("Send the Render API key:")

        elif step == "await_api_key":
            api_key = text
            await event.respond("🔍 Verifying key...")
            status, data = await verify_render_key(api_key)
            if status == 200:
                accounts = get_accounts()
                tag = state["data"]["tag"]
                accounts[tag] = {"owner_id": state["data"]["owner_id"], "api_key": api_key}
                save_json(ACCOUNTS_FILE, accounts)
                active_account[uid] = tag
                await event.respond(f"✅ Account `{tag}` verified and saved. It's now your active account.")
            else:
                await event.respond(f"❌ Verification failed. Status: {status}\nReason: {data}")
            del user_state[uid]

        elif step == "env_await_service":
            services = get_services()
            service_id = services.get(text, {}).get("service_id", text)
            buttons = [
                [Button.inline("📄 View Env", f"env_view_{service_id}")],
                [Button.inline("➕ Add New", f"env_add_{service_id}")],
                [Button.inline("✏️ Edit Existing", f"env_edit_{service_id}")],
            ]
            await event.respond("Choose an action:", buttons=buttons)
            del user_state[uid]

        elif step == "env_add_key":
            state["data"]["key"] = text
            state["step"] = "env_add_value"
            await event.respond("Send the VALUE:")

        elif step == "env_add_value":
            service_id = state["data"]["service_id"]
            key = state["data"]["key"]
            value = text
            tag, api_key = get_active_key(uid)
            status, envs = await render_get_env(api_key, service_id)
            if status != 200:
                await event.respond(f"❌ Could not fetch existing env vars. Status: {status}")
                del user_state[uid]
                return
            envs = [e for e in envs if e["key"] != key]
            envs.append({"key": key, "value": value})
            put_status, put_data = await render_put_env(api_key, service_id, envs)
            if put_status == 200:
                await event.respond(f"✅ `{key}` added/updated successfully.")
            else:
                await event.respond(f"❌ Failed to update. Status: {put_status}\n{put_data}")
            del user_state[uid]

        elif step == "env_edit_value":
            service_id = state["data"]["service_id"]
            key = state["data"]["key"]
            value = text
            tag, api_key = get_active_key(uid)
            status, envs = await render_get_env(api_key, service_id)
            if status != 200:
                await event.respond(f"❌ Could not fetch existing env vars. Status: {status}")
                del user_state[uid]
                return
            for e in envs:
                if e["key"] == key:
                    e["value"] = value
            put_status, put_data = await render_put_env(api_key, service_id, envs)
            if put_status == 200:
                await event.respond(f"✅ `{key}` updated successfully.")
            else:
                await event.respond(f"❌ Failed to update. Status: {put_status}\n{put_data}")
            del user_state[uid]

        elif step == "create_await_name":
            state["data"]["name"] = text
            state["step"] = "create_await_repo"
            await event.respond("Send the GitHub repo URL:")

        elif step == "create_await_repo":
            repo_url = text
            name = state["data"]["name"]
            tag, api_key = get_active_key(uid)
            accounts = get_accounts()
            owner_id = accounts[tag]["owner_id"]

            env_vars = []
            status, data = await render_create_service(api_key, name, repo_url, owner_id, env_vars)
            if status not in (200, 201):
                await event.respond(f"❌ Creation failed. Status: {status}\n{data}")
                del user_state[uid]
                return

            service_id = data.get("service", {}).get("id") or data.get("id")
            service_url = data.get("service", {}).get("serviceDetails", {}).get("url", "")

            services = get_services()
            services[name] = {"service_id": service_id, "account_tag": tag}
            save_json(SERVICES_FILE, services)

            ur_result = None
            if service_url:
                ur_result = await uptimerobot_create_monitor(service_url, name)

            msg = f"✅ Service `{name}` created (ID: `{service_id}`)."
            if ur_result:
                msg += f"\nUptimeRobot: {ur_result.get('stat', 'unknown')}"
            await event.respond(msg)
            del user_state[uid]

        elif step == "op_await_target":
            verb = state["data"]["action"]
            fn_map = {"deployed": render_deploy, "suspended": render_suspend, "resumed": render_resume}
            await run_action_for_target(event, text, fn_map[verb], verb)
            del user_state[uid]


async def resolve_and_run(event, action_fn, verb):
    arg = event.pattern_match.group(1)
    if not arg:
        user_state[event.sender_id] = {"step": "op_await_target", "data": {"action": verb}}
        await event.respond(f"Send the service name or ID to {verb.rstrip('ed')}:")
        return
    await run_action_for_target(event, arg.strip(), action_fn, verb)

async def run_action_for_target(event, identifier, action_fn, verb):
    tag, api_key = get_active_key(event.sender_id)
    if not api_key:
        await event.respond("⚠️ No active account selected. Use /accounts first.")
        return
    services = get_services()
    service_id = services.get(identifier, {}).get("service_id", identifier)
    status, data = await action_fn(api_key, service_id)
    if status in (200, 201, 202):
        await event.respond(f"✅ Service `{identifier}` {verb} successfully.")
    else:
        await event.respond(f"❌ Failed to {verb.rstrip('ed')} `{identifier}`. Status: {status}\n{data}")

# ---------------- Keep-alive HTTP server ----------------
async def handle_root(request):
    return web.Response(text="Bot is alive", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Web server running on 0.0.0.0:{PORT}")

# ---------------- Main ----------------
async def main():
    global client
    client = TelegramClient("render_manager_bot", API_ID, API_HASH)
    register_handlers()
    await start_web_server()
    await client.start(bot_token=BOT_TOKEN)
    log.info("Bot started.")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
