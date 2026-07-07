import os, asyncio, json, base64, threading, itertools, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from telethon import TelegramClient, events, Button
import aiohttp

# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================
API_ID    = int(os.environ.get("API_ID", "0"))
API_HASH  = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID  = int(os.environ.get("OWNER_ID", "0"))
PORT      = int(os.environ.get("PORT", 8080))

GH_API = "https://api.github.com"
ACCOUNTS_FILE = "accounts.json"
WORKSPACE_FILE = "workspace.json"

# ============================================================
# EVENT LOOP FIX (Python 3.14+)
# ============================================================
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

bot = TelegramClient("github_bot", API_ID, API_HASH, loop=loop)

# ============================================================
# HTTP HEALTH SERVER
# ============================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    
    def log_message(self, *args):
        pass

def run_health_server():
    HTTPServer(('0.0.0.0', PORT), HealthHandler).serve_forever()

# ============================================================
# PERSISTENCE
# ============================================================
def load_accounts():
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_accounts(data):
    with open(ACCOUNTS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_account(user_id):
    return load_accounts().get(str(user_id))

def update_account(user_id, **kwargs):
    data = load_accounts()
    entry = data.get(str(user_id), {})
    entry.update(kwargs)
    data[str(user_id)] = entry
    save_accounts(data)

def load_workspace():
    if os.path.exists(WORKSPACE_FILE):
        try:
            with open(WORKSPACE_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_workspace(data):
    with open(WORKSPACE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_workspace(user_id):
    ws = load_workspace()
    return ws.get(str(user_id), {})

def update_workspace(user_id, **kwargs):
    ws = load_workspace()
    entry = ws.get(str(user_id), {})
    entry.update(kwargs)
    ws[str(user_id)] = entry
    save_workspace(ws)

# ============================================================
# CALLBACK TOKEN CACHE
# ============================================================
CB_STORE = {}
CB_COUNTER = itertools.count()

def cb_put(value):
    token = str(next(CB_COUNTER))
    CB_STORE[token] = value
    if len(CB_STORE) > 5000:
        for k in list(CB_STORE.keys())[:2500]:
            CB_STORE.pop(k, None)
    return token

def cb_get(token):
    return CB_STORE.get(token)

def cb_data(prefix, value):
    return f"{prefix}:{cb_put(value)}".encode()

# ============================================================
# GITHUB API
# ============================================================
def gh_headers(token):
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

async def gh_request(method, path, token, json_data=None, params=None):
    url = f"{GH_API}{path}"
    try:
        async with asyncio.timeout(10):
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method, url, headers=gh_headers(token), json=json_data, params=params
                ) as resp:
                    try:
                        data = await resp.json()
                    except:
                        data = {}
                    return resp.status, data
    except asyncio.TimeoutError:
        return 0, {"message": "Request timed out"}
    except Exception as e:
        return -1, {"message": str(e)}

async def gh_validate_token(token):
    status, data = await gh_request("GET", "/user", token)
    if status == 200:
        return True, data.get("login"), None
    err = data.get("message", "Unknown error")
    return False, None, err

async def gh_list_repos(token):
    status, data = await gh_request("GET", "/user/repos", token, params={"per_page": 100, "sort": "updated"})
    return data if status == 200 else []

async def gh_get_contents(token, repo, path=""):
    status, data = await gh_request("GET", f"/repos/{repo}/contents/{path}", token)
    return data if status == 200 else None

async def gh_put_file(token, repo, path, content_str, message, sha=None):
    encoded = base64.b64encode(content_str.encode()).decode()
    payload = {"message": message, "content": encoded}
    if sha:
        payload["sha"] = sha
    status, data = await gh_request("PUT", f"/repos/{repo}/contents/{path}", token, json_data=payload)
    return data if status in (200, 201) else None

async def gh_delete_file(token, repo, path, sha):
    payload = {"message": f"Delete {path}", "sha": sha}
    status, data = await gh_request("DELETE", f"/repos/{repo}/contents/{path}", token, json_data=payload)
    return status == 200

async def gh_create_repo(token, name, private=False):
    payload = {"name": name, "private": private, "auto_init": True}
    status, data = await gh_request("POST", "/user/repos", token, json_data=payload)
    return data if status == 201 else None

async def gh_get_repo_info(token, repo):
    status, data = await gh_request("GET", f"/repos/{repo}", token)
    return data if status == 200 else None

# ============================================================
# HELPERS
# ============================================================
def owner_only(func):
    async def wrapper(event):
        if event.sender_id != OWNER_ID:
            try:
                await event.respond("⛔ Access Denied")
            except:
                pass
            return
        return await func(event)
    return wrapper

async def render_file_buttons(items, current_path):
    buttons = []
    for item in items:
        name = item.get("name")
        path = item.get("path")
        if item.get("type") == "dir":
            buttons.append([Button.inline(f"📁 {name}", cb_data("nav", path))])
        else:
            buttons.append([Button.inline(f"📄 {name}", cb_data("file", path))])
    
    if current_path:
        parent = "/".join(current_path.split("/")[:-1])
        buttons.append([Button.inline("⬅️ Back", cb_data("nav", parent))])
    
    return buttons

# ============================================================
# /start
# ============================================================
@bot.on(events.NewMessage(pattern=r"^/start$"))
@owner_only
async def start_handler(event):
    await event.respond(
        "🐙 **GitHub Repo Manager Pro**\n\n"
        "**Account & Repos**\n"
        "/add_account - Add GitHub PAT\n"
        "/whoami - Show current account\n"
        "/new_repo - Create repository\n"
        "/switch_repo - Select repo\n\n"
        "**File Operations**\n"
        "/files - Browse files\n"
        "/create_file - Create file\n\n"
        "**Repository Info**\n"
        "/repo_info - Repository details\n"
        "/repo_stats - Stars, forks, etc",
        parse_mode="markdown"
    )

# ============================================================
# /add_account
# ============================================================
@bot.on(events.NewMessage(pattern=r"^/add_account$"))
@owner_only
async def add_account_handler(event):
    chat_id = event.chat_id
    try:
        async with bot.conversation(chat_id, timeout=180) as conv:
            await conv.send_message(
                "🔐 **GitHub Account Setup**\n\n"
                "Send your GitHub Personal Access Token (PAT):\n"
                "(Needs: repo, user scope)",
                parse_mode="markdown"
            )
            resp = await conv.get_response()
            pat = resp.raw_text.strip()
            
            if not pat or len(pat) < 10:
                await conv.send_message("❌ Invalid token format")
                return
            
            await conv.send_message("⏳ Validating token...")
            valid, username, err = await gh_validate_token(pat)
            
            if not valid:
                await conv.send_message(f"❌ Validation failed:\n{err}")
                return
            
            update_account(OWNER_ID, gh_token=pat, gh_username=username)
            await conv.send_message(
                f"✅ **Account Linked Successfully!**\n\n"
                f"👤 Username: `{username}`\n"
                f"🔐 Token: `{pat[:20]}...`",
                parse_mode="markdown"
            )
    except asyncio.TimeoutError:
        await event.respond("⌛ Setup timed out")
    except Exception as e:
        await event.respond(f"❌ Error: {e}")

# ============================================================
# /whoami
# ============================================================
@bot.on(events.NewMessage(pattern=r"^/whoami$"))
@owner_only
async def whoami_handler(event):
    acc = get_account(OWNER_ID)
    if not acc or not acc.get("gh_token"):
        await event.respond("⚠️ No account linked. Use /add_account")
        return
    
    ws = get_workspace(OWNER_ID)
    active_repo = ws.get("active_repo", "None")
    
    await event.respond(
        f"👤 GitHub: **{acc.get('gh_username')}**\n"
        f"📦 Active Repo: **{active_repo}**\n"
        f"⏱️ Last Sync: **{int(time.time())}**",
        parse_mode="markdown"
    )

# ============================================================
# /new_repo
# ============================================================
@bot.on(events.NewMessage(pattern=r"^/new_repo$"))
@owner_only
async def new_repo_handler(event):
    acc = get_account(OWNER_ID)
    if not acc or not acc.get("gh_token"):
        await event.respond("⚠️ No account linked. Use /add_account")
        return
    
    chat_id = event.chat_id
    token = acc["gh_token"]
    
    try:
        async with bot.conversation(chat_id, timeout=180) as conv:
            await conv.send_message("📦 Send repository name:")
            resp = await conv.get_response()
            repo_name = resp.raw_text.strip()
            
            if not repo_name:
                await conv.send_message("❌ Invalid name")
                return
            
            await conv.send_message(
                "🔒 Choose privacy:",
                buttons=[
                    [Button.inline("🌐 Public", b"privacy:public"), Button.inline("🔒 Private", b"privacy:private")]
                ]
            )
            
            cb_event = await conv.wait_event(events.CallbackQuery(func=lambda e: e.sender_id == OWNER_ID))
            privacy = cb_event.data.decode().split(":")[1]
            await cb_event.answer()
            is_private = privacy == "private"
            
            await conv.send_message(f"⏳ Creating `{repo_name}` ({privacy})...", parse_mode="markdown")
            
            data = await gh_create_repo(token, repo_name, is_private)
            
            if data:
                full_name = data.get("full_name")
                update_workspace(OWNER_ID, active_repo=full_name, current_path="")
                url = data.get("html_url")
                await conv.send_message(
                    f"✅ **Repository Created!**\n\n"
                    f"📦 **{full_name}**\n"
                    f"🔗 [Open on GitHub]({url})\n"
                    f"🔄 Set as active repo",
                    parse_mode="markdown"
                )
            else:
                await conv.send_message("❌ Failed to create repo")
    except asyncio.TimeoutError:
        await event.respond("⌛ Timeout")
    except Exception as e:
        await event.respond(f"❌ Error: {e}")

# ============================================================
# /switch_repo (6 per page)
# ============================================================
REPOS_CACHE = {}

async def show_repos_page(event, page=0, edit=False):
    acc = get_account(OWNER_ID)
    if not acc or not acc.get("gh_token"):
        await event.respond("⚠️ No account linked")
        return
    
    if OWNER_ID not in REPOS_CACHE:
        repos = await gh_list_repos(acc["gh_token"])
        REPOS_CACHE[OWNER_ID] = repos
    
    repos = REPOS_CACHE.get(OWNER_ID, [])
    if not repos:
        msg = "📭 No repositories found"
        if edit:
            await event.edit(msg)
        else:
            await event.respond(msg)
        return
    
    per_page = 6
    total = (len(repos) + per_page - 1) // per_page
    page = max(0, min(page, total - 1))
    start = page * per_page
    chunk = repos[start:start + per_page]
    
    buttons = []
    for repo in chunk:
        full_name = repo.get("full_name")
        buttons.append([Button.inline(f"📦 {full_name}", cb_data("select_repo", full_name))])
    
    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️ Prev", cb_data("repos_page", page - 1)))
    if page < total - 1:
        nav.append(Button.inline("➡️ Next", cb_data("repos_page", page + 1)))
    if nav:
        buttons.append(nav)
    
    text = f"📂 Select Repository (Page {page + 1}/{total})"
    
    if edit:
        await event.edit(text, buttons=buttons)
    else:
        await event.respond(text, buttons=buttons)

@bot.on(events.NewMessage(pattern=r"^/switch_repo$"))
@owner_only
async def switch_repo_handler(event):
    msg = await event.respond("⏳ Loading repos...")
    acc = get_account(OWNER_ID)
    if acc and acc.get("gh_token"):
        REPOS_CACHE[OWNER_ID] = await gh_list_repos(acc["gh_token"])
    await show_repos_page(msg, 0, edit=True)

@bot.on(events.CallbackQuery(pattern=rb"^select_repo:"))
@owner_only
async def select_repo_callback(event):
    token = event.data.decode().split(":", 1)[1]
    repo = cb_get(token)
    if not repo:
        await event.answer("⚠️ Expired", alert=True)
        return
    
    update_workspace(OWNER_ID, active_repo=repo, current_path="")
    await event.answer()
    await event.respond(f"✅ Switched to **{repo}**", parse_mode="markdown")

@bot.on(events.CallbackQuery(pattern=rb"^repos_page:"))
@owner_only
async def repos_page_callback(event):
    token = event.data.decode().split(":", 1)[1]
    page = cb_get(token)
    if page is None:
        await event.answer("⚠️ Expired", alert=True)
        return
    await event.answer()
    await show_repos_page(event, page, edit=True)

# ============================================================
# /files
# ============================================================
@bot.on(events.NewMessage(pattern=r"^/files$"))
@owner_only
async def files_handler(event):
    acc = get_account(OWNER_ID)
    if not acc or not acc.get("gh_token"):
        await event.respond("⚠️ No account linked")
        return
    
    ws = get_workspace(OWNER_ID)
    active_repo = ws.get("active_repo")
    
    if not active_repo:
        await event.respond("⚠️ No active repo. Use /switch_repo")
        return
    
    msg = await event.respond("⏳ Loading...")
    
    path = ws.get("current_path", "")
    contents = await gh_get_contents(acc["gh_token"], active_repo, path)
    
    if not contents:
        await msg.edit(f"❌ Path not found")
        return
    
    if not isinstance(contents, list):
        contents = [contents]
    
    buttons = await render_file_buttons(contents, path)
    label = path if path else "/ (root)"
    text = f"📂 **{active_repo}**\nPath: `{label}`"
    
    await msg.edit(text, buttons=buttons, parse_mode="markdown")

@bot.on(events.CallbackQuery(pattern=rb"^nav:"))
@owner_only
async def nav_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    
    if path is None:
        await event.answer("⚠️ Expired", alert=True)
        return
    
    acc = get_account(OWNER_ID)
    ws = get_workspace(OWNER_ID)
    repo = ws.get("active_repo")
    
    if not repo:
        await event.answer("⚠️ No repo selected", alert=True)
        return
    
    update_workspace(OWNER_ID, current_path=path)
    
    contents = await gh_get_contents(acc["gh_token"], repo, path)
    if not contents:
        await event.answer("❌ Path not found", alert=True)
        return
    
    if not isinstance(contents, list):
        contents = [contents]
    
    buttons = await render_file_buttons(contents, path)
    label = path if path else "/ (root)"
    text = f"📂 **{repo}**\nPath: `{label}`"
    
    await event.answer()
    await event.edit(text, buttons=buttons, parse_mode="markdown")

@bot.on(events.CallbackQuery(pattern=rb"^file:"))
@owner_only
async def file_selected_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    
    if path is None:
        await event.answer("⚠️ Expired", alert=True)
        return
    
    await event.answer()
    buttons = [
        [Button.inline("📄 View", cb_data("view", path))],
        [Button.inline("✏️ Edit", cb_data("edit", path))],
        [Button.inline("➕ Append", cb_data("append", path))],
        [Button.inline("🗑️ Delete", cb_data("delete", path))]
    ]
    
    await event.respond(f"🗂️ **{path}**", buttons=buttons, parse_mode="markdown")

# ============================================================
# FILE OPERATIONS
# ============================================================
@bot.on(events.CallbackQuery(pattern=rb"^view:"))
@owner_only
async def view_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    
    if not path:
        await event.answer("⚠️ Expired", alert=True)
        return
    
    await event.answer()
    
    acc = get_account(OWNER_ID)
    ws = get_workspace(OWNER_ID)
    
    data = await gh_get_contents(acc["gh_token"], ws["active_repo"], path)
    if not data or "content" not in data:
        await event.respond("❌ Could not fetch file")
        return
    
    try:
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except:
        await event.respond("❌ Could not decode")
        return
    
    if len(content) > 3500:
        content = content[:3500] + "\n...[truncated]"
    
    await event.respond(f"```\n{content}\n```", parse_mode="markdown")

@bot.on(events.CallbackQuery(pattern=rb"^edit:"))
@owner_only
async def edit_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    
    if not path:
        await event.answer("⚠️ Expired", alert=True)
        return
    
    await event.answer()
    await edit_file_flow(event.chat_id, path)

@bot.on(events.CallbackQuery(pattern=rb"^append:"))
@owner_only
async def append_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    
    if not path:
        await event.answer("⚠️ Expired", alert=True)
        return
    
    await event.answer()
    await append_file_flow(event.chat_id, path)

@bot.on(events.CallbackQuery(pattern=rb"^delete:"))
@owner_only
async def delete_callback(event):
    token = event.data.decode().split(":", 1)[1]
    path = cb_get(token)
    
    if not path:
        await event.answer("⚠️ Expired", alert=True)
        return
    
    await event.answer()
    
    acc = get_account(OWNER_ID)
    ws = get_workspace(OWNER_ID)
    
    data = await gh_get_contents(acc["gh_token"], ws["active_repo"], path)
    if not data or "sha" not in data:
        await event.respond("❌ Not found")
        return
    
    msg = await event.respond("⏳ Deleting...")
    ok = await gh_delete_file(acc["gh_token"], ws["active_repo"], path, data["sha"])
    
    if ok:
        await msg.edit(f"✅ Deleted: `{path}`", parse_mode="markdown")
    else:
        await msg.edit(f"❌ Failed", parse_mode="markdown")

async def edit_file_flow(chat_id, path):
    acc = get_account(OWNER_ID)
    ws = get_workspace(OWNER_ID)
    
    try:
        async with bot.conversation(chat_id, timeout=180) as conv:
            await conv.send_message(f"✏️ Send new content for `{path}`:", parse_mode="markdown")
            resp = await conv.get_response()
            new_content = resp.raw_text
            
            data = await gh_get_contents(acc["gh_token"], ws["active_repo"], path)
            if not data or "sha" not in data:
                await conv.send_message("❌ File SHA not found")
                return
            
            await conv.send_message("⏳ Uploading...")
            result = await gh_put_file(acc["gh_token"], ws["active_repo"], path, new_content, f"Edit {path}", data["sha"])
            
            if result:
                await conv.send_message(f"✅ Updated: `{path}`", parse_mode="markdown")
            else:
                await conv.send_message("❌ Upload failed")
    except asyncio.TimeoutError:
        await bot.send_message(chat_id, "⌛ Timeout")
    except Exception as e:
        await bot.send_message(chat_id, f"❌ Error: {e}")

async def append_file_flow(chat_id, path):
    acc = get_account(OWNER_ID)
    ws = get_workspace(OWNER_ID)
    
    try:
        async with bot.conversation(chat_id, timeout=180) as conv:
            await conv.send_message(f"➕ Send text to append to `{path}`:", parse_mode="markdown")
            resp = await conv.get_response()
            append_text = resp.raw_text
            
            data = await gh_get_contents(acc["gh_token"], ws["active_repo"], path)
            if not data or "content" not in data:
                await conv.send_message("❌ Could not fetch file")
                return
            
            try:
                existing = base64.b64decode(data["content"]).decode()
            except:
                existing = ""
            
            sha = data.get("sha")
            new_content = existing.rstrip("\n") + "\n" + append_text
            
            await conv.send_message("⏳ Uploading...")
            result = await gh_put_file(acc["gh_token"], ws["active_repo"], path, new_content, f"Append to {path}", sha)
            
            if result:
                await conv.send_message(f"✅ Appended: `{path}`", parse_mode="markdown")
            else:
                await conv.send_message("❌ Upload failed")
    except asyncio.TimeoutError:
        await bot.send_message(chat_id, "⌛ Timeout")
    except Exception as e:
        await bot.send_message(chat_id, f"❌ Error: {e}")

# ============================================================
# /create_file
# ============================================================
@bot.on(events.NewMessage(pattern=r"^/create_file$"))
@owner_only
async def create_file_handler(event):
    acc = get_account(OWNER_ID)
    if not acc or not acc.get("gh_token"):
        await event.respond("⚠️ No account linked")
        return
    
    ws = get_workspace(OWNER_ID)
    if not ws.get("active_repo"):
        await event.respond("⚠️ No active repo")
        return
    
    chat_id = event.chat_id
    try:
        async with bot.conversation(chat_id, timeout=180) as conv:
            await conv.send_message("📝 Send file path (e.g. `src/main.py`):", parse_mode="markdown")
            resp = await conv.get_response()
            file_path = resp.raw_text.strip()
            
            if not file_path:
                await conv.send_message("❌ Invalid path")
                return
            
            await conv.send_message("✍️ Send content:")
            resp = await conv.get_response()
            content = resp.raw_text
            
            await conv.send_message("⏳ Creating...")
            result = await gh_put_file(acc["gh_token"], ws["active_repo"], file_path, content, f"Create {file_path}")
            
            if result:
                await conv.send_message(f"✅ Created: `{file_path}`", parse_mode="markdown")
            else:
                await conv.send_message("❌ Failed")
    except asyncio.TimeoutError:
        await event.respond("⌛ Timeout")
    except Exception as e:
        await event.respond(f"❌ Error: {e}")

# ============================================================
# /repo_info
# ============================================================
@bot.on(events.NewMessage(pattern=r"^/repo_info$"))
@owner_only
async def repo_info_handler(event):
    acc = get_account(OWNER_ID)
    if not acc or not acc.get("gh_token"):
        await event.respond("⚠️ No account linked")
        return
    
    ws = get_workspace(OWNER_ID)
    repo = ws.get("active_repo")
    
    if not repo:
        await event.respond("⚠️ No active repo")
        return
    
    msg = await event.respond("⏳ Fetching info...")
    data = await gh_get_repo_info(acc["gh_token"], repo)
    
    if not data:
        await msg.edit("❌ Could not fetch repo info")
        return
    
    desc = data.get("description", "No description")
    url = data.get("html_url")
    stars = data.get("stargazers_count", 0)
    forks = data.get("forks_count", 0)
    language = data.get("language", "Unknown")
    topics = ", ".join(data.get("topics", []))
    
    text = (
        f"📦 **{repo}**\n\n"
        f"📝 `{desc}`\n"
        f"⭐ Stars: `{stars}`\n"
        f"🍴 Forks: `{forks}`\n"
        f"💻 Language: `{language}`\n"
        f"🏷️ Topics: `{topics if topics else 'None'}`\n\n"
        f"[🔗 Open Repository]({url})"
    )
    
    await msg.edit(text, parse_mode="markdown")

# ============================================================
# /repo_stats
# ============================================================
@bot.on(events.NewMessage(pattern=r"^/repo_stats$"))
@owner_only
async def repo_stats_handler(event):
    acc = get_account(OWNER_ID)
    if not acc or not acc.get("gh_token"):
        await event.respond("⚠️ No account linked")
        return
    
    ws = get_workspace(OWNER_ID)
    repo = ws.get("active_repo")
    
    if not repo:
        await event.respond("⚠️ No active repo")
        return
    
    msg = await event.respond("⏳ Calculating stats...")
    data = await gh_get_repo_info(acc["gh_token"], repo)
    
    if not data:
        await msg.edit("❌ Could not fetch stats")
        return
    
    stars = data.get("stargazers_count", 0)
    forks = data.get("forks_count", 0)
    watchers = data.get("watchers_count", 0)
    open_issues = data.get("open_issues_count", 0)
    size = data.get("size", 0)
    pushed = data.get("pushed_at", "Unknown")
    
    text = (
        f"📊 **Repository Statistics**\n\n"
        f"⭐ Stars: `{stars}`\n"
        f"🍴 Forks: `{forks}`\n"
        f"👁️ Watchers: `{watchers}`\n"
        f"❌ Open Issues: `{open_issues}`\n"
        f"📦 Size: `{size}KB`\n"
        f"⏱️ Last Push: `{pushed}`"
    )
    
    await msg.edit(text, parse_mode="markdown")

# ============================================================
# MAIN
# ============================================================
async def main():
    threading.Thread(target=run_health_server, daemon=True).start()
    await bot.start(bot_token=BOT_TOKEN)
    print("[+] GitHub Repo Manager Pro Running on Port", PORT)
    await bot.run_until_disconnected()

if __name__ == "__main__":
    loop.run_until_complete(main())
