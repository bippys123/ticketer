import os
import json
import asyncio
import urllib.parse
import aiohttp
from aiohttp import web
import discord
from discord.ext import commands, tasks
from discord import ui, app_commands

TOKEN = os.environ.get("TOKEN")
CLIENT_ID = os.environ.get("CLIENT_ID")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET")
RENDER_URL = os.environ.get("RENDER_URL", "").rstrip("/")

STAFF_ROLE_ID = int(os.environ.get("STAFF_ROLE_ID", 0))
APPROVED_ROLE_ID = int(os.environ.get("APPROVED_ROLE_ID", 0))
APPLICATIONS_CATEGORY_ID = int(os.environ.get("APPLICATIONS_CATEGORY_ID", 0))
SERVER_LOGS_CHANNEL_ID = int(os.environ.get("SERVER_LOGS_CHANNEL_ID", 0))

REDIRECT_URI = f"{RENDER_URL}/callback"
TOKENS_FILE = "user_tokens.json"


def load_tokens() -> dict:
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_user_token(user_id: str, data: dict):
    tokens = load_tokens()
    tokens[user_id] = data
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2)


async def fetch_user_guilds(access_token: str) -> list[str] | None:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get("https://discord.com", headers=headers) as resp:
            if resp.status == 200:
                guilds = await resp.json()
                return [g["name"] for g in guilds]
    return None


async def handle_home(request):
    return web.Response(text="Enlist Bot is online and running!")


async def handle_callback(request):
    code = request.query.get("code")
    if not code:
        return web.Response(text="Missing authorization code.", status=400)

    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    async with aiohttp.ClientSession() as session:
        async with session.post("https://discord.com", data=data, headers=headers) as resp:
            token_json = await resp.json()
            if resp.status != 200:
                return web.Response(text=f"OAuth2 Error: {token_json}", status=400)

        access_token = token_json["access_token"]
        refresh_token = token_json.get("refresh_token")

        auth_headers = {"Authorization": f"Bearer {access_token}"}
        async with session.get("https://discord.com", headers=auth_headers) as user_resp:
            user_json = await user_resp.json()
            user_id = str(user_json["id"])
            username = user_json["username"]

    guilds = await fetch_user_guilds(access_token) or []
    save_user_token(user_id, {
        "username": username,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "guilds": guilds
    })

    return web.Response(
        text="Verification complete! You may now close this tab, return to Discord, and click Enlist / Apply again.",
        content_type="text/plain"
    )


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_home)
    app.router.add_get("/callback", handle_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Web server listening on port {port}")


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(EnlistView())
        self.add_view(ReviewView())
        await self.tree.sync()
        await start_web_server()

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        if not daily_server_check.is_running():
            daily_server_check.start()


bot = Bot()
class EnlistView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Enlist / Apply", style=discord.ButtonStyle.green, custom_id="enlist_button")
    async def enlist_button(self, interaction: discord.Interaction, button: ui.Button):
        try:
            tokens = load_tokens()
            user_id = str(interaction.user.id)

            if user_id not in tokens:
                if not CLIENT_ID or not RENDER_URL:
                    await interaction.response.send_message(
                        "Bot setup error: CLIENT_ID or RENDER_URL is missing in Render Environment Variables.",
                        ephemeral=True
                    )
                    return

                encoded_redirect = urllib.parse.quote(REDIRECT_URI, safe="")
                oauth_url = (
                    f"https://discord.com{CLIENT_ID}"
                    f"&response_type=code&redirect_uri={encoded_redirect}&scope=identify%20guilds"
                )
                view = ui.View()
                view.add_item(ui.Button(label="1. Verify Servers (Required)", url=oauth_url))
                await interaction.response.send_message(
                    "**Step 1:** Click the button below to authorize server verification.\n"
                    "**Step 2:** Once verified, click **Enlist / Apply** again to fill out your application!",
                    view=view,
                    ephemeral=True
                )
                return

            await interaction.response.send_modal(ApplicationModal())
        except Exception as e:
            print(f"Enlist button error: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Error: {e}", ephemeral=True)


class ApplicationModal(ui.Modal, title="Server Enlistment Application"):
    ign = ui.TextInput(label="1. In-Game Name / Username", placeholder="Enter your username...", required=True)
    age = ui.TextInput(label="2. Age", placeholder="e.g., 21", max_length=3, required=True)
    timezone = ui.TextInput(label="3. Timezone / Region", placeholder="e.g., PST, EST, GMT+1", max_length=30, required=True)
    referral = ui.TextInput(label="4. How did you find this server?", placeholder="e.g., Friend, Disboard...", max_length=100, required=True)
    reason = ui.TextInput(
        label="5. Why do you want to join?",
        style=discord.TextStyle.paragraph,
        placeholder="Tell us a bit about yourself and your experience...",
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        staff_role = guild.get_role(STAFF_ROLE_ID)
        category = guild.get_channel(APPLICATIONS_CATEGORY_ID)

        clean_name = "".join(c for c in interaction.user.name.lower() if c.isalnum())
        channel_name = f"application-{clean_name}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            staff_role: discord.PermissionOverwrite(view_channel=True, send_messages=True)
        }

        app_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"Applicant ID: {interaction.user.id}"
        )

        tokens = load_tokens()
        user_data = tokens.get(str(interaction.user.id), {})
        guilds_list = await fetch_user_guilds(user_data.get("access_token", "")) or user_data.get("guilds", [])
        servers_text = ", ".join(guilds_list) if guilds_list else "None found"
        if len(servers_text) > 1000:
            servers_text = servers_text[:995] + "..."

        embed = discord.Embed(
            title=f"New Application: {interaction.user.display_name}",
            description=f"**Applicant:** {interaction.user.mention} (`{interaction.user.id}`)",
            color=discord.Color.blue()
        )
        embed.add_field(name="1. In-Game Name", value=self.ign.value, inline=True)
        embed.add_field(name="2. Age", value=self.age.value, inline=True)
        embed.add_field(name="3. Timezone / Region", value=self.timezone.value, inline=True)
        embed.add_field(name="4. How They Found Us", value=self.referral.value, inline=False)
        embed.add_field(name="5. Reason for Joining", value=self.reason.value, inline=False)
        embed.add_field(name=f"All Servers User Is In ({len(guilds_list)})", value=servers_text, inline=False)

        await app_channel.send(content=f"{staff_role.mention}", embed=embed, view=ReviewView())
        await interaction.response.send_message(
            "Your application has been submitted! Staff will review it shortly.",
            ephemeral=True
        )


class ReviewView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def get_applicant_id(self, channel: discord.TextChannel) -> int | None:
        if channel.topic and "Applicant ID:" in channel.topic:
            return int(channel.topic.split("Applicant ID:").strip())
        return None

    @ui.button(label="Admit", style=discord.ButtonStyle.success, custom_id="admit_app")
    async def admit_button(self, interaction: discord.Interaction, button: ui.Button):
        applicant_id = self.get_applicant_id(interaction.channel)
        member = interaction.guild.get_member(applicant_id)
        approved_role = interaction.guild.get_role(APPROVED_ROLE_ID)

        if member and approved_role:
            await member.add_roles(approved_role)
            try:
                await member.send(f"Congratulations! Your application to **{interaction.guild.name}** has been **approved**.")
            except discord.Forbidden:
                pass

        await interaction.response.send_message(f"Application **admitted** by {interaction.user.mention}. Closing channel in 5 seconds...")
        await asyncio.sleep(5)
        await interaction.channel.delete()

    @ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="deny_app")
    async def deny_button(self, interaction: discord.Interaction, button: ui.Button):
        applicant_id = self.get_applicant_id(interaction.channel)
        member = interaction.guild.get_member(applicant_id)

        if member:
            try:
                await member.send(f"Your application to **{interaction.guild.name}** has been **denied**.")
            except discord.Forbidden:
                pass

        await interaction.response.send_message(f"Application **denied** by {interaction.user.mention}. Closing channel in 5 seconds...")
        await asyncio.sleep(5)
        await interaction.channel.delete()


@bot.tree.command(name="enlist", description="Post the server enlistment application panel in this channel.")
@app_commands.default_permissions(administrator=True)
async def enlist_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Server Enlistment",
        description="Click the button below to verify and fill out an application to join the server.",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, view=EnlistView())


@tasks.loop(hours=24)
async def daily_server_check():
    log_channel = bot.get_channel(SERVER_LOGS_CHANNEL_ID)
    if not log_channel:
        return

    tokens = load_tokens()
    if not tokens:
        return

    report_lines = []
    for user_id, info in tokens.items():
        guilds = await fetch_user_guilds(info.get("access_token", ""))
        if guilds is not None:
            info["guilds"] = guilds
            save_user_token(user_id, info)
        else:
            guilds = info.get("guilds", [])

        report_lines.append(f"- **{info.get('username', user_id)}** (`{user_id}`): {', '.join(guilds) if guilds else 'None'}")

    embed = discord.Embed(
        title="Daily Member Full-Server Report",
        description="\n".join(report_lines)[:4000],
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow()
    )
    await log_channel.send(embed=embed)


@daily_server_check.before_loop
async def before_daily_server_check():
    await bot.wait_until_ready()


bot.run(TOKEN)
