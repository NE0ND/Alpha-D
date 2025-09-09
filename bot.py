import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import random
import aiosqlite
import asyncio
import datetime
from discord import app_commands
from threading import Thread
from flask import Flask

load_dotenv()  # load .env

# Game state tracking for interactive Russian Roulette
active_games = {}

TOKEN = os.getenv('DISCORD_TOKEN')
OWNER_ID_STR = os.getenv('OWNER_ID')
if OWNER_ID_STR is None:
    print("Error: OWNER_ID environment variable not found!")
    exit(1)
OWNER_ID = int(OWNER_ID_STR)

MUTE_ROLE_NAME = "Muted"
DB_PATH = "userdata.db"

intents = discord.Intents.none()
intents.guilds = True
intents.guild_messages = True
# Aktifkan setelah enable privileged intents di developer portal
intents.message_content = True


class MyBot(commands.Bot):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # self.tree = discord.app_commands.CommandTree(self)  # HAPUS baris ini


bot = MyBot(command_prefix='!', intents=intents, help_command=None)


# Inisialisasi database
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Users table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 100,
                vip INTEGER NOT NULL DEFAULT 0
            )
        """)

        # Inventory table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                item_category TEXT NOT NULL,
                item_value INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        """)

        # Usage tracking table for daily limits
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_usage (
                user_id INTEGER PRIMARY KEY,
                cari_count INTEGER NOT NULL DEFAULT 0,
                last_reset DATE NOT NULL DEFAULT (date('now')),
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        """)
        await db.commit()


async def get_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT balance, vip FROM users WHERE user_id = ?", (user_id, ))
        row = await cursor.fetchone()
        if row is None:
            await db.execute(
                "INSERT INTO users (user_id, balance, vip) VALUES (?, 100, 0)",
                (user_id, ))
            await db.commit()
            return 100, False
        balance, vip = row
        return balance, bool(vip)


async def update_user(user_id, balance=None, vip=None):
    async with aiosqlite.connect(DB_PATH) as db:
        if balance is not None and vip is not None:
            await db.execute(
                "UPDATE users SET balance = ?, vip = ? WHERE user_id = ?",
                (balance, int(vip), user_id))
        elif balance is not None:
            await db.execute("UPDATE users SET balance = ? WHERE user_id = ?",
                             (balance, user_id))
        elif vip is not None:
            await db.execute("UPDATE users SET vip = ? WHERE user_id = ?",
                             (int(vip), user_id))
        await db.commit()


# Inventory management functions
async def add_to_inventory(user_id, item_name, item_category, item_value, quantity=1):
    """Add item to user's inventory"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Check if item already exists
        cursor = await db.execute(
            "SELECT quantity FROM inventory WHERE user_id = ? AND item_name = ?",
            (user_id, item_name))
        row = await cursor.fetchone()

        if row:
            # Update quantity if item exists
            new_quantity = row[0] + quantity
            await db.execute(
                "UPDATE inventory SET quantity = ? WHERE user_id = ? AND item_name = ?",
                (new_quantity, user_id, item_name))
        else:
            # Add new item
            await db.execute(
                "INSERT INTO inventory (user_id, item_name, item_category, item_value, quantity) VALUES (?, ?, ?, ?, ?)",
                (user_id, item_name, item_category, item_value, quantity))
        await db.commit()


async def get_inventory(user_id):
    """Get user's inventory"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT item_name, item_category, item_value, quantity FROM inventory WHERE user_id = ? ORDER BY item_category, item_name",
            (user_id,))
        return await cursor.fetchall()


async def get_inventory_count(user_id):
    """Get total item count in inventory"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT SUM(quantity) FROM inventory WHERE user_id = ?", (user_id,))
        result = await cursor.fetchone()
        return result[0] if result[0] else 0


async def remove_from_inventory(user_id, item_name, quantity=1):
    """Remove item from inventory"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT quantity FROM inventory WHERE user_id = ? AND item_name = ?",
            (user_id, item_name))
        row = await cursor.fetchone()

        if not row:
            return False  # Item not found

        current_quantity = row[0]
        if current_quantity < quantity:
            return False  # Not enough items

        new_quantity = current_quantity - quantity
        if new_quantity == 0:
            # Remove item completely
            await db.execute(
                "DELETE FROM inventory WHERE user_id = ? AND item_name = ?",
                (user_id, item_name))
        else:
            # Update quantity
            await db.execute(
                "UPDATE inventory SET quantity = ? WHERE user_id = ? AND item_name = ?",
                (new_quantity, user_id, item_name))

        await db.commit()
        return True


# Daily usage tracking functions
async def get_daily_usage(user_id):
    """Get user's daily usage count for !cari"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Check if we need to reset (new day)
        cursor = await db.execute(
            "SELECT cari_count, last_reset FROM daily_usage WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()

        today = datetime.date.today().isoformat()

        if not row:
            # Create new record for user
            await db.execute(
                "INSERT INTO daily_usage (user_id, cari_count, last_reset) VALUES (?, 0, ?)",
                (user_id, today))
            await db.commit()
            return 0

        cari_count, last_reset = row

        # Reset count if it's a new day
        if last_reset != today:
            await db.execute(
                "UPDATE daily_usage SET cari_count = 0, last_reset = ? WHERE user_id = ?",
                (today, user_id))
            await db.commit()
            return 0

        return cari_count


async def increment_daily_usage(user_id):
    """Increment user's daily !cari usage count"""
    async with aiosqlite.connect(DB_PATH) as db:
        today = datetime.date.today().isoformat()

        # Insert or update usage count
        await db.execute("""
            INSERT INTO daily_usage (user_id, cari_count, last_reset) 
            VALUES (?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET 
            cari_count = cari_count + 1,
            last_reset = ?
        """, (user_id, today, today))
        await db.commit()


@bot.event
async def on_ready():
    await init_db()
    print(f'Bot sudah online sebagai {bot.user}')

    # Set bot status to online with activity
    activity = discord.Game(name="/help untuk bantuan")
    await bot.change_presence(status=discord.Status.online, activity=activity)
    await bot.tree.sync()
    print("Bot status set to online dengan regular commands dan slash commands")


# Event on_member_join memerlukan members intent yang privileged
# Dinonaktifkan untuk sementara
# @bot.event
# async def on_member_join(member):
#     channel = member.guild.system_channel
#     if channel:
#         await channel.send(f"Selamat datang di server, {member.mention}!")


@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason=None):
    try:
        await member.kick(reason=reason)
        await ctx.send(f"{member} telah di-kick. Alasan: {reason}")
    except Exception as e:
        await ctx.send(f"Gagal kick: {e}")


@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason=None):
    try:
        await member.ban(reason=reason)
        await ctx.send(f"{member} telah di-ban. Alasan: {reason}")
    except Exception as e:
        await ctx.send(f"Gagal ban: {e}")


@bot.command()
@commands.has_permissions(manage_roles=True)
async def mute(ctx, member: discord.Member, *, reason=None):
    guild = ctx.guild
    mute_role = discord.utils.get(guild.roles, name=MUTE_ROLE_NAME)
    if not mute_role:
        mute_role = await guild.create_role(name=MUTE_ROLE_NAME)
        for channel in guild.channels:
            await channel.set_permissions(mute_role,
                                          speak=False,
                                          send_messages=False,
                                          read_message_history=True,
                                          read_messages=False)
    try:
        await member.add_roles(mute_role, reason=reason)
        await ctx.send(f"{member} telah di-mute. Alasan: {reason}")
    except Exception as e:
        await ctx.send(f"Gagal mute: {e}")


@bot.command()
async def vip(ctx, member: discord.Member = None):
    """Berikan status VIP (owner only) - !vip @user"""
    if member is None:
        await ctx.send(
            "❌ **Error:** Tag user yang mau dikasih VIP!\n📝 **Contoh:** `!vip @username`"
        )
        return

    if ctx.author.id != OWNER_ID:
        await ctx.send(
            "🔒 **Akses ditolak!** Hanya owner bot yang bisa menambahkan VIP.")
        return
    balance, _ = await get_user(member.id)
    await update_user(member.id, balance=balance, vip=True)
    await ctx.send(
        f"💎 **VIP GRANTED!** {member.mention} sekarang adalah VIP!\n🎉 **Selamat!** Kamu bisa akses semua fitur premium!"
    )


@bot.command()
async def ping(ctx):
    """Test bot responsif"""
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! Latency: {latency}ms\nBot online dan berfungsi!")


@bot.command()
async def cari(ctx):
    """Cari barang di tong sampah - !cari"""
    balance, vip = await get_user(ctx.author.id)

    # Check daily usage limits
    current_usage = await get_daily_usage(ctx.author.id)

    # Determine usage limit based on user status
    if ctx.author.id == OWNER_ID:
        # Owner has unlimited usage
        daily_limit = float('inf')
        status_text = "👑 **OWNER** (Unlimited)"
    elif vip:
        daily_limit = 50
        status_text = "💎 **VIP** (50/hari)"  
    else:
        daily_limit = 25
        status_text = "👤 **Regular** (25/hari)"

    # Check if user has reached daily limit
    if current_usage >= daily_limit:
        await ctx.send(
            f"⏰ **Daily limit tercapai!** ({current_usage}/{int(daily_limit)})\n\n"
            f"📊 **Status:** {status_text}\n"
            f"🔄 **Reset:** Besok jam 00:00 WIB\n\n"
            f"💡 **Upgrade ke VIP untuk limit 50x/hari:** `!vip @{ctx.author.name}`"
        )
        return

    # Check inventory capacity
    current_items = await get_inventory_count(ctx.author.id)
    max_capacity = 25 if vip else 15

    if current_items >= max_capacity:
        await ctx.send(f"🗑️ **Inventori penuh!** ({current_items}/{max_capacity})\n💡 **Tip:** Gunakan `!sell [barang]` untuk jual barang terlebih dahulu")
        return

    # Define possible items found in trash with NEW RARITIES
    trash_items = {
        # Trash items (auto-deleted, no value)
        "trash": [
            ("Makanan Busuk", 0), ("Sayuran Basi", 0), ("Roti Berjamur", 0),
            ("Daging Busuk", 0), ("Buah Busuk", 0), ("Nasi Basi", 0)
        ],

        # Recyclable items (5-20 value)
        "recyclable": [
            ("Botol Plastik", random.randint(5, 20)), ("Kaleng Soda", random.randint(5, 20)),
            ("Kardus Bekas", random.randint(5, 20)), ("Koran Lama", random.randint(5, 20)),
            ("Botol Kaca", random.randint(5, 20)), ("Plastik Kemasan", random.randint(5, 20)),
            ("Kertas Bekas", random.randint(5, 20)), ("Kantong Plastik", random.randint(5, 20))
        ],

        # Electronics (50-90 value) - rare
        "electronics": [
            ("HP Rusak", random.randint(50, 90)), ("Kabel USB", random.randint(50, 90)),
            ("Headphone Bekas", random.randint(50, 90)), ("Charger Lama", random.randint(50, 90)),
            ("Remote Rusak", random.randint(50, 90)), ("Baterai Bekas", random.randint(50, 90)),
            ("Flashdisk Rusak", random.randint(50, 90))
        ],

        # NEW: Legendary items (very rare, high value)
        "legendary": [
            ("Silver Ring", random.randint(120, 150)), ("Silver Chain", random.randint(120, 150)),
            ("Gold Coin", random.randint(155, 200)), ("Gold Bracelet", random.randint(155, 200)),
            ("Diamond Earring", random.randint(210, 260)), ("Diamond Ring", random.randint(210, 260))
        ],

        # NEW: Mythical meme items (ultra rare, viral content)
        "mythical": [
            ("🐸 Pepe Sticker", random.randint(300, 500)), ("😎 Chad Sticker", random.randint(300, 500)),
            ("🐕 Doge Sticker", random.randint(300, 500)), ("🚀 To The Moon Sticker", random.randint(300, 500)),
            ("🔥 This is Fine Sticker", random.randint(300, 500)), ("💎 Diamond Hands Sticker", random.randint(300, 500)),
            ("🌙 Mooning Sticker", random.randint(300, 500)), ("⚡ Sigma Grindset Sticker", random.randint(300, 500)),
            ("🎮 Among Us Sticker", random.randint(300, 500)), ("🍌 Minion Sticker", random.randint(300, 500))
        ]
    }

    # NEW PROBABILITY SYSTEM with all rarities
    find_chance = random.random() * 100

    if find_chance < 35:  # 35% chance trash (auto-deleted)
        category = "trash"
    elif find_chance < 70:  # 35% chance recyclable items
        category = "recyclable"
    elif find_chance < 90:  # 20% chance electronics
        category = "electronics"  
    elif find_chance < 97:  # 7% chance legendary
        category = "legendary"
    else:  # 3% chance mythical (ultra rare!)
        category = "mythical"

    # Select random item from category
    item_name, item_value = random.choice(trash_items[category])

    # Handle trash items (auto-deleted)
    if category == "trash":
        trash_messages = [
            f"🗑️ **Yuck!** {ctx.author.mention} menemukan **{item_name}** tapi langsung dibuang lagi! 🤢",
            f"🤮 **Eww!** {ctx.author.mention} mendapat **{item_name}** - langsung ke tempat sampah!",
            f"😷 **Gross!** {ctx.author.mention} nemu **{item_name}** tapi tidak bisa diambil (terlalu busuk!)"
        ]
        await ctx.send(random.choice(trash_messages))
        return

    # Increment daily usage counter
    await increment_daily_usage(ctx.author.id)

    # Add valuable items to inventory
    await add_to_inventory(ctx.author.id, item_name, category, item_value)

    # Response messages and styling based on category
    if category == "recyclable":
        emoji = "♻️"
        category_name = "barang daur ulang"
        rarity = "🟢 **UMUM**"
    elif category == "electronics":
        emoji = "⚡"
        category_name = "elektronik bekas"
        rarity = "🟡 **LANGKA**"
    elif category == "legendary":
        emoji = "💎"
        category_name = "treasure legendary"
        rarity = "🟠 **LEGENDARY**"
    else:  # mythical
        emoji = "🌟"
        category_name = "meme mythical"
        rarity = "🔮 **MYTHICAL**"

    # Special messages for rare finds
    if category == "legendary":
        success_messages = [
            f"{emoji} **TREASURE FOUND!** {ctx.author.mention} menggali **{item_name}** yang berharga!",
            f"{emoji} **LEGENDARY DROP!** {ctx.author.mention} menemukan **{item_name}** di kedalaman sampah!",
            f"{emoji} **JACKPOT!** {ctx.author.mention} beruntung dapat **{item_name}**!"
        ]
    elif category == "mythical":
        success_messages = [
            f"{emoji} **MYTHICAL MEME!** {ctx.author.mention} menemukan sticker viral **{item_name}**! 🔥",
            f"{emoji} **ULTRA RARE!** {ctx.author.mention} dapat meme legendaris **{item_name}**! 🚀",
            f"{emoji} **VIRAL CONTENT!** {ctx.author.mention} nemu **{item_name}** yang lagi trending! 💫"
        ]
    else:
        success_messages = [
            f"{emoji} **Nice find!** {ctx.author.mention} mendapat **{item_name}** dari tong sampah!",
            f"{emoji} **Lucky!** {ctx.author.mention} nemu **{item_name}** yang masih bisa dijual!"
        ]

    new_count = await get_inventory_count(ctx.author.id)
    new_usage = await get_daily_usage(ctx.author.id)

    # Special notification for rare items
    rare_bonus = ""
    if category in ["legendary", "mythical"]:
        rare_bonus = f"\n🎉 **RARE ITEM ALERT!** Kamu beruntung banget nih!"

    # Usage counter display
    if ctx.author.id == OWNER_ID:
        usage_text = "👑 **Unlimited**"
    else:
        limit = 50 if vip else 25
        usage_text = f"📊 **Usage:** {new_usage}/{limit} hari ini"

    await ctx.send(
        f"{random.choice(success_messages)}\n\n"
        f"📦 **Item:** {item_name}\n"
        f"💰 **Nilai:** {item_value} uang\n"
        f"🏷️ **Kategori:** {category_name.title()}\n"
        f"⭐ **Rarity:** {rarity}\n"
        f"📊 **Inventori:** {new_count}/{max_capacity}\n"
        f"{usage_text}{rare_bonus}\n\n"
        f"💡 **Tip:** Gunakan `!sell {item_name}` untuk menjual barang ini!"
    )


@bot.command()
async def balance(ctx):
    """Cek saldo kamu"""
    balance, vip = await get_user(ctx.author.id)
    vip_status = "💎 VIP" if vip else "👤 Regular"
    await ctx.send(
        f"💰 **Saldo {ctx.author.mention}:** {balance} uang\n🏆 **Status:** {vip_status}"
    )


@bot.command(aliases=['inv', 'inventory'])
async def inventori(ctx):
    """Cek inventori kamu - !inventori atau !inv"""
    balance, vip = await get_user(ctx.author.id)
    inventory = await get_inventory(ctx.author.id)

    if not inventory:
        await ctx.send(f"📦 **Inventori {ctx.author.mention} kosong!**\n💡 **Tip:** Gunakan `!cari` untuk mencari barang di tong sampah")
        return

    # Group items by category
    categories = {}
    total_value = 0
    item_count = 0

    for item_name, item_category, item_value, quantity in inventory:
        if item_category not in categories:
            categories[item_category] = []
        categories[item_category].append((item_name, item_value, quantity))
        total_value += item_value * quantity
        item_count += quantity

    max_capacity = 25 if vip else 15

    # Build inventory display
    inventory_text = f"📦 **INVENTORI {ctx.author.name.upper()}**\n"
    inventory_text += f"📊 **Slot:** {item_count}/{max_capacity}\n"
    inventory_text += f"💰 **Total Nilai:** {total_value} uang\n\n"

    category_emojis = {
        "recyclable": "♻️",
        "electronics": "⚡",
        "legendary": "💎",
        "mythical": "🌟"
    }

    category_names = {
        "recyclable": "BARANG DAUR ULANG",
        "electronics": "ELEKTRONIK BEKAS",
        "legendary": "TREASURE LEGENDARY",
        "mythical": "MEME MYTHICAL"
    }

    for category, items in categories.items():
        emoji = category_emojis.get(category, "📦")
        category_name = category_names.get(category, category.upper())
        inventory_text += f"{emoji} **{category_name}:**\n"

        for item_name, item_value, quantity in items:
            if quantity > 1:
                inventory_text += f"• **{item_name}** x{quantity} - {item_value} uang each\n"
            else:
                inventory_text += f"• **{item_name}** - {item_value} uang\n"
        inventory_text += "\n"

    inventory_text += "💡 **Tips:**\n"
    inventory_text += "• `!sell [nama barang]` - Jual barang tertentu\n"
    inventory_text += "• `!give [user] [barang]` - Beri barang ke user lain\n"
    inventory_text += "• `!cari` - Cari barang baru di tong sampah"

    await ctx.send(inventory_text)


@bot.command()
async def sell(ctx, *, item_name: str = None):
    """Jual barang dari inventori - !sell [nama barang]"""
    if item_name is None:
        await ctx.send(
            "❌ **Error:** Masukkan nama barang yang ingin dijual!\n"
            "📝 **Contoh:** `!sell Botol Plastik`\n"
            "💡 **Tip:** Gunakan `!inventori` untuk lihat barang yang kamu punya"
        )
        return

    balance, vip = await get_user(ctx.author.id)
    inventory = await get_inventory(ctx.author.id)

    if not inventory:
        await ctx.send(f"📦 **Inventori kosong!** Gunakan `!cari` untuk mencari barang dulu")
        return

    # Find the item (case insensitive)
    item_found = None
    for inv_item in inventory:
        if inv_item[0].lower() == item_name.lower():
            item_found = inv_item
            break

    if not item_found:
        # Show available items if item not found
        available_items = [item[0] for item in inventory]
        available_text = ", ".join(available_items[:10])  # Show max 10 items
        if len(available_items) > 10:
            available_text += f"... (+{len(available_items) - 10} lainnya)"

        await ctx.send(
            f"❌ **Barang tidak ditemukan:** `{item_name}`\n\n"
            f"📦 **Barang yang tersedia:**\n{available_text}\n\n"
            f"💡 **Tip:** Gunakan `!inventori` untuk lihat semua barang"
        )
        return

    # Extract item details
    found_name, found_category, found_value, found_quantity = item_found

    # Remove 1 quantity from inventory
    success = await remove_from_inventory(ctx.author.id, found_name, 1)

    if not success:
        await ctx.send("❌ **Error sistem inventori!** Coba lagi.")
        return

    # Add money to balance (VIP gets 2x bonus!)
    final_value = found_value * 2 if vip else found_value
    balance += final_value
    await update_user(ctx.author.id, balance=balance)

    # Category-specific responses with NEW RARITIES
    if found_category == "recyclable":
        emoji = "♻️"
        sell_messages = [
            f"{emoji} **Dijual ke pengepul!** {ctx.author.mention} menjual **{found_name}** seharga {found_value} uang!",
            f"{emoji} **Eco-friendly sale!** {ctx.author.mention} daur ulang **{found_name}** dan dapat {found_value} uang!"
        ]
    elif found_category == "electronics":
        emoji = "⚡"
        sell_messages = [
            f"{emoji} **Terjual ke tukang servis!** {ctx.author.mention} jual **{found_name}** seharga {found_value} uang!",
            f"{emoji} **Spare parts money!** {ctx.author.mention} berhasil jual **{found_name}** ke bengkel!"
        ]
    elif found_category == "legendary":
        emoji = "💎"
        sell_messages = [
            f"{emoji} **TREASURE SOLD!** {ctx.author.mention} jual **{found_name}** ke kolektor seharga {found_value} uang!",
            f"{emoji} **HIGH VALUE SALE!** {ctx.author.mention} berhasil jual **{found_name}** dengan harga premium!"
        ]
    else:  # mythical
        emoji = "🌟"
        sell_messages = [
            f"{emoji} **VIRAL MEME SOLD!** {ctx.author.mention} jual **{found_name}** ke meme collector seharga {found_value} uang!",
            f"{emoji} **LEGENDARY TRADE!** {ctx.author.mention} berhasil jual **{found_name}** dengan harga fantastis!"
        ]

    # Check remaining quantity for display
    remaining_quantity = found_quantity - 1
    quantity_text = f" (masih ada {remaining_quantity}x)" if remaining_quantity > 0 else ""

    vip_bonus_text = f"💎 **VIP BONUS 2x!** ({found_value} → {final_value})" if vip else ""

    await ctx.send(
        f"{random.choice(sell_messages)}\n\n"
        f"💰 **Dapat:** {final_value} uang\n"
        f"{vip_bonus_text}\n" if vip else ""
        f"💵 **Saldo baru:** {balance}\n"
        f"📦 **Item:** {found_name}{quantity_text}"
    )


@bot.command()
async def give(ctx, user: discord.Member = None, *, item_name: str = None):
    """Beri barang dari inventori ke user lain - !give [user] [nama barang]"""
    if user is None or item_name is None:
        await ctx.send(
            "❌ **Error:** Format tidak lengkap!\n"
            "📝 **Contoh:** `!give @username Botol Plastik`\n"
            "💡 **Tip:** Tag user dan masukkan nama barang yang ingin diberikan"
        )
        return

    giver_id = ctx.author.id
    receiver_id = user.id

    # Can't give to yourself
    if giver_id == receiver_id:
        await ctx.send("❌ **Error:** Tidak bisa memberi barang ke diri sendiri!")
        return

    # Can't give to bots
    if user.bot:
        await ctx.send("❌ **Error:** Tidak bisa memberi barang ke bot!")
        return

    # Check giver's inventory
    giver_inventory = await get_inventory(giver_id)
    if not giver_inventory:
        await ctx.send("📦 **Inventori kosong!** Tidak ada barang untuk diberikan.")
        return

    # Find the item
    item_found = None
    for inv_item in giver_inventory:
        if inv_item[0].lower() == item_name.lower():
            item_found = inv_item
            break

    if not item_found:
        available_items = [item[0] for item in giver_inventory]
        available_text = ", ".join(available_items[:8])
        if len(available_items) > 8:
            available_text += f"... (+{len(available_items) - 8} lainnya)"

        await ctx.send(
            f"❌ **Barang tidak ditemukan:** `{item_name}`\n\n"
            f"📦 **Barang yang kamu punya:**\n{available_text}"
        )
        return

    found_name, found_category, found_value, found_quantity = item_found

    # Check receiver's inventory capacity
    receiver_balance, receiver_vip = await get_user(receiver_id)
    receiver_current_items = await get_inventory_count(receiver_id)
    receiver_max_capacity = 25 if receiver_vip else 15

    if receiver_current_items >= receiver_max_capacity:
        vip_status = "💎 VIP" if receiver_vip else "👤 Regular"
        await ctx.send(
            f"❌ **{user.mention} inventori penuh!** ({receiver_current_items}/{receiver_max_capacity})\n"
            f"👤 **Status:** {vip_status}\n"
            f"💡 **Tip:** User tersebut harus jual barang dulu untuk memberi ruang"
        )
        return

    # Remove item from giver
    success = await remove_from_inventory(giver_id, found_name, 1)
    if not success:
        await ctx.send("❌ **Error sistem inventori!** Coba lagi.")
        return

    # Add item to receiver
    await add_to_inventory(receiver_id, found_name, found_category, found_value, 1)

    # Category emojis for display
    category_emojis = {
        "recyclable": "♻️",
        "electronics": "⚡", 
        "legendary": "💎",
        "mythical": "🌟"
    }

    emoji = category_emojis.get(found_category, "📦")

    # Special messages for rare items
    if found_category == "legendary":
        give_messages = [
            f"{emoji} **TREASURE GIFT!** {ctx.author.mention} memberikan **{found_name}** kepada {user.mention}!",
            f"{emoji} **LEGENDARY PRESENT!** {user.mention} dapat hadiah berharga dari {ctx.author.mention}!"
        ]
    elif found_category == "mythical":
        give_messages = [
            f"{emoji} **MYTHICAL GIFT!** {ctx.author.mention} sharing meme viral **{found_name}** ke {user.mention}! 🔥",
            f"{emoji} **ULTRA RARE PRESENT!** {user.mention} dapat sticker legendaris dari {ctx.author.mention}! 🚀"
        ]
    else:
        give_messages = [
            f"{emoji} **Gift delivered!** {ctx.author.mention} memberikan **{found_name}** kepada {user.mention}!",
            f"{emoji} **Generous act!** {user.mention} dapat hadiah dari {ctx.author.mention}!"
        ]

    # Check remaining quantity for giver
    remaining_quantity = found_quantity - 1
    giver_remaining_text = f" (kamu masih ada {remaining_quantity}x)" if remaining_quantity > 0 else " (barang terakhir kamu!)"

    await ctx.send(
        f"{random.choice(give_messages)}\n\n"
        f"🎁 **Item:** {found_name}\n"
        f"💰 **Nilai:** {found_value} uang\n"
        f"📦 **Status:** Transfer berhasil{giver_remaining_text}\n"
        f"🎉 **{user.mention}** sekarang memiliki **{found_name}**!"
    )


@bot.command(aliases=['sellall', 'sell-all'])
async def jualall(ctx):
    """Jual semua barang di inventori - !jualall atau !sellall"""
    balance, vip = await get_user(ctx.author.id)
    inventory = await get_inventory(ctx.author.id)

    if not inventory:
        await ctx.send(f"📦 **Inventori kosong!** Tidak ada barang untuk dijual.\n💡 **Tip:** Gunakan `!cari` untuk mencari barang")
        return

    # Calculate total value and build sell summary
    total_base_value = 0
    item_count = 0
    categories = {"recyclable": [], "electronics": [], "legendary": [], "mythical": []}

    for item_name, item_category, item_value, quantity in inventory:
        total_base_value += item_value * quantity
        item_count += quantity

        if item_category in categories:
            categories[item_category].append((item_name, item_value, quantity))

    # Apply VIP bonus
    final_total = total_base_value * 2 if vip else total_base_value
    balance += final_total

    # Clear all inventory
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM inventory WHERE user_id = ?", (ctx.author.id,))
        await db.commit()

    # Update user balance
    await update_user(ctx.author.id, balance=balance)

    # Build response message with special flair for rare items
    has_rare_items = len(categories["legendary"]) > 0 or len(categories["mythical"]) > 0

    if has_rare_items:
        sell_messages = [
            "🌟 **EPIC MASS LIQUIDATION!** Koleksi rare items terjual habis dengan harga fantastis!",
            "💫 **LEGENDARY GARAGE SALE!** Semua treasure dan meme viral berhasil dijual!",
            "🔥 **ULTRA BULK SALE!** Kolektor dari seluruh dunia membayar mahal untuk items kamu!"
        ]
    else:
        sell_messages = [
            "🏪 **MASS LIQUIDATION!** Semua barang terjual habis!",
            "💸 **GARAGE SALE SUCCESS!** Inventori dikosongkan total!",
            "🎉 **BULK SALE COMPLETE!** Semua item berhasil dijual!"
        ]

    response = f"{random.choice(sell_messages)}\n\n"
    response += f"📦 **Total Items:** {item_count} barang\n"
    response += f"💰 **Base Value:** {total_base_value} uang\n"

    if vip:
        response += f"💎 **VIP BONUS 2x!** {total_base_value} → {final_total} uang\n"

    response += f"💵 **Saldo baru:** {balance}\n\n"

    # Show breakdown by category with new rarities
    category_count = 0
    if categories["recyclable"]:
        response += f"♻️ **Barang Daur Ulang:** {len(categories['recyclable'])} jenis\n"
        category_count += 1
    if categories["electronics"]:
        response += f"⚡ **Elektronik Bekas:** {len(categories['electronics'])} jenis\n"
        category_count += 1
    if categories["legendary"]:
        response += f"💎 **Treasure Legendary:** {len(categories['legendary'])} jenis ✨\n"
        category_count += 1
    if categories["mythical"]:
        response += f"🌟 **Meme Mythical:** {len(categories['mythical'])} jenis 🔥\n"
        category_count += 1

    response += f"\n🎯 **Inventori sekarang:** 0/{'25' if vip else '15'}"

    if has_rare_items:
        response += f"\n🌟 **RARE COLLECTION BONUS!** Kamu telah menjual {category_count} kategori items yang berbeda!"

    await ctx.send(response)


@bot.command()
async def gamble(ctx, amount: int = None):
    """Gambling sederhana (VIP only) - !gamble [jumlah]"""
    if amount is None:
        await ctx.send(
            "❌ **Error:** Masukkan jumlah taruhan!\n📝 **Contoh:** `!gamble 50`"
        )
        return

    balance, vip = await get_user(ctx.author.id)
    if not vip:
        await ctx.send(
            "🔒 **Maaf,** hanya VIP yang bisa menggunakan fitur gambling.\n💎 **Minta owner untuk VIP:** `!vip @kamu`"
        )
        return
    if amount <= 0:
        await ctx.send(
            "❌ **Error:** Masu�kkan jumlah taruhan yang valid (lebih dari 0)!")
        return
    if balance < amount:
        await ctx.send(
            f"💸 **Saldo tidak cukup!** Kamu butuh {amount} tapi hanya punya {balance} uang.\n💰 **Cari uang dulu:** `!cari`"
        )
        return

    menang = random.choice([True, False])
    if menang:
        balance += amount
        await update_user(ctx.author.id, balance=balance)
        await ctx.send(
            f"🎉 **MENANG!** {ctx.author.mention} mendapat {amount} uang!\n💰 **Saldo sekarang:** {balance}"
        )
    else:
        balance -= amount
        await update_user(ctx.author.id, balance=balance)
        await ctx.send(
            f"😢 **Kalah!** {ctx.author.mention} kehilangan {amount} uang.\n💰 **Saldo sekarang:** {balance}"
        )


@bot.command()
async def roulette(ctx, bet: int = None):
    """🎲 Russian Roulette - Interactive Choice-Based Game!"""
    if bet is None:
        await ctx.send(
            "🎯 **RUSSIAN ROULETTE - CHOICE-BASED!**\n❌ **Error:** Masukkan taruhan!\n📝 **Contoh:** `!roulette 100`\n\n🎮 **Aturan Interactive:**\n🤖 **Bot vs Player** - strategic decisions!\n🎯 **!kepala** - tembak diri sendiri (empty = extra turn!)\n🔫 **!lawan** - tembak lawan (safe play)\n❤️ **3 nyawa per ronde**, reset setiap ronde\n💰 **Menang = taruhan x3**"
        )
        return

    # Check if player already has active game
    if ctx.author.id in active_games:
        await ctx.send(
            "❌ **Kamu sudah punya game aktif!**\n🔄 **Selesaikan dulu atau ketik** `!surrender` **untuk menyerah**"
        )
        return

    balance, vip = await get_user(ctx.author.id)

    if bet <= 0:
        await ctx.send("❌ **Error:** Taruhan harus lebih dari 0!")
        return
    if balance < bet:
        await ctx.send(
            f"💸 **Saldo tidak cukup!** Butuh {bet} tapi hanya punya {balance} uang."
        )
        return

    # Initialize game state
    game_state = {
        'player_id': ctx.author.id,
        'channel_id': ctx.channel.id,
        'bet': bet,
        'round': 1,
        'max_rounds': 3,
        'player_wins': 0,
        'bot_wins': 0,
        'player_lives': 3,
        'bot_lives': 3,
        'turn_player': True,  # True = player, False = bot
        'chambers': 0,
        'bullets': 0,
        'revolver': [],
        'current_chamber': 0,
        'waiting_for_choice': False
    }

    active_games[ctx.author.id] = game_state

    await ctx.send(
        f"🎲 **RUSSIAN ROULETTE - STRATEGIC DUEL!**\n\n👤 **Player:** {ctx.author.mention}\n🤖 **Opponent:** 🤖 Alpha D\n💰 **Taruhan:** {bet} uang\n\n🔫 **Mempersiapkan revolver...**"
    )

    await start_new_round(ctx, game_state)


async def start_new_round(ctx, game_state):
    """Start a new round of Russian Roulette"""
    round_num = game_state['round']

    await ctx.send(
        f"\n🔥 **═══ RONDE {round_num} ═══**\n\n🔄 **RESET!** Kedua pemain kembali memiliki 3 nyawa\n🎯 **Player:** <@{game_state['player_id']}> ❤️❤️❤️\n🤖 **Bot:** 🤖 Alpha D ❤️❤️❤️"
    )

    await asyncio.sleep(2)

    # Reset lives for this round
    game_state['player_lives'] = 3
    game_state['bot_lives'] = 3

    # Setup realistic revolver with progressive difficulty
    chambers = random.randint(6, 9)

    # Progressive difficulty system
    if round_num == 1:
        bullets = random.randint(1, 3)  # Round 1: 1-3 bullets (easier)
    elif round_num == 2:
        bullets = random.randint(3, 4)  # Round 2: 3-4 bullets (medium)
    else:  # round 3
        bullets = random.randint(4, 5)  # Round 3: 4-5 bullets (harder)

    # Ensure bullets don't exceed chambers
    bullets = min(bullets, chambers - 1)

    # Create revolver with random bullet positions
    revolver = [False] * chambers
    bullet_positions = random.sample(range(chambers), bullets)
    for pos in bullet_positions:
        revolver[pos] = True

    game_state['chambers'] = chambers
    game_state['bullets'] = bullets
    game_state['revolver'] = revolver
    game_state['current_chamber'] = 0

    # Difficulty indicator
    if round_num == 1:
        difficulty = "🟢 **MUDAH**"
    elif round_num == 2:
        difficulty = "🟡 **MEDIUM**"
    else:
        difficulty = "🔴 **SULIT**"

    await ctx.send(
        f"🔫 **REVOLVER SETUP:**\n📊 **Chambers:** {chambers}\n💥 **Bullets:** {bullets}\n🎲 **Bullet positions:** *Hidden*\n⚡ **Difficulty:** {difficulty}\n\n🎯 **Starting chamber:** 1/{chambers}"
    )

    await asyncio.sleep(1)

    # Determine who goes first randomly
    game_state['turn_player'] = random.choice([True, False])

    if game_state['turn_player']:
        await ctx.send("🎲 **Koin dilempar...** 🪙\n\n✨ **PLAYER MULAI DULUAN!**"
                       )
        await asyncio.sleep(1)
        await prompt_player_choice(ctx, game_state)
    else:
        await ctx.send("🎲 **Koin dilempar...** 🪙\n\n🤖 **BOT MULAI DULUAN!**")
        await asyncio.sleep(1)
        await bot_turn(ctx, game_state)


async def prompt_player_choice(ctx, game_state):
    """Prompt player to make a choice"""
    current_chamber = game_state['current_chamber'] % game_state['chambers']

    await ctx.send(
        f"\n🎯 **GILIRAN ANDA!**\n\n🔫 **Chamber {current_chamber + 1}/{game_state['chambers']}**\n❤️ **Nyawa Player:** {game_state['player_lives']} | **Bot:** {game_state['bot_lives']}\n\n🤔 **Pilih tindakan:**\n🎯 **!kepala** - Tembak diri sendiri (berisiko, tapi bisa extra turn!)\n🔫 **!lawan** - Tembak lawan (bermain aman)\n\n⏰ **Waktu 30 detik untuk memilih...**"
    )

    game_state['waiting_for_choice'] = True


async def bot_turn(ctx, game_state):
    """Handle bot's turn with AI decision making"""
    current_chamber = game_state['current_chamber'] % game_state['chambers']

    await ctx.send(
        f"\n🤖 **GILIRAN BOT**\n\n🔫 **Chamber {current_chamber + 1}/{game_state['chambers']}**\n❤️ **Nyawa Player:** {game_state['player_lives']} | **Bot:** {game_state['bot_lives']}\n\n🎲 **Bot sedang menganalisis...**"
    )

    await asyncio.sleep(2)

    # Advanced Bot AI decision making
    chambers_left = game_state['chambers'] - game_state['current_chamber']
    bullets_left = sum(game_state['revolver'][game_state['current_chamber']:])
    bullet_chance = bullets_left / chambers_left if chambers_left > 0 else 0

    # Smart bot strategy based on multiple factors
    shoot_self = False

    # Factor 1: Life advantage/disadvantage
    life_advantage = game_state['bot_lives'] - game_state['player_lives']

    # Factor 2: Round progression (more aggressive in later rounds)
    round_aggression = 0.1 * game_state['round']  # 0.1, 0.2, 0.3

    # Factor 3: Bullet risk assessment
    safe_threshold = 0.25 + round_aggression  # Gets more aggressive each round

    # Decision matrix
    if bullet_chance <= safe_threshold:
        # Relatively safe - consider shooting self for extra turn
        if life_advantage >= 0:
            # Bot is ahead or tied - play for extra turns
            shoot_self = True
        else:
            # Bot is behind - 70% chance to risk it for extra turn
            shoot_self = random.random() < 0.7

    elif game_state['player_lives'] == 1:
        # Player has 1 life - always go for the kill
        shoot_self = False

    elif life_advantage > 1:
        # Bot has significant life advantage - play it safe
        shoot_self = False

    elif life_advantage < -1:
        # Bot is significantly behind - take risks
        if bullet_chance < 0.5:
            shoot_self = True
        else:
            # Even risky, but desperate
            shoot_self = random.random() < 0.4

    else:
        # Close game - balanced strategy
        if bullet_chance < 0.35:
            shoot_self = random.random(
            ) < 0.6  # 60% chance to risk for extra turn
        else:
            shoot_self = random.random(
            ) < 0.3  # 30% chance to risk with high danger

    if shoot_self:
        # Bot shoots self - various strategic reasons
        if bullet_chance <= 0.2:
            bot_thoughts = [
                "🤖 *\"Probabilitas sangat aman, ambil extra turn...\"*",
                "🤖 *\"Risiko minimal, keuntungan maksimal...\"*",
                "�🤖 *\"Matematika mendukung keputusan ini...\"*"
            ]
        elif life_advantage < 0:
            bot_thoughts = [
                "🤖 *\"Situasi sulit, harus ambil risiko...\"*",
                "🤖 *\"Desperate times, desperate measures...\"*",
                "🤖 *\"All-in untuk comeback!\"*"
            ]
        else:
            bot_thoughts = [
                "🤖 *\"Strategi agresif untuk dominasi...\"*",
                "🤖 *\"Kalkulasi risiko vs reward...\"*",
                "🤖 *\"Confidence level: optimal...\"*"
            ]

        await ctx.send(
            f"{random.choice(bot_thoughts)}\n\n🎯 **Bot memilih: TEMBAK DIRI SENDIRI!**\n📊 **Bullet chance:** {bullet_chance:.1%}"
        )
        await asyncio.sleep(1)
        await execute_shot(ctx, game_state, True,
                           False)  # shoot_self=True, is_player=False
    else:
        # Bot shoots player - offensive strategy
        if game_state['player_lives'] == 1:
            bot_thoughts = [
                "🤖 *\"Target dalam mode critical - eliminasi!\"*",
                "🤖 *\"Finishing move activated...\"*",
                "🤖 *\"Saatnya mengakhiri permainan ini...\"*"
            ]
        elif bullet_chance > 0.5:
            bot_thoughts = [
                "🤖 *\"Terlalu berisiko untuk diri sendiri...\"*",
                "🤖 *\"Safety first, attack second...\"*",
                "🤖 *\"Bermain konservatif lebih bijak...\"*"
            ]
        else:
            bot_thoughts = [
                "🤖 *\"Strategi ofensif langsung...\"*",
                "🤖 *\"Pressure is the key to victory...\"*",
                "🤖 *\"Eliminate atau be eliminated...\"*"
            ]

        await ctx.send(
            f"{random.choice(bot_thoughts)}\n\n🔫 **Bot memilih: TEMBAK PLAYER!**\n📊 **Bullet chance:** {bullet_chance:.1%}"
        )
        await asyncio.sleep(1)
        await execute_shot(ctx, game_state, False,
                           False)  # shoot_self=False, is_player=False


async def execute_shot(ctx, game_state, shoot_self, is_player):
    """Execute the shot and handle the consequences"""
    current_chamber = game_state['current_chamber'] % game_state['chambers']
    is_bullet = game_state['revolver'][current_chamber]

    await asyncio.sleep(2)

    if is_bullet:
        # Hit bullet
        if shoot_self:
            if is_player:
                game_state['player_lives'] -= 1
                await ctx.send(
                    f"💥 **BANG!** 💀\n\n☠️ **Player menembak diri sendiri dan kena peluru!**\n❤️ **Nyawa tersisa:** {game_state['player_lives']}"
                )
            else:
                game_state['bot_lives'] -= 1
                await ctx.send(
                    f"💥 **BANG!** 🔥\n\n🤖 **Bot menembak diri sendiri dan kena peluru!**\n❤️ **Nyawa tersisa:** {game_state['bot_lives']}"
                )
        else:
            if is_player:
                game_state['bot_lives'] -= 1
                await ctx.send(
                    f"💥 **BANG!** 🎯\n\n🔫 **Player menembak bot dan kena sasaran!**\n❤️ **Bot nyawa tersisa:** {game_state['bot_lives']}"
                )
            else:
                game_state['player_lives'] -= 1
                await ctx.send(
                    f"💥 **BANG!** 🎯\n\n🤖 **Bot menembak player dan kena sasaran!**\n❤️ **Player nyawa tersisa:** {game_state['player_lives']}"
                )

        # Switch turns (bullet = end turn)
        game_state['turn_player'] = not game_state['turn_player']

    else:
        # Empty chamber
        if shoot_self:
            if is_player:
                await ctx.send(
                    f"🔫 **KLIK** ✨\n\n😎 **Player berani dan beruntung!** Chamber kosong!\n🎉 **BONUS TURN! Player bisa menembak lagi!**"
                )
                # Don't switch turns - player gets extra turn
            else:
                await ctx.send(
                    f"🔫 **KLIK** ⚡\n\n🤖 *\"Perhitungan yang tepat!\"* Chamber kosong!\n🎉 **Bot mendapat giliran tambahan!**"
                )
                # Don't switch turns - bot gets extra turn
        else:
            if is_player:
                await ctx.send(
                    f"🔫 **KLIK** 😤\n\n💔 **Chamber kosong! Bot selamat!**\n🔄 **Giliran berganti ke bot...**"
                )
            else:
                await ctx.send(
                    f"🔫 **KLIK** 😅\n\n💚 **Chamber kosong! Player selamat!**\n🔄 **Giliran berganti ke player...**"
                )

            # Switch turns (missed shot = opponent's turn)
            game_state['turn_player'] = not game_state['turn_player']

    game_state['current_chamber'] += 1
    await asyncio.sleep(1)

    # Check for round end
    if game_state['player_lives'] <= 0:
        await end_round(ctx, game_state, 'bot')
    elif game_state['bot_lives'] <= 0:
        await end_round(ctx, game_state, 'player')
    else:
        # Continue game
        if game_state['turn_player']:
            await prompt_player_choice(ctx, game_state)
        else:
            await bot_turn(ctx, game_state)


async def end_round(ctx, game_state, winner):
    """End the current round and start next or end game"""
    if winner == 'player':
        game_state['player_wins'] += 1
        await ctx.send(f"🎉 **PLAYER MENANG RONDE {game_state['round']}!**")
    else:
        game_state['bot_wins'] += 1
        await ctx.send(f"🤖 **BOT MENANG RONDE {game_state['round']}!**")

    await asyncio.sleep(2)

    if game_state['round'] < game_state['max_rounds']:
        await ctx.send(
            f"\n📊 **SKOR SEMENTARA:**\n🎯 **Player:** {game_state['player_wins']} ronde\n🤖 **Bot:** {game_state['bot_wins']} ronde\n\n⏭️ **Lanjut ke ronde berikutnya...**"
        )
        game_state['round'] += 1
        await asyncio.sleep(2)
        await start_new_round(ctx, game_state)
    else:
        await end_game(ctx, game_state)


async def end_game(ctx, game_state):
    """End the entire game and distribute rewards"""
    player_id = game_state['player_id']
    bet = game_state['bet']

    await ctx.send(
        f"\n🏁 **═══ HASIL AKHIR ═══**\n\n📊 **SKOR FINAL:**\n🎯 **Player:** {game_state['player_wins']}/{game_state['max_rounds']} ronde\n🤖 **Bot:** {game_state['bot_wins']}/{game_state['max_rounds']} ronde"
    )

    balance, vip = await get_user(player_id)

    if game_state['player_wins'] > game_state['bot_wins']:
        # Player wins
        winnings = bet * 3
        balance += winnings
        await update_user(player_id, balance=balance)
        await ctx.send(
            f"🏆 **KEMENANGAN STRATEGIC!** 🎉\n\n🎯 **PLAYER MENANG!**\n💰 **Hadiah:** {winnings} uang (3x taruhan!)\n💵 **Saldo baru:** {balance}\n\n🤖 *\"Strategi yang mengesankan, manusia...\"*"
        )
    elif game_state['bot_wins'] > game_state['player_wins']:
        # Bot wins
        balance -= bet
        await update_user(player_id, balance=balance)
        await ctx.send(
            f"💀 **KEKALAHAN STRATEGIC!** 😱\n\n🤖 **BOT MENANG!**\n💸 **Kehilangan:** {bet} uang\n💵 **Saldo baru:** {balance}\n\n🤖 *\"Artificial Intelligence > Human Intuition!\"*"
        )
    else:
        # Tie
        await ctx.send(
            f"🤝 **SERI STRATEGIC!** ⚖️\n\nBattle of minds berakhir seri!\n💰 **Taruhan dikembalikan:** {bet} uang\n\n🤖 *\"Kemampuan strategis yang setara...\"*"
        )

    # Remove from active games
    del active_games[player_id]

    await ctx.send(
        f"\n🎲 **Main lagi?** `!roulette {bet}`\n🔄 **Atau ubah taruhan:** `!roulette [jumlah_baru]`"
    )


@bot.command()
async def kepala(ctx):
    """Tembak kepala sendiri - berisiko tapi bisa dapat extra turn!"""
    if ctx.author.id not in active_games:
        await ctx.send(
            "❌ **Tidak ada game aktif!**\n🎲 **Mulai game:** `!roulette [taruhan]`"
        )
        return

    game_state = active_games[ctx.author.id]

    if not game_state['waiting_for_choice']:
        await ctx.send("❌ **Bukan giliran kamu atau sudah memilih!**")
        return

    if not game_state['turn_player']:
        await ctx.send("❌ **Ini giliran bot, bukan kamu!**")
        return

    game_state['waiting_for_choi�ce'] = False
    await ctx.send("🎯 **PILIHAN BERANI!** Menembak kepala sendiri...")
    await execute_shot(ctx, game_state, True,
                       True)  # shoot_self=True, is_player=True


@bot.command()
async def lawan(ctx):
    """Tembak lawan - bermain aman"""
    if ctx.author.id not in active_games:
        await ctx.send(
            "❌ **Tidak ada game aktif!**\n🎲 **Mulai game:** `!roulette [taruhan]`"
        )
        return

    game_state = active_games[ctx.author.id]

    if not game_state['waiting_for_choice']:
        await ctx.send("❌ **Bukan giliran kamu atau sudah memilih!**")
        return

    if not game_state['turn_player']:
        await ctx.send("❌ **Ini giliran bot, bukan kamu!**")
        return

    game_state['waiting_for_choice'] = False
    await ctx.send("🔫 **PILIHAN AMAN!** Menembak lawan...")
    await execute_shot(ctx, game_state, False,
                       True)  # shoot_self=False, is_player=True


@bot.command()
async def surrender(ctx):
    """Menyerah dari game aktif"""
    if ctx.author.id not in active_games:
        await ctx.send("❌ **Tidak ada game aktif untuk diserahkan!**")
        return

    game_state = active_games[ctx.author.id]
    bet = game_state['bet']

    # Lose half the bet when surrendering
    balance, vip = await get_user(ctx.author.id)
    penalty = bet // 2
    balance -= penalty
    await update_user(ctx.author.id, balance=balance)

    del active_games[ctx.author.id]

    await ctx.send(
        f"🏳️ **SURRENDER!**\n\n😔 **{ctx.author.mention} menyerah dari Russian Roulette**\n💸 **Penalty:** {penalty} uang (50% taruhan)\n💵 **Saldo baru:** {balance}\n\n🤖 *\"Keputusan yang bijak... atau pengecut?\"*"
    )


@bot.tree.command(name="ping", description="Test bot responsif")
async def ping_slash(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! Latency: {latency}ms\nBot online dan berfungsi!")


@bot.tree.command(name="balance", description="Cek saldo kamu")
async def balance_slash(interaction: discord.Interaction):
    user_id = interaction.user.id
    balance, vip = await get_user(user_id)
    vip_status = "💎 VIP" if vip else "👤 Regular"
    await interaction.response.send_message(
        f"💰 **Saldo {interaction.user.mention}:** {balance} uang\n🏆 **Status:** {vip_status}")


@bot.tree.command(name="help", description="Menampilkan daftar command dan panduan penggunaan bot.")
async def help_slash(interaction: discord.Interaction):
    help_text = (
        "**Panduan Bot Alpha D**\n"
        "\n"
        "/ping - Cek respons bot\n"
        "/cari - Cari barang di tong sampah\n"
        "/balance - Cek saldo kamu\n"
        "/inventori - Lihat inventori kamu\n"
        "/sell [nama barang] - Jual barang tertentu\n"
        "/give [user] [nama barang] - Beri barang ke user lain\n"
        "/vip @user - Berikan status VIP (owner only)\n"
        "/gamble [jumlah] - Gambling sederhana (VIP only)\n"
        "/roulette [jumlah] - Main Russian Roulette\n"
        "/jualall - Jual semua barang di inventori\n"
        "/mute, /kick, /ban - Moderasi server\n"
        "\n"
        "💡 Untuk detail tiap command, gunakan /help [nama command]"
        "\n"
        "Note : beberapa command mungkin belum tersedia di slash command. Jadi gunakan prefix '!' untuk command tersebut."
    )
    await interaction.response.send_message(help_text)


@bot.tree.command(name="transfer", description="Transfer uang ke user lain.")
@app_commands.describe(user="User tujuan", jumlah="Jumlah uang yang akan dikirim")
async def transfer_slash(interaction: discord.Interaction, user: discord.Member, jumlah: int):
    if user.bot:
        await interaction.response.send_message("❌ Tidak bisa transfer ke bot!", ephemeral=True)
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message("❌ Tidak bisa transfer ke diri sendiri!", ephemeral=True)
        return
    if jumlah <= 0:
        await interaction.response.send_message("❌ Jumlah harus lebih dari 0!", ephemeral=True)
        return
    balance, _ = await get_user(interaction.user.id)
    if balance < jumlah:
        await interaction.response.send_message(f"❌ Saldo tidak cukup! Kamu punya {balance} uang.", ephemeral=True)
        return
    receiver_balance, _ = await get_user(user.id)
    await update_user(interaction.user.id, balance=balance-jumlah)
    await update_user(user.id, balance=receiver_balance+jumlah)
    await interaction.response.send_message(f"✅ {interaction.user.mention} mengirim {jumlah} uang ke {user.mention}!", ephemeral=False)

@bot.tree.command(name="gambling", description="Main gambling melawan agen bot.")
@app_commands.describe(game="Pilih game: rolet, blackjack, poker", jumlah="Jumlah taruhan")
async def gambling_slash(interaction: discord.Interaction, game: str, jumlah: int):
    game = game.lower()
    if game not in ["rolet", "blackjack", "poker"]:
        await interaction.response.send_message("❌ Game tidak tersedia! Pilih: rolet, blackjack, poker.", ephemeral=True)
        return
    if jumlah <= 0:
        await interaction.response.send_message("❌ Jumlah taruhan harus lebih dari 0!", ephemeral=True)
        return
    balance, vip = await get_user(interaction.user.id)
    if balance < jumlah:
        await interaction.response.send_message(f"❌ Saldo tidak cukup! Kamu punya {balance} uang.", ephemeral=True)
        return
    # Rolet
    if game == "rolet":
        menang = random.choice([True, False])
        if menang:
            balance += jumlah
            await update_user(interaction.user.id, balance=balance)
            await interaction.response.send_message(f"🎲 **ROLET**: Kamu MENANG! +{jumlah} uang. Saldo sekarang: {balance}")
        else:
            balance -= jumlah
            await update_user(interaction.user.id, balance=balance)
            await interaction.response.send_message(f"🎲 **ROLET**: Kamu KALAH! -{jumlah} uang. Saldo sekarang: {balance}")
    # Blackjack
    elif game == "blackjack":
        player = random.randint(16, 21)
        dealer = random.randint(16, 21)
        if player > dealer:
            balance += jumlah
            await update_user(interaction.user.id, balance=balance)
            await interaction.response.send_message(f"🃏 **BLACKJACK**: Kamu {player}, Dealer {dealer}. MENANG! +{jumlah} uang. Saldo: {balance}")
        elif player < dealer:
            balance -= jumlah
            await update_user(interaction.user.id, balance=balance)
            await interaction.response.send_message(f"🃏 **BLACKJACK**: Kamu {player}, Dealer {dealer}. KALAH! -{jumlah} uang. Saldo: {balance}")
        else:
            await interaction.response.send_message(f"🃏 **BLACKJACK**: Seri! Kamu {player}, Dealer {dealer}. Saldo: {balance}")
    # Poker
    elif game == "poker":
        hasil = random.choice(["MENANG", "KALAH", "SERI"])
        if hasil == "MENANG":
            balance += jumlah * 2
            await update_user(interaction.user.id, balance=balance)
            await interaction.response.send_message(f"♠️ **POKER**: Kamu MENANG! +{jumlah*2} uang. Saldo: {balance}")
        elif hasil == "KALAH":
            balance -= jumlah
            await update_user(interaction.user.id, balance=balance)
            await interaction.response.send_message(f"♠️ **POKER**: Kamu KALAH! -{jumlah} uang. Saldo: {balance}")
        else:
            await interaction.response.send_message(f"♠️ **POKER**: Seri! Saldo: {balance}")


if TOKEN is None:
    print("Error: DISCORD_TOKEN environment variable not found!")
    print("Please set your Discord bot token.")
    exit(1)

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()
    
keep_alive()

bot.run(TOKEN)
