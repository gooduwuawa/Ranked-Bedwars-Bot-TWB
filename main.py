import discord
from discord.ext import commands
from discord.ui import View
import asyncio, time, json, os, random, functools
from dotenv import load_dotenv
from discord import app_commands
import aiohttp

party_group = app_commands.Group(name="party", description="Party commands")

if os.path.exists("linked_accounts.json"):
    with open("linked_accounts.json", "r") as f:
        linked_accounts = json.load(f)
else:
    linked_accounts = {}

def load_hypixel_api_key():
    with open("api.json", "r") as f:
        data = json.load(f)
        return data.get("hypixel_api_key")

pending_tasks = {}  # <- 在檔案頂端定義
last_member_ids = set()
active_countdown = False
queue_countdown_task = None
queue_members_snapshot = []
queue_in_progress = False
queue_task = None
moving_in_progress = False

LINKED_FILE = "linked_accounts.json"
PARTY_SAVE_FILE = "parties.json"
ELO_FILE = "elo.json"

def load_pending():
    return load_json("pending_elo.json")

def load_elo():
    return load_json("elo.json")

def load_json(filename):
    if os.path.exists(filename):
        with open(filename, "r") as f:
            return json.load(f)
    return {}

def save_json(filename, data):
    with open(filename, "w") as f:
        json.dump(data, f, indent=4)

def load_hypixel_api_key():
    with open("api.json", "r") as f:
        data = json.load(f)
        return data.get("hypixel_api_key")

def linked_required():
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(interaction, *args, **kwargs):
            if not os.path.exists(LINKED_FILE):
                await interaction.response.send_message("❌ You need to link your account first using the /link command.", ephemeral=True)
                return
            with open(LINKED_FILE, "r") as f:
                linked_users = json.load(f)
            user_id = str(interaction.user.id)
            if user_id not in linked_users:
                await interaction.response.send_message("❌ You need to link your account first using the /link command.", ephemeral=True)
                return
            return await func(interaction, *args, **kwargs)
        return wrapper
    return decorator

# === CONFIG ===
load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

ALLOWED_TEXT_CHANNEL_ID = 1394929319809388604
VC1_ID = 1394929366198390925
VC2_ID = 1394929400750801007
VC3_ID = 1394929685804351558
VC4_ID = 1394929709703368704
QUEUE_VC_IDS = [1394961454481801367]
QUEUE_VC_ID = 1395300181854912604

INVITE_EXPIRATION = 1800

# === BOT INIT ===
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# === DATA ===
party_data = {}          # key: user_id (int), value: Party instance
linked_accounts = {}     # key: str(user_id), value: minecraft_id (str)
pending_invites = {}     # key: user_id (int), value: (inviter_id (int), timestamp)

class Party:
    def __init__(self, leader_id):
        self.leader_id = leader_id
        self.members = [leader_id]
        self.queued = False
        self.last_activity = time.time()

    def update_activity(self):
        self.last_activity = time.time()

    def to_dict(self):
        return {
            "leader_id": self.leader_id,
            "members": self.members,
            "queued": self.queued,
            "last_activity": self.last_activity
        }

    @staticmethod
    def from_dict(data):
        p = Party(data["leader_id"])
        p.members = data["members"]
        p.queued = data["queued"]
        p.last_activity = data["last_activity"]
        return p

# === SAVE & LOAD ===
def save_parties():
    to_save = {str(uid): party.to_dict() for uid, party in party_data.items() if party.leader_id == uid}
    with open(PARTY_SAVE_FILE, "w") as f:
        json.dump(to_save, f)

def load_parties():
    global party_data
    party_data = {}
    if os.path.exists(PARTY_SAVE_FILE):
        with open(PARTY_SAVE_FILE, "r") as f:
            data = json.load(f)
            for lid, pdata in data.items():
                party = Party.from_dict(pdata)
                for m in party.members:
                    party_data[m] = party

def save_links():
    with open("linked_accounts.json", "w") as f:
        json.dump(linked_accounts, f, indent=4)

def load_links():
    global linked_accounts
    if os.path.exists(LINKED_FILE):
        with open(LINKED_FILE, "r") as f:
            linked_accounts = json.load(f)
    else:
        linked_accounts = {}

# === HELPERS ===
def is_leader(uid): 
    return uid in party_data and party_data[uid].leader_id == uid

def is_in_party(uid): 
    return uid in party_data

def get_party(uid): 
    return party_data.get(uid)

def update_party_data(party: Party):
    for m in party.members:
        party_data[m] = party

# === CLEANUP TASKS ===
async def auto_cleanup_inactive_parties():
    while True:
        now = time.time()
        to_remove = [lid for lid, party in party_data.items() if party.leader_id == lid and now - party.last_activity > 600]
        for lid in to_remove:
            for m in party_data[lid].members:
                party_data.pop(m, None)
        if to_remove:
            save_parties()
        await asyncio.sleep(60)

async def cleanup_expired_invites():
    while True:
        now = time.time()
        for uid in list(pending_invites):
            if now - pending_invites[uid][1] > INVITE_EXPIRATION:
                del pending_invites[uid]
        await asyncio.sleep(60)

# === INVITE VIEW ===
class InviteResponseView(View):
    def __init__(self, inviter_id, invitee_id):
        super().__init__(timeout=INVITE_EXPIRATION)
        self.inviter_id = inviter_id
        self.invitee_id = invitee_id

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green)
    async def accept(self, inter, button):
        if inter.user.id != self.invitee_id:
            return await inter.response.send_message("This invite is not for you.", ephemeral=True)
            # ➕ 檢查是否已綁定 Minecraft 帳號
        if str(inter.user.id) not in linked_accounts:
            return await inter.response.send_message("❌ You must use /link to link your Minecraft account before accepting.", ephemeral=True)
        if self.invitee_id not in pending_invites:
            return await inter.response.edit_message(content="Invite expired.", view=None)
        if is_in_party(self.invitee_id):
            return await inter.response.edit_message(content="You are already in a party.", view=None)
        pid, _ = pending_invites.pop(self.invitee_id)
        if not is_leader(pid):
            return await inter.response.edit_message(content="Party no longer exists.", view=None)
        party = get_party(pid)
        if self.invitee_id not in party.members:
            party.members.append(self.invitee_id)
        update_party_data(party)
        party.update_activity()
        save_parties()
        await inter.response.edit_message(content=f"You joined <@{pid}>'s party!", view=None)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red)
    async def decline(self, inter, button):
        if inter.user.id != self.invitee_id:
            return await inter.response.send_message("This invite is not for you.", ephemeral=True)
            # ➕ 檢查是否已綁定 Minecraft 帳號
        if str(inter.user.id) not in linked_accounts:
            return await inter.response.send_message("❌ You must use /link to link your Minecraft account before accepting.", ephemeral=True)
        pending_invites.pop(self.invitee_id, None)
        await inter.response.edit_message(content="Declined the invite.", view=None)

# === COMMANDS ===
party_group = app_commands.Group(name="party", description="Party system")

async def on_game_end(party):
    guild = bot.get_guild(1404001303872671775)  # 替換成你的伺服器ID (int)
    final_vc = guild.get_channel(1404001305248141405)

    for vc_id in getattr(party, "temp_vcs", []):
        vc = guild.get_channel(vc_id)
        if vc:
            for member in vc.members:
                await member.move_to(final_vc)
            await vc.delete()

    party.temp_vcs = []
    save_parties()

@tree.command(name="leaderboard", description="Show the top 10 players by Elo")
@linked_required()
async def leaderboard(inter):
    await inter.response.defer(ephemeral=True)

    elo_data = load_json("elo.json")  # Load your Elo database
    if not elo_data:
        return await inter.followup.send("No Elo data available.")

    # ✅ Filter out users with 0 Elo
    filtered_elo = {k: v for k, v in elo_data.items() if v > 0}

    if not filtered_elo:
        return await inter.followup.send("No players with Elo above 0.")

# Sort players by Elo (descending)
    sorted_elo = sorted(filtered_elo.items(), key=lambda x: x[1], reverse=True)
    top_10 = sorted_elo[:10]
    description = ""                         
    for rank, (uid, elo_score) in enumerate(top_10, start=1):
        username = linked_accounts.get(uid, f"User {uid}")
    description += f"**#{rank}** – {username}: {elo_score} Elo\n"

    embed = discord.Embed(
        title="🏆 Top 10 Elo Leaderboard",
        description=description,
        color=discord.Color.gold()
    )
    await inter.followup.send(embed=embed)

@tree.command(name="claim", description="Claim your pending Elo reward after a game")
@linked_required()
async def claim(inter):
    await inter.response.defer(ephemeral=True)

    uid = str(inter.user.id)
    linked_mc = linked_accounts.get(uid)
    if not linked_mc:
        return await inter.followup.send("❌ You have not linked a Minecraft account.")

    pending_elo = load_json("pending_elo.json")
    elo_data = load_json("elo.json")

    if uid not in pending_elo or not pending_elo[uid]:
        return await inter.followup.send("❌ You have no pending Elo to claim.")

    hypixel_key = load_hypixel_api_key()

    try:
        async with aiohttp.ClientSession() as session:
            # Step 1: Fetch UUID from Mojang
            async with session.get(f"https://api.mojang.com/users/profiles/minecraft/{linked_mc}") as r:
                if r.status != 200:
                    return await inter.followup.send("❌ Failed to fetch UUID from Mojang.")
                uuid_data = await r.json()
                uuid = uuid_data["id"]

            # Step 2: Fetch Hypixel stats
            async with session.get(f"https://api.hypixel.net/player?key={hypixel_key}&uuid={uuid}") as r:
                if r.status != 200:
                    return await inter.followup.send("❌ Failed to fetch Hypixel stats.")
                player_data = await r.json()
                stats = player_data.get("player", {}).get("stats", {}).get("Bedwars", {})
                kills = stats.get("kills_bedwars", 0)
                finals = stats.get("final_kills_bedwars", 0)
    except Exception as e:
        return await inter.followup.send(f"❌ An error occurred while checking stats: {e}")

    # Step 3: Evaluate rewards
    reward_elo = 0
    remaining_tasks = []
    for entry in pending_elo[uid]:
        required_kills = entry.get("expected_kills", 0)
        required_finals = entry.get("expected_finals", 0)
        elo_change = entry.get("elo_change", 0)

        if kills >= required_kills and finals >= required_finals:
            reward_elo += elo_change
        else:
            remaining_tasks.append(entry)

    if reward_elo == 0:
        return await inter.followup.send(
            "❌ No rewards available to claim (your stats might not be updated yet, please try again later)."
        )

    # Step 4: Update Elo and task list
    elo_data[uid] = elo_data.get(uid, 0) + reward_elo
    pending_elo[uid] = remaining_tasks

    save_json("elo.json", elo_data)
    save_json("pending_elo.json", pending_elo)

    await inter.followup.send(
        f"✅ Successfully claimed {reward_elo} Elo!\n🏆 Your new Elo: {elo_data[uid]}"
    )

@tree.command(name="elo", description="Check your current ELO rating")
@linked_required()
async def elo(inter):
    uid = str(inter.user.id)
    username = linked_accounts.get(uid)
    elo_data = load_json("elo.json")  # ← 用你前面寫的 load_json 方法
    elo_score = elo_data.get(uid, 0)  # 用 Discord ID 當 key
    await inter.response.send_message(f"🏆 {username}'s current ELO: {elo_score}", ephemeral=True)

@tree.command(name="link", description="Link your Discord to a Minecraft username")
@app_commands.describe(minecraft_id="Your Minecraft name")
async def link(inter, minecraft_id: str):
    uid = str(inter.user.id)

    if len(minecraft_id) <= 3:
        await inter.response.send_message("❌ Minecraft username must be more than 3 characters.", ephemeral=True)
        return

    if uid in linked_accounts:
        await inter.response.send_message(
            f"❌ You have already linked to {linked_accounts[uid]}.\nUse /unlink first if you want to relink.",
            ephemeral=True
        )
        return

    linked_accounts[uid] = minecraft_id
    save_links()
    await inter.response.send_message(f"✅ Successfully linked to {minecraft_id}.", ephemeral=True)

@tree.command(name="unlink", description="Unlink your Minecraft account")
@linked_required()
async def unlink(inter):
    uid = str(inter.user.id)
    if uid in linked_accounts:
        del linked_accounts[uid]
        save_links()
        await inter.response.send_message("Unlinked your Minecraft account.", ephemeral=True)
    else:
        await inter.response.send_message("You have no linked account.", ephemeral=True)

@party_group.command(name="invite", description="Invite a user to your party")
@app_commands.describe(user="The user to invite to your party")
@linked_required()
async def invite(inter, user: discord.User):
    if inter.channel_id != ALLOWED_TEXT_CHANNEL_ID:
        return await inter.response.send_message("Wrong channel.", ephemeral=True)

    inviter, invitee = inter.user.id, user.id
    if inviter == invitee:
        return await inter.response.send_message("You can't invite yourself.", ephemeral=True)
    if is_in_party(invitee):
        return await inter.response.send_message("That user is already in a party.", ephemeral=True)
    if invitee in pending_invites:
        return await inter.response.send_message("That user already has a pending invite.", ephemeral=True)

    if not is_in_party(inviter):
        party = Party(inviter)
        update_party_data(party)
    else:
        if not is_leader(inviter):
            return await inter.response.send_message("Only the party leader can invite.", ephemeral=True)
        party = get_party(inviter)

    pending_invites[invitee] = (inviter, time.time())
    view = InviteResponseView(inviter, invitee)
    embed = discord.Embed(
        title="Party Invitation",
        description=f"{inter.user.mention} invited {user.mention}\nUse /party accept or click below\nExpires in 3 minutes.",
        color=discord.Color.purple()
    )
    await inter.response.send_message(f"✅ Sent invite to {user.mention}", ephemeral=True)
    await inter.channel.send(content=user.mention, embed=embed, view=view)

@party_group.command(name="accept", description="Accept a pending party invite")
@linked_required()
async def accept(inter):
    uid = inter.user.id
    if uid not in pending_invites:
        return await inter.response.send_message("You have no pending invites.", ephemeral=True)
    inviter_id, sent = pending_invites.pop(uid)
    if time.time() - sent > INVITE_EXPIRATION:
        return await inter.response.send_message("Invite expired.", ephemeral=True)
    if is_in_party(uid):
        return await inter.response.send_message("You are already in a party.", ephemeral=True)
    if not is_leader(inviter_id):
        return await inter.response.send_message("Invalid party.", ephemeral=True)

    party = get_party(inviter_id)
    if uid not in party.members:
        party.members.append(uid)
    update_party_data(party)
    party.update_activity()
    save_parties()
    await inter.response.send_message(f"You joined {inter.guild.get_member(inviter_id).display_name}'s party!")

@party_group.command(name="leave", description="Leave your current party")
@linked_required()
async def leave(inter):
    uid = inter.user.id
    if not is_in_party(uid):
        return await inter.response.send_message("You are not in a party.", ephemeral=True)
    party = get_party(uid)
    if is_leader(uid):
        for m in party.members:
            party_data.pop(m, None)
        await inter.response.send_message("You disbanded the party.")
    else:
        party.members.remove(uid)
        party_data.pop(uid, None)
        await inter.response.send_message("You left the party.")
    save_parties()

@party_group.command(name="queue", description="Re-split party members currently in the queue VC")
@linked_required()
async def queue(inter):
    if not linked_required()(inter):
        return await inter.response.send_message("Please use /link first.", ephemeral=True)

    uid = inter.user.id
    if not is_leader(uid):
        return await inter.response.send_message("Only the leader can queue.", ephemeral=True)

    party = get_party(uid)
    if not party:
        return await inter.response.send_message("You are not in a party.", ephemeral=True)

    member_count = len(party.members)
    if member_count not in [6, 8]:
        return await inter.response.send_message("Party must have exactly 6 or 8 members to queue.", ephemeral=True)

    queue_channel_id = QUEUE_VC_IDS[0]
    queue_channel = inter.guild.get_channel(queue_channel_id)
    if not queue_channel:
        return await inter.response.send_message("Queue voice channel not found.", ephemeral=True)

    members_in_queue = [
        mid for mid in party.members
        if (member := inter.guild.get_member(mid)) and member.voice and member.voice.channel and member.voice.channel.id == queue_channel_id
    ]

    if len(members_in_queue) < 2:
        return await inter.response.send_message("Not enough party members are currently in the queue voice channel.", ephemeral=True)

    target_vcs = [VC1_ID, VC2_ID]

    for i, mid in enumerate(members_in_queue):
        member = inter.guild.get_member(mid)
        if member:
            await member.move_to(inter.guild.get_channel(target_vcs[i % len(target_vcs)]))

    # 取得 Minecraft 名稱，分批傳送，每批最多4人
    mc_names = [linked_accounts.get(str(mid)) for mid in members_in_queue if str(mid) in linked_accounts]

    if mc_names:
        target_channel = inter.guild.get_channel(1394937257474920541)
        if target_channel:
            batch_size = 4
            for i in range(0, len(mc_names), batch_size):
                batch = mc_names[i:i+batch_size]
            await target_channel.send(f"/p {' '.join(batch)}")
        else:
            await inter.channel.send("⚠️ Cannot find the specified channel, unable to send the command.")
    else:
        await inter.channel.send("⚠️ No linked Minecraft accounts found for members in the queue voice channel.")

    party.update_activity()
    save_parties()
    await inter.response.send_message(f"🔁 Requeued {len(members_in_queue)} members currently in the queue voice channel.", ephemeral=True)

@party_group.command(name="forcequeue", description="Forcefully re-split party members into new VCs")
@linked_required()
async def forcequeue(inter: discord.Interaction):
    uid = inter.user.id

    if not is_leader(uid):
        return await inter.response.send_message("Only the leader can use this.", ephemeral=True)

    party = get_party(uid)
    if not party:
        return await inter.response.send_message("You are not in a party.", ephemeral=True)

    queue_channel_id = QUEUE_VC_IDS[0]
    queue_channel = inter.guild.get_channel(queue_channel_id)
    if not queue_channel:
        return await inter.response.send_message("Queue voice channel not found.", ephemeral=True)

    members_in_queue = [
        mid for mid in party.members
        if (member := inter.guild.get_member(mid)) and member.voice and member.voice.channel and member.voice.channel.id == queue_channel_id
    ]

    if len(members_in_queue) < 2:
        return await inter.response.send_message("Not enough party members are currently in the queue voice channel.", ephemeral=True)

    # Create random code for VC names
    random_code = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

    # Create two temporary VCs under the given category
    category = inter.guild.get_channel(1404001305248141403)
    red_vc = await inter.guild.create_voice_channel(f"red-{random_code}", category=category)
    green_vc = await inter.guild.create_voice_channel(f"green-{random_code}", category=category)

    # Randomly shuffle and split members
    random.shuffle(members_in_queue)
    half = len(members_in_queue) // 2
    red_members = members_in_queue[:half]
    green_members = members_in_queue[half:]

    # Move members to the new VCs
    for mid in red_members:
        member = inter.guild.get_member(mid)
        if member:
            await member.move_to(red_vc)
    for mid in green_members:
        member = inter.guild.get_member(mid)
        if member:
            await member.move_to(green_vc)

    # Send /p command in MC linked channel
    mc_names = [linked_accounts.get(str(mid)) for mid in members_in_queue if str(mid) in linked_accounts]
    if mc_names:
        target_channel = inter.guild.get_channel(1394937257474920541)
        if target_channel:
            batch_size = 4
            for i in range(0, len(mc_names), batch_size):
                batch = mc_names[i:i+batch_size]
                await target_channel.send(f"`/p {' '.join(batch)}`")
        else:
            await inter.channel.send("⚠️ Cannot find the target channel to send the command.")
    else:
        await inter.channel.send("⚠️ No linked Minecraft accounts found.")

    party.queued = True
    save_parties()

    # Store temp VC IDs in party object for cleanup after game ends
    party.temp_vcs = [red_vc.id, green_vc.id]
    save_parties()

    await inter.response.send_message(f"🔁 Created temporary VCs and moved {len(members_in_queue)} members.", ephemeral=True)

@bot.event
async def on_voice_state_update(member, before, after):
    if (before.channel and before.channel.id == QUEUE_VC_ID) or (after.channel and after.channel.id == QUEUE_VC_ID):
        await handle_queue_vc_update(member.guild)

async def handle_queue_vc_update(guild):
    global active_countdown, last_member_ids

    queue_vc = guild.get_channel(QUEUE_VC_ID)
    if not queue_vc:
        return

    current_ids = {m.id for m in queue_vc.members}
    if current_ids == last_member_ids:
        return
    last_member_ids = current_ids

    count = len(current_ids)
    if count < 6:
        if active_countdown:
            active_countdown.cancel()
            active_countdown = None
        return

    if active_countdown:
        active_countdown.cancel()

    async def countdown_and_move():
        try:
            await asyncio.sleep(5)

            # 再次確認當前成員
            current_members = list(queue_vc.members)
            current_count = len(current_members)
            if current_count < 6:
                return

            if current_count <= 7:
                move_count = 6
                targets = [VC3_ID, VC4_ID]
            else:
                move_count = 8
                targets = [VC1_ID, VC2_ID]

            selected = random.sample(current_members, move_count)
            target_vcs = [guild.get_channel(tid) for tid in targets]

            for i, m in enumerate(selected):
                await m.move_to(target_vcs[i % 2])

            # 傳送 /p 指令
            mc_names = [linked_accounts.get(str(m.id)) for m in selected if str(m.id) in linked_accounts]
            text_channel = guild.get_channel(1394937257474920541)
            if text_channel and mc_names:
                half = len(mc_names) // 2
                group1 = mc_names[:half]
                group2 = mc_names[half:]
                if group1:
                    await text_channel.send(f"/p {' '.join(group1)}")
                if group2:
                    await text_channel.send(f"/p {' '.join(group2)}")

        except asyncio.CancelledError:
            pass

    active_countdown = asyncio.create_task(countdown_and_move())

@party_group.command(name="requeue", description="Re-split the party again into voice channels")
@linked_required()
async def requeue(inter):
    uid = inter.user.id
    if not is_leader(uid):
        return await inter.response.send_message("Only the party leader can requeue.", ephemeral=True)

    party = get_party(uid)
    if not party.queued:
        return await inter.response.send_message("You must /party queue or /party forcequeue first.", ephemeral=True)

    allowed_vc_ids = {VC1_ID, VC2_ID, VC3_ID, VC4_ID}
    members_in_vc = []

    for mid in party.members:
        member = inter.guild.get_member(mid)
        if member and member.voice and member.voice.channel and member.voice.channel.id in allowed_vc_ids:
            members_in_vc.append(mid)

    if len(members_in_vc) < 2:
        return await inter.response.send_message("Not enough party members are currently in VC1–VC4.", ephemeral=True)

    random.shuffle(members_in_vc)
    vcs = [VC1_ID, VC2_ID]
    for i, mid in enumerate(members_in_vc):
        member = inter.guild.get_member(mid)
        if member:
            await member.move_to(inter.guild.get_channel(vcs[i % 2]))

    mc_names = [linked_accounts.get(str(mid)) for mid in members_in_vc if str(mid) in linked_accounts]

    if mc_names:
        target_channel = inter.guild.get_channel(1394937257474920541)
        if target_channel:
            batch_size = 4
            for i in range(0, len(mc_names), batch_size):
                batch = mc_names[i:i+batch_size]
        else:
            await inter.channel.send("⚠️ Cannot find the specified channel, unable to send the command.")
    else:
        await inter.channel.send("⚠️ No members in VC have linked their Minecraft account.")

    party.update_activity()
    save_parties()
    await inter.response.send_message("🔁 Requeued only members currently in VC1~VC4.", ephemeral=True)

@party_group.command(name="disband", description="Disband the party (only leader can do this)")
@linked_required()
async def disband(inter):
    uid = inter.user.id
    if not is_leader(uid):
        return await inter.response.send_message("Only the leader can disband the party.", ephemeral=True)
    for m in get_party(uid).members:
        party_data.pop(m, None)
    save_parties()
    await inter.response.send_message("Party disbanded.")

@party_group.command(name="kick", description="Kick a member from your party")
@app_commands.describe(user="The user to kick from your party")
@linked_required()
async def kick(inter, user: discord.User):
    uid = inter.user.id
    if not is_leader(uid):
        return await inter.response.send_message("Only the leader can kick members.", ephemeral=True)
    party = get_party(uid)
    if user.id not in party.members:
        return await inter.response.send_message("That user is not in your party.", ephemeral=True)
    if user.id == uid:
        return await inter.response.send_message("You can't kick yourself.", ephemeral=True)
    party.members.remove(user.id)
    party_data.pop(user.id, None)
    save_parties()
    await inter.response.send_message(f"Kicked {user.display_name} from the party.")

@party_group.command(name="promote", description="Promote another member to party leader")
@app_commands.describe(user="The member to promote to leader")
@linked_required()
async def promote(inter, user: discord.User):
    uid = inter.user.id
    if not is_leader(uid):
        return await inter.response.send_message("Only the leader can promote.", ephemeral=True)
    party = get_party(uid)
    if user.id not in party.members:
        return await inter.response.send_message("That user is not in your party.", ephemeral=True)
    party.leader_id = user.id
    update_party_data(party)
    save_parties()
    await inter.response.send_message(f"Promoted {user.display_name} to party leader.")

@tree.command(name="setelo", description="Set a player's ELO manually (admin only)")
@app_commands.describe(user="The user whose ELO to set", value="The ELO value to set")
async def setelo(inter: discord.Interaction, user: discord.User, value: int):
    # 只有指定管理員才能用（你可以改成你自己的 ID）
    if inter.user.id != 792326325050146816:
        return await inter.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)

    uid = str(user.id)
    elo_data = load_json("elo.json")

    elo_data[uid] = value
    save_json("elo.json", elo_data)

    await inter.response.send_message(f"✅ Set {user.display_name}'s Elo to {value}.")

@party_group.command(name="list", description="List all members in your current party")
@linked_required()
async def list_members(inter):
    # 延遲回覆，避免逾時
    await inter.response.defer(ephemeral=True)

    uid = inter.user.id
    if not is_in_party(uid):
        return await inter.followup.send("You are not in a party.")

    party = get_party(uid)
    members = party.members
    names = []

    for m in members:
        member = inter.guild.get_member(m)
        if member:
            names.append(member.display_name)
        else:
            try:
                user = await inter.client.fetch_user(m)
                names.append(user.name)
            except Exception:
                names.append(f"Unknown({m})")

    if not names:
        await inter.followup.send("Party is empty.")
    else:
        # 先嘗試直接訊息清單，長度限制可自行調整
        msg = "Party members: " + ", ".join(names)
        await inter.followup.send(msg)

@tree.command(name="help", description="Show all available commands")
async def help_cmd(inter):
    help_text = (
    "**🔗 Account Commands**\n"
    "/link <ID> – Link your Minecraft account\n"
    "/unlink – Unlink your account\n\n"
    "**🎉 Party Commands**\n"
    "/party invite <user> – Invite a user to your party\n"
    "/party accept – Accept a party invite\n"
    "/party leave – Leave your current party\n"
    "/party disband – Disband your party\n"
    "/party kick <user> – Kick a member from the party\n"
    "/party promote <user> – Promote a member to leader\n"
    "/party list – List all members in your party"
    )
    await inter.response.send_message(help_text, ephemeral=True)

@tree.command(name="ping", description="Check if the bot is alive")
async def ping(inter):
    await inter.response.send_message("Pong!")

@bot.event
async def on_voice_state_update(member, before, after):
    global queue_task, last_member_ids, moving_in_progress

    if moving_in_progress:
        return  # Don't start a new countdown while moving is in progress

    if (before.channel and before.channel.id == QUEUE_VC_ID) or (after.channel and after.channel.id == QUEUE_VC_ID):
        vc = bot.get_channel(QUEUE_VC_ID)
        current_ids = {m.id for m in vc.members}

        if len(current_ids) < 6:
            if queue_task and not queue_task.done():
                queue_task.cancel()
                queue_task = None
            return

        if queue_task and not queue_task.done():
            queue_task.cancel()

        # Save snapshot of current members
        last_member_ids = current_ids.copy()
        queue_task = asyncio.create_task(queue_countdown_and_move())

async def queue_countdown_and_move():
    global queue_task, moving_in_progress, last_member_ids

    try:
        countdown = 5
        vc = bot.get_channel(QUEUE_VC_ID)

        while countdown > 0:
            await asyncio.sleep(1)
            current_ids = {m.id for m in vc.members}

            if current_ids != last_member_ids:
                last_member_ids = current_ids.copy()
                countdown = 5
                continue

            countdown -= 1

        members = list(vc.members)
        count = len(members)

        if count < 6:
            return

        if count in [6, 7]:
            move_count = 6
            targets = [bot.get_channel(VC3_ID), bot.get_channel(VC4_ID)]
        else:
            move_count = min(8, count)
            targets = [bot.get_channel(VC1_ID), bot.get_channel(VC2_ID)]

        selected = random.sample(members, move_count)
        moving_in_progress = True

        for i, m in enumerate(selected):
            try:
                await m.move_to(targets[i % 2])
            except Exception as e:
                print(f"Error moving {m.display_name}: {e}")

        # ✅ 以下內容保持在 async 函數內
        text_channel = bot.get_channel(1394937257474920541)

        mc_names = [
            linked_accounts.get(str(m.id))
            for m in selected
            if str(m.id) in linked_accounts and linked_accounts[str(m.id)]
        ]

        mc_names = [name for name in mc_names if name]

        if text_channel is None:
            print("❌ Could not find the target text channel.")
        elif not mc_names:
            print("❌ No linked Minecraft usernames found.")
        else:
            half = len(mc_names) // 2
            group1 = mc_names[:half]
            group2 = mc_names[half:]
            if group1:
                await text_channel.send(f"/p {''.join(group1)}")
                print(f"✅ Sent /p command for group 1: {group1}")
            if group2:
                await text_channel.send(f"/p {''.join(group2)}")
                print(f"✅ Sent /p command for group 2: {group2}")

    except asyncio.CancelledError:
        print("Countdown was cancelled due to voice state change.")
    finally:
        moving_in_progress = False
        queue_task = None

@bot.event
async def on_ready():
    bot.tree.add_command(party_group)
    # 立即同步到測試伺服器
    await bot.tree.sync()
    load_parties()
    load_links()
    load_pending()
    load_elo()
    bot.loop.create_task(auto_cleanup_inactive_parties())
    bot.loop.create_task(cleanup_expired_invites())
    print("Bot is ready.")

bot.run(TOKEN)