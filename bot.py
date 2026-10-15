"""Polish VACC bot: wallpaper of the month polls, METAR/TAF, ATC bookings, pilot roles, moderation and role commands - controlled from CoreOPS."""
import asyncio
import io
import json
import logging
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------------
load_dotenv(Path(__file__).with_name(".env"))
TOKEN = os.getenv("DISCORD_TOKEN", "")
_guild_match = re.search(r"\d{17,20}", os.getenv("GUILD_ID", "875305430321086485"))  # tolerant of quotes / spaces in .env
GUILD_ID = _guild_match.group() if _guild_match else ""

BOARD_ROLE_ID = int(os.getenv("BOARD_ROLE_ID", "1394941236284227604"))  # Board role - manages the poll access list
PILOT_ROLES = {
    "training": int(os.getenv("TRAINING_PILOT_ROLE_ID", "1103329876053872780")),
    "exam": int(os.getenv("EXAM_PILOT_ROLE_ID", "1103330255760007229")),
}

# CoreOPS panel (Configuration -> Discord bot): the bot connects to CoreOPS, fetches settings and commands and reports its state.
# Without COREOPS_URL and COREOPS_TOKEN it runs on its own (all functions on, values from .env).
COREOPS_URL = os.getenv("COREOPS_URL", "").strip().rstrip("/")
COREOPS_TOKEN = os.getenv("COREOPS_TOKEN", "").strip()
SYNC_SECONDS = 15
VERSION = "2.1-coreops"
STARTED_AT = int(time.time())
CONFIG_CACHE = Path(__file__).with_name("coreops_config.json")  # last settings from the panel (used when CoreOPS is unreachable)

EMBED_COLOR = 0xBD0505
ORG_NAME = "Polish VACC"
VOTE_EMOJI = "👍"
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
TZ = timezone.utc  # all dates and times are UTC
MIN_ENTRIES, MAX_ENTRIES = 3, 10
DB_PATH = Path(__file__).with_name("tapeta.db")

WEATHER_API = "https://aviationweather.gov/api/data"
# Note: the User-Agent must not contain the word "DiscordBot" - the CoreOPS host firewall takes it for a fake Discord crawler and answers 406.
USER_AGENT = "PLVACC-CoreOPS/2.1 (+https://plvacc.pl)"

NO_PINGS = discord.AllowedMentions.none()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("plvacc-bot")

# ----------------------------------------------------------------------------
# SETTINGS FROM THE COREOPS PANEL (empty = defaults: everything on, values from .env)
# ----------------------------------------------------------------------------
# slash command name -> function key in the CoreOPS panel (the keys keep their old names so saved settings are not lost)
COMMAND_FEATURES = {"wallpaper": "tapeta", "metar": "metar", "taf": "taf", "bookings": "bookings", "role": "rola", "mod": "moderation"}
panel_features: dict[str, bool] = {}
panel_settings: dict = {}
panel_rev = ""
panel_role_commands: list[dict] = []
_cooldowns: dict[tuple[str, int], float] = {}


def apply_panel_config(config: dict) -> None:
    global panel_features, panel_settings, panel_rev, panel_role_commands
    features, settings = config.get("features"), config.get("settings")
    if isinstance(features, dict):
        panel_features = {str(k): bool(v) for k, v in features.items()}
    if isinstance(settings, dict):
        panel_settings = dict(settings)
    commands = config.get("role_commands")
    if isinstance(commands, list):
        panel_role_commands = [c for c in commands if isinstance(c, dict)]
    panel_rev = str(config.get("rev") or "")


def load_config_cache() -> None:
    try:
        apply_panel_config(json.loads(CONFIG_CACHE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass


def feature_enabled(name: str) -> bool:
    return panel_features.get(name, True)


def setting_int(name: str, default: int) -> int:
    try:
        return int(panel_settings.get(name, default))
    except (TypeError, ValueError):
        return default


def setting_id(name: str, default: int) -> int:
    value = str(panel_settings.get(name) or "").strip()
    return int(value) if value.isdigit() else default


def role_id_list(name: str) -> list[int]:
    """A setting that holds one or several role IDs separated by commas / spaces."""
    raw = str(panel_settings.get(name) or "")
    return [int(part) for part in re.split(r"[,\s]+", raw) if part.isdigit()]


def moderator_role_ids() -> set[int]:
    return set(role_id_list("moderator_role_id"))


def board_role_id() -> int:
    return setting_id("board_role_id", BOARD_ROLE_ID)


def pilot_role_id(kind: str) -> int:
    return setting_id(f"{kind}_pilot_role_id", PILOT_ROLES[kind])


def booking_prefixes() -> tuple[str, ...]:
    parts = tuple(p for p in re.split(r"[,\s]+", str(panel_settings.get("bookings_prefix") or "EP").upper()) if p)
    return parts or ("EP",)


def poll_limits() -> tuple[int, int]:
    low = max(1, setting_int("poll_min_entries", MIN_ENTRIES))
    return low, max(low, setting_int("poll_max_entries", MAX_ENTRIES))


def disabled_message() -> str:
    return str(panel_settings.get("disabled_message") or "This function is switched off at the moment.")[:200]


def command_cooldown(name: str) -> int:
    if name in ("metar", "taf"):
        return max(0, setting_int("weather_cooldown", 5))
    if name == "bookings":
        return max(0, setting_int("bookings_cooldown", 10))
    return 0


class PlvaccTree(app_commands.CommandTree):
    """Before every command: is the function switched on in the panel, and is the user within the cooldown."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        command = interaction.command
        root = getattr(command, "root_parent", None) or command  # /mod warn -> mod
        name = root.name if root else ""
        feature = COMMAND_FEATURES.get(name)
        if feature and not feature_enabled(feature):
            await interaction.response.send_message(f"⛔ {disabled_message()}", ephemeral=True)
            return False
        seconds = command_cooldown(name)
        if seconds > 0:
            now = time.monotonic()
            key = (name, interaction.user.id)
            wait = _cooldowns.get(key, 0.0) - now
            if wait > 0:
                await interaction.response.send_message(f"Try again in {math.ceil(wait)} s.", ephemeral=True)
                return False
            if len(_cooldowns) > 2000:
                _cooldowns.clear()
            _cooldowns[key] = now + seconds
        return True

# ----------------------------------------------------------------------------
# DATABASE (SQLite - survives a restart of the bot)
# ----------------------------------------------------------------------------
db = sqlite3.connect(DB_PATH)
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS polls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        end_ts INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_by INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS entries (
        poll_id INTEGER NOT NULL,
        number INTEGER NOT NULL,
        vote_message_id INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS permissions (
        user_id INTEGER PRIMARY KEY,
        added_by INTEGER NOT NULL
    );
    """
)
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS mod_cases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER NOT NULL,
        action TEXT NOT NULL,
        target_id INTEGER NOT NULL,
        target_name TEXT NOT NULL DEFAULT '',
        mod_id INTEGER NOT NULL DEFAULT 0,
        mod_name TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        duration_s INTEGER,
        expires_ts INTEGER,
        active INTEGER NOT NULL DEFAULT 0,
        dm TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'discord',
        action_id TEXT NOT NULL DEFAULT '',
        reported INTEGER NOT NULL DEFAULT 0   -- 0 = to send to CoreOPS, 1 = sent, 2 = being carried out
    );
    """
)
try:  # older databases: the poll's header message (deleted after the results)
    db.execute("ALTER TABLE polls ADD COLUMN header_id INTEGER")
except sqlite3.OperationalError:
    pass
try:  # older databases: the server a case belongs to (the timers work per case)
    db.execute("ALTER TABLE mod_cases ADD COLUMN guild_id INTEGER NOT NULL DEFAULT 0")
except sqlite3.OperationalError:
    pass
db.commit()

# ----------------------------------------------------------------------------
# CLIENT
# ----------------------------------------------------------------------------
class PlvaccBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # without it the bot cannot see attachments of other people's messages
        super().__init__(intents=intents)
        self.tree = PlvaccTree(self)
        self.web: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.web = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20), headers={"User-Agent": USER_AGENT}
        )
        load_config_cache()
        await refresh_role_commands(sync=False)
        await sync_commands()
        check_polls.start()
        check_mod_expiry.start()
        coreops_sync.start()

    async def close(self):
        if self.web is not None:
            await self.web.close()
        await super().close()

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        log.info(
            "The bot is on %d server(s): %s",
            len(self.guilds),
            ", ".join(f"{g.name} ({g.id})" for g in self.guilds) or "none",
        )
        guild = get_guild()
        log.info(
            "Server used for panel actions and timers: %s (GUILD_ID from .env: %s)",
            f"{guild.name} ({guild.id})" if guild else "NOT FOUND",
            GUILD_ID or "not set",
        )
        if COREOPS_URL and COREOPS_TOKEN:
            log.info("CoreOPS panel: %s (checking every %d s)", COREOPS_URL, SYNC_SECONDS)
        else:
            log.warning("CoreOPS panel: DISABLED - COREOPS_URL and COREOPS_TOKEN are not both set in .env, so no settings are received.")


client = PlvaccBot()
tree = client.tree


class UserError(Exception):
    """An error that is shown to the user."""


# ----------------------------------------------------------------------------
# HELPERS: HTTP REQUESTS WITH A SHORT CACHE
# ----------------------------------------------------------------------------
_cache: dict[tuple, tuple[float, object]] = {}


async def fetch(url: str, params: dict | None = None, ttl: int = 60):
    """GET returning parsed JSON, or None when the API has no data (204 / empty answer)."""
    key = (url, tuple(sorted((params or {}).items())))
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        async with client.web.get(url, params=params) as response:
            if response.status == 429:
                raise UserError("API rate limit reached. Try again in a minute.")
            if response.status == 204:
                result = None
            else:
                response.raise_for_status()
                text = (await response.text()).strip()
                result = json.loads(text) if text else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
        log.warning("API %s unavailable: %s: %s", url, type(error).__name__, error)
        raise UserError("Could not fetch data from the API. Try again in a moment.") from error
    if len(_cache) > 500:
        _cache.clear()
    _cache[key] = (time.monotonic() + ttl, result)
    return result


# ----------------------------------------------------------------------------
# PERMISSIONS
# ----------------------------------------------------------------------------
def is_board(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and any(
        r.id == board_role_id() for r in interaction.user.roles
    )


def is_allowed(interaction: discord.Interaction) -> bool:
    if is_board(interaction):
        return True
    row = db.execute("SELECT 1 FROM permissions WHERE user_id=?", (interaction.user.id,)).fetchone()
    return row is not None


def extract_id(text: str) -> int:
    m = re.search(r"\d{17,20}", text)
    if not m:
        raise UserError("Give a valid user ID (or mention the user with @).")
    return int(m.group())


async def grant_access(interaction: discord.Interaction, text: str):
    if not is_board(interaction):
        return await interaction.response.send_message("❌ Only the **Board** role can do this.", ephemeral=True)
    try:
        uid = extract_id(text)
    except UserError as e:
        return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
    db.execute(
        "INSERT OR REPLACE INTO permissions (user_id, added_by) VALUES (?, ?)", (uid, interaction.user.id)
    )
    db.commit()
    await interaction.response.send_message(
        f"✅ <@{uid}> (`{uid}`) can now create polls.", ephemeral=True, allowed_mentions=NO_PINGS
    )


async def revoke_access(interaction: discord.Interaction, text: str):
    if not is_board(interaction):
        return await interaction.response.send_message("❌ Only the **Board** role can do this.", ephemeral=True)
    try:
        uid = extract_id(text)
    except UserError as e:
        return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
    db.execute("DELETE FROM permissions WHERE user_id=?", (uid,))
    db.commit()
    await interaction.response.send_message(
        f"✅ Permission removed: <@{uid}> (`{uid}`).", ephemeral=True, allowed_mentions=NO_PINGS
    )


async def list_access(interaction: discord.Interaction):
    if not is_board(interaction):
        return await interaction.response.send_message("❌ Only the **Board** role can do this.", ephemeral=True)
    rows = db.execute("SELECT user_id FROM permissions").fetchall()
    text = "\n".join(f"• <@{r[0]}> (`{r[0]}`)" for r in rows) or "No additional people (only the Board role)."
    await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_PINGS)


# ----------------------------------------------------------------------------
# WALLPAPER OF THE MONTH: CREATING A POLL
# ----------------------------------------------------------------------------
REF_RE = re.compile(r"discord(?:app)?\.com/channels/\d+/(\d+)/(\d+)|\b(\d{17,20})\b")


def parse_refs(text: str, default_channel_id: int) -> list[tuple[int, int]]:
    """Extracts (channel_id, message_id) from links and/or bare IDs."""
    refs = []
    for chan, msg, bare in REF_RE.findall(text):
        if bare:
            refs.append((default_channel_id, int(bare)))
        else:
            refs.append((int(chan), int(msg)))
    return refs


def parse_end(text: str) -> datetime:
    text = text.strip()
    for fmt, date_only in (("%d.%m.%Y %H:%M", False), ("%Y-%m-%d %H:%M", False), ("%d.%m.%Y", True)):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if date_only:
            dt = dt.replace(hour=23, minute=59)
        dt = dt.replace(tzinfo=TZ)
        if dt <= datetime.now(TZ):
            raise UserError("The end of the poll must be in the future.")
        return dt
    raise UserError("Wrong date format. Use e.g. `31.10.2026 20:00` (UTC) or `31.10.2026`.")


def resolve_channels(guild: discord.Guild, default_channel, default_source_id: int | None, text: str):
    """Returns (publishing channel, ID of the channel with the source messages)."""
    channel, source_id = default_channel, default_source_id
    if text.strip():
        try:
            ids = [int(part) for part in text.split(",")]
        except ValueError:
            raise UserError("Channel IDs must be numbers separated by a comma.")
        if len(ids) > 2:
            raise UserError("Give at most two channel IDs separated by a comma.")
        channel = guild.get_channel_or_thread(ids[0])
        if len(ids) == 2:
            source = guild.get_channel_or_thread(ids[1])
            if not isinstance(source, (discord.TextChannel, discord.Thread)):
                raise UserError("The source channel was not found on this server.")
            source_id = source.id
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        raise UserError("Give the ID of a text channel of this server.")
    if source_id is None:
        source_id = channel.id
    return channel, source_id

async def load_entries(guild: discord.Guild, refs):
    items = []
    for cid, mid in refs:
        ch = guild.get_channel_or_thread(cid)
        if ch is None:
            raise UserError(f"I cannot see the channel of message `{mid}`.")
        try:
            msg = await ch.fetch_message(mid)
        except discord.NotFound:
            raise UserError(f"Message `{mid}` was not found in {ch.mention}.")
        except discord.Forbidden:
            raise UserError(f"The bot has no access to {ch.mention}.")
        att = next(
            (
                a
                for a in msg.attachments
                if (a.content_type or "").startswith("image/")
                or a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
            ),
            None,
        )
        if att is None:
            raise UserError(
                f"Message `{mid}` has no image attached. "
                "The screenshot must be uploaded as an attachment, not pasted as a link."
            )
        items.append((msg, att))
    return items


async def create_poll(
    guild: discord.Guild,
    created_by: int,
    entries_text: str,
    end_text: str,
    channels_text: str,
    default_channel,
    default_source_id: int | None,
):
    """Creates a poll (shared by /wallpaper and the CoreOPS panel). Returns (number, channel, end as a timestamp)."""
    end_dt = parse_end(end_text)
    channel, source_id = resolve_channels(guild, default_channel, default_source_id, channels_text)
    refs = parse_refs(entries_text, source_id)
    low, high = poll_limits()
    if not (low <= len(refs) <= high):
        raise UserError(f"Give {low} to {high} messages (found: {len(refs)}).")
    if len(set(refs)) != len(refs):
        raise UserError("The list contains duplicates.")
    items = await load_entries(guild, refs)

    try:
        # download all the images first so nothing is sent if something fails
        prepared = []
        for n, (msg, att) in enumerate(items, 1):
            data = await att.read()
            ext = Path(att.filename).suffix.lower() or ".png"
            prepared.append((n, msg.author.id, f"wallpaper_{n}{ext}", data))

        end_ts = int(end_dt.timestamp())
        header = discord.Embed(
            title="📸 Wallpaper of the month",
            description=(
                f"Vote with the {VOTE_EMOJI} reaction under your favourite wallpaper.\n"
                f"The poll ends <t:{end_ts}:F> (<t:{end_ts}:R>)."
            ),
            color=EMBED_COLOR,
        )
        header.set_footer(text=ORG_NAME)
        header_message = await channel.send(embed=header, allowed_mentions=NO_PINGS)

        vote_ids = []
        for n, author_id, fname, data in prepared:
            file = discord.File(io.BytesIO(data), filename=fname)
            emb = discord.Embed(description=f"Author: <@{author_id}>", color=EMBED_COLOR)
            emb.set_image(url=f"attachment://{fname}")
            emb.set_footer(text=f"{ORG_NAME} • vote with the {VOTE_EMOJI} reaction")
            sent = await channel.send(embed=emb, file=file, allowed_mentions=NO_PINGS)
            await sent.add_reaction(VOTE_EMOJI)
            vote_ids.append((n, sent.id))
    except discord.HTTPException as error:
        log.exception("Error while posting the poll")
        raise UserError(
            f"Discord error: {error}\nCheck that the bot may send messages, "
            "embed links, attach files and add reactions in this channel."
        ) from error

    cur = db.execute(
        "INSERT INTO polls (guild_id, channel_id, end_ts, created_by, header_id) VALUES (?, ?, ?, ?, ?)",
        (guild.id, channel.id, end_ts, created_by, header_message.id),
    )
    poll_id = cur.lastrowid
    db.executemany(
        "INSERT INTO entries (poll_id, number, vote_message_id) VALUES (?, ?, ?)",
        [(poll_id, n, mid) for n, mid in vote_ids],
    )
    db.commit()
    return poll_id, channel, end_ts


class WallpaperCreateModal(discord.ui.Modal, title="New wallpaper poll"):
    entries = discord.ui.TextInput(
        label="Links to the messages with the screenshots",
        style=discord.TextStyle.paragraph,
        placeholder="Paste links (or IDs) of the messages with the screenshots, separated by a new line or space",
        max_length=3000,
    )
    end_input = discord.ui.TextInput(label="End: DD.MM.YYYY HH:MM (UTC)", max_length=30)
    channel_input = discord.ui.TextInput(
        label="Channel IDs: publish, source (optional)", required=False, max_length=45
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not feature_enabled("tapeta"):
            return await interaction.response.send_message(f"⛔ {disabled_message()}", ephemeral=True)
        if not is_allowed(interaction):
            return await interaction.response.send_message("You do not have permission.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            poll_id, channel, end_ts = await create_poll(
                interaction.guild,
                interaction.user.id,
                self.entries.value,
                self.end_input.value,
                self.channel_input.value,
                interaction.channel,
                interaction.channel_id,
            )
            await interaction.followup.send(
                f"✅ Poll **#{poll_id}** started in {channel.mention}. Ends <t:{end_ts}:F>.",
                ephemeral=True,
            )
        except UserError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
        except discord.HTTPException as e:
            log.exception("Error while posting the poll")
            await interaction.followup.send(
                f"❌ Discord error: {e}\nCheck that the bot may send messages, "
                "embed links, attach files and add reactions in this channel.",
                ephemeral=True,
            )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("Modal error", exc_info=error)


# ----------------------------------------------------------------------------
# WALLPAPER OF THE MONTH: LIST, CANCEL, FINISH AND RESULTS
# ----------------------------------------------------------------------------
async def show_active_polls(interaction: discord.Interaction):
    rows = db.execute(
        "SELECT id, channel_id, end_ts FROM polls WHERE status='active' AND guild_id=? ORDER BY end_ts",
        (interaction.guild_id,),
    ).fetchall()
    text = "\n".join(f"**#{i}** <#{c}>, ends <t:{e}:F>" for i, c, e in rows) or "No active polls."
    await interaction.response.send_message(text, ephemeral=True)


async def cancel_poll(interaction: discord.Interaction, poll_id: int):
    cur = db.execute(
        "UPDATE polls SET status='cancelled' WHERE id=? AND guild_id=? AND status='active'",
        (poll_id, interaction.guild_id),
    )
    db.commit()
    if cur.rowcount:
        await interaction.response.send_message(f"✅ Poll #{poll_id} cancelled.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ There is no such active poll.", ephemeral=True)


async def read_image(url: str, limit: int) -> bytes | None:
    """Downloads an image (to re-upload it in the results); None when impossible or over the limit."""
    try:
        async with client.web.get(url) as response:
            if response.status != 200:
                return None
            if response.content_length and response.content_length > limit:
                return None
            data = await response.read()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
    return data if 0 < len(data) <= limit else None


async def finish_poll(poll_id: int):
    row = db.execute("SELECT channel_id, header_id FROM polls WHERE id=?", (poll_id,)).fetchone()
    # mark it at once so a failure does not post the results again every 30 s
    db.execute("UPDATE polls SET status='done' WHERE id=?", (poll_id,))
    db.commit()
    if row is None:
        return
    header_id = row[1]

    channel = client.get_channel(row[0]) or await client.fetch_channel(row[0])
    entry_rows = db.execute(
        "SELECT number, vote_message_id FROM entries WHERE poll_id=? ORDER BY number", (poll_id,)
    ).fetchall()

    results = []
    for number, vote_msg_id in entry_rows:
        try:
            msg = await channel.fetch_message(vote_msg_id)
        except discord.NotFound:
            continue
        votes = sum(
            max(r.count - (1 if r.me else 0), 0) for r in msg.reactions if str(r.emoji) == VOTE_EMOJI
        )
        emb = msg.embeds[0] if msg.embeds else None
        if emb is None or emb.image is None:
            continue
        author = re.search(r"<@!?(\d+)>", emb.description or "")
        results.append(
            {
                "number": number,
                "votes": votes,
                "image": emb.image.url,
                "author_id": author.group(1) if author else None,
                "message": msg,
            }
        )

    if not results:
        await channel.send(
            "The poll has ended, but none of the entries could be read.", allowed_mentions=NO_PINGS
        )
        return

    results.sort(key=lambda r: (-r["votes"], r["number"]))
    top = results[:3]

    place, prev_votes = 0, None
    for idx, r in enumerate(top, 1):
        r["tie"] = prev_votes is not None and r["votes"] == prev_votes
        if r["votes"] != prev_votes:
            place, prev_votes = idx, r["votes"]
        r["place"] = place

    # Deleting the voting messages also deletes their attachments, so the winning images have to be uploaded again.
    # If that fails (or deleting is switched off in the panel) the results point at the old messages and nothing is deleted.
    files = {}
    if panel_settings.get("poll_delete_messages", True):
        guild = getattr(channel, "guild", None)
        budget = min(getattr(guild, "filesize_limit", 8 * 1024 * 1024), 24 * 1024 * 1024)
        for r in top:
            data = await read_image(r["image"], budget)
            if data is None:
                files = None
                break
            budget -= len(data)
            ext = Path(urlparse(r["image"]).path).suffix.lower() or ".png"
            files[r["number"]] = (f"place_{r['place']}_{r['number']}{ext}", data)

    embeds, attachments = [], []
    for r in top:
        emb = discord.Embed(
            title=f"{MEDALS[r['place']]} Place {r['place']}",
            description=(
                (f"Author: <@{r['author_id']}>\n" if r["author_id"] else "")
                + f"Votes: **{r['votes']}**"
                + (" (tie)" if r["tie"] else "")
            ),
            color=EMBED_COLOR,
        )
        if files:
            filename, data = files[r["number"]]
            attachments.append(discord.File(io.BytesIO(data), filename=filename))
            emb.set_image(url=f"attachment://{filename}")
        else:
            emb.set_image(url=r["image"])
        emb.set_footer(text=ORG_NAME)
        embeds.append(emb)

    kwargs = {"files": attachments} if attachments else {}
    await channel.send(
        content="🏆 **Wallpaper of the month - results**", embeds=embeds, allowed_mentions=NO_PINGS, **kwargs
    )

    if files:  # the results are in the channel with their own copies of the images - clean up the voting messages
        for r in results:
            try:
                await r["message"].delete()
            except discord.HTTPException:
                log.warning("Could not delete voting message %s", r["message"].id)
        if header_id:
            try:
                header = await channel.fetch_message(header_id)
                await header.delete()
            except discord.HTTPException:
                pass

@tasks.loop(seconds=30)
async def check_polls():
    rows = db.execute(
        "SELECT id FROM polls WHERE status='active' AND end_ts<=?", (int(time.time()),)
    ).fetchall()
    for (poll_id,) in rows:
        try:
            await finish_poll(poll_id)
        except Exception:
            log.exception("Could not finish poll #%s", poll_id)


@check_polls.before_loop
async def _before_check_polls():
    await client.wait_until_ready()


# ----------------------------------------------------------------------------
# WALLPAPER OF THE MONTH: /wallpaper MENU
# ----------------------------------------------------------------------------
class WallpaperActionModal(discord.ui.Modal):
    value = discord.ui.TextInput(label="Poll number or user ID", max_length=100)

    def __init__(self, action: str):
        super().__init__(title="Wallpaper: " + action)
        self.action = action

    async def on_submit(self, interaction: discord.Interaction):
        if self.action == "cancel":
            if not is_allowed(interaction):
                return await interaction.response.send_message("You do not have permission.", ephemeral=True)
            try:
                poll_id = int(self.value.value)
            except ValueError:
                return await interaction.response.send_message("Give the poll number.", ephemeral=True)
            await cancel_poll(interaction, poll_id)
        elif self.action == "grant":
            await grant_access(interaction, self.value.value)
        else:
            await revoke_access(interaction, self.value.value)


class WallpaperMenu(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not feature_enabled("tapeta"):
            await interaction.response.send_message(f"⛔ {disabled_message()}", ephemeral=True)
            return False
        if interaction.user.id != self.owner_id or not is_allowed(interaction):
            await interaction.response.send_message("You do not have access to this panel.", ephemeral=True)
            return False
        return True

    @discord.ui.select(
        placeholder="Choose what you want to do",
        options=[
            discord.SelectOption(label="Create a poll", value="create"),
            discord.SelectOption(label="Active polls", value="list"),
            discord.SelectOption(label="Cancel a poll", value="cancel"),
            discord.SelectOption(label="Allow a user (Board)", value="grant"),
            discord.SelectOption(label="Remove access (Board)", value="revoke"),
            discord.SelectOption(label="List of allowed users (Board)", value="access"),
        ],
    )
    async def choose(self, interaction: discord.Interaction, select: discord.ui.Select):
        action = select.values[0]
        if action in ("grant", "revoke", "access") and not is_board(interaction):
            return await interaction.response.send_message(
                "Only the Board role can manage access.", ephemeral=True
            )
        if action == "create":
            await interaction.response.send_modal(WallpaperCreateModal())
        elif action == "list":
            await show_active_polls(interaction)
        elif action == "access":
            await list_access(interaction)
        else:
            await interaction.response.send_modal(WallpaperActionModal(action))


@tree.command(name="wallpaper", description="Wallpaper of the month: polls and access management")
@app_commands.guild_only()
async def wallpaper(interaction: discord.Interaction):
    if not is_allowed(interaction):
        return await interaction.response.send_message("You do not have permission to manage wallpaper polls.", ephemeral=True)
    await interaction.response.send_message(
        "Choose an action:", view=WallpaperMenu(interaction.user.id), ephemeral=True
    )


# ----------------------------------------------------------------------------
# WEATHER: /metar and /taf (aviationweather.gov)
# ----------------------------------------------------------------------------
FLIGHT_CATEGORY_COLORS = {"VFR": 0x2ECC71, "MVFR": 0x3498DB, "IFR": 0xE74C3C, "LIFR": 0xC2185B}
# visibility right after the wind group in a raw METAR: metres or CAVOK
VISIBILITY_RE = re.compile(r"\b(?:\d{3}|VRB)\d{2,3}(?:G\d{2,3})?KT(?: \d{3}V\d{3})? (CAVOK|\d{4})\b")


def parse_icao(text: str) -> str:
    icao = text.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{4}", icao):
        raise UserError("Give a four-character ICAO code, e.g. EPWA.")
    return icao


def format_wind(m: dict) -> str:
    speed, gust, direction = m.get("wspd"), m.get("wgst"), m.get("wdir")
    if not speed:
        return "Calm"
    if direction == "VRB":
        text = f"variable / {speed} kt"
    elif isinstance(direction, (int, float)):
        text = f"{int(direction):03d}° / {speed} kt"
    else:
        text = f"{speed} kt"
    return text + (f"\ngusts {gust} kt" if gust else "")


def format_visibility(m: dict) -> str:
    match = VISIBILITY_RE.search(m.get("rawOb") or "")
    if match:
        if match.group(1) == "CAVOK":
            return "CAVOK"
        meters = int(match.group(1))
    else:
        visibility = m.get("visib")
        if visibility is None:
            return "—"
        if isinstance(visibility, str) and visibility.endswith("+"):
            return "10 km or more"
        try:
            meters = round(float(visibility) * 1609.344, -2)  # the API gives statute miles
        except (TypeError, ValueError):
            return str(visibility)
    if meters >= 9999:
        return "10 km or more"
    if meters >= 1000:
        return f"{meters / 1000:g} km"
    return f"{int(meters)} m"


def format_clouds(m: dict) -> str:
    if "CAVOK" in (m.get("rawOb") or "").split():
        return "CAVOK"
    parts = []
    for cloud in m.get("clouds") or []:
        cover, base = cloud.get("cover"), cloud.get("base")
        parts.append(f"{cover} {base:,} ft".replace(",", " ") if base is not None else str(cover))
    return "\n".join(parts) or "—"


def format_degrees(value) -> str:
    return f"{value:g}°C" if isinstance(value, (int, float)) else "—"


def build_metar_embed(m: dict, icao: str) -> discord.Embed:
    category = m.get("fltCat")
    embed = discord.Embed(
        title=f"🌤️ METAR • {m.get('icaoId') or icao}",
        color=FLIGHT_CATEGORY_COLORS.get(category, EMBED_COLOR),
    )
    description = f"**{m['name']}**" if m.get("name") else ""
    if isinstance(m.get("obsTime"), (int, float)):
        observed = datetime.fromtimestamp(m["obsTime"], TZ)
        description += f"\nObserved: {observed:%H:%M}Z, {observed:%d.%m} • <t:{int(m['obsTime'])}:R>"
    embed.description = description.strip() or None

    altimeter = m.get("altim")
    embed.add_field(name="💨 Wind", value=format_wind(m))
    embed.add_field(name="👁️ Visibility", value=format_visibility(m))
    embed.add_field(name="☁️ Clouds", value=format_clouds(m))
    embed.add_field(
        name="🌡️ Temp. / dew point",
        value=f"{format_degrees(m.get('temp'))} / {format_degrees(m.get('dewp'))}",
    )
    embed.add_field(name="🔽 QNH", value=f"{round(altimeter)} hPa" if altimeter is not None else "—")
    embed.add_field(name="✈️ Category", value=category or "—")
    if m.get("wxString"):
        embed.add_field(name="🌧️ Weather", value=f"`{m['wxString']}`", inline=False)
    embed.add_field(name="Report", value=f"```\n{m.get('rawOb', '—')}\n```", inline=False)
    embed.set_footer(text="Source: aviationweather.gov • times in UTC")
    return embed


def format_taf(raw: str) -> str:
    """Splits a TAF into lines: every BECMG / TEMPO / FM / PROBxx starts a new row."""
    text = re.sub(r"\s+(?=(?:BECMG|TEMPO|FM\d{6}|PROB\d{2})\b)", "\n  ", raw.strip())
    return re.sub(r"(PROB\d{2})\n\s+(TEMPO)", r"\1 \2", text)


def build_taf_embed(t: dict, icao: str) -> discord.Embed:
    lines = []
    if t.get("name"):
        lines.append(f"**{t['name']}**")
    if isinstance(t.get("validTimeFrom"), (int, float)) and isinstance(t.get("validTimeTo"), (int, float)):
        start, end = datetime.fromtimestamp(t["validTimeFrom"], TZ), datetime.fromtimestamp(t["validTimeTo"], TZ)
        lines.append(f"Valid: {start:%d.%m %H:%M}Z → {end:%d.%m %H:%M}Z")
    raw = format_taf(t.get("rawTAF") or "")
    embed = discord.Embed(
        title=f"🌦️ TAF • {t.get('icaoId') or icao}",
        description=("\n".join(lines) + "\n" if lines else "") + f"```\n{raw[:3500]}\n```",
        color=EMBED_COLOR,
    )
    embed.set_footer(text="Source: aviationweather.gov • times in UTC")
    return embed


async def send_weather(interaction: discord.Interaction, icao: str, product: str):
    try:
        station = parse_icao(icao)
    except UserError as error:
        return await interaction.response.send_message(f"❌ {error}", ephemeral=True)
    await interaction.response.defer()
    try:
        data = await fetch(f"{WEATHER_API}/{product}", {"ids": station, "format": "json"})
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise UserError(
                f"No current {product.upper()} for **{station}**. "
                "This airport may not issue this report - try another station."
            )
        builder = build_metar_embed if product == "metar" else build_taf_embed
        embed = builder(data[0], station)
    except UserError as error:
        return await interaction.followup.send(f"❌ {error}")
    await interaction.followup.send(embed=embed, allowed_mentions=NO_PINGS)


@tree.command(name="metar", description="Current METAR from aviationweather.gov")
@app_commands.describe(icao="Airport ICAO code, e.g. EPWA")
async def metar(interaction: discord.Interaction, icao: str):
    await send_weather(interaction, icao, "metar")


@tree.command(name="taf", description="Current TAF from aviationweather.gov")
@app_commands.describe(icao="Airport ICAO code, e.g. EPKK")
async def taf(interaction: discord.Interaction, icao: str):
    await send_weather(interaction, icao, "taf")


# ----------------------------------------------------------------------------
# ATC BOOKINGS: /bookings (Core VACC API through CoreOPS)
# ----------------------------------------------------------------------------
# booking type from the API -> (marker, name in the legend); order = order of the legend
BOOKING_TYPES = {
    "training": ("🔹", "Training session"),
    "exam": ("🔸", "Exam"),
    "booking": ("🟢", "Booking"),
    "event": ("🟣", "Event"),
}
BOOKING_FILTERS = {
    "all": None,
    "sessions": {"training", "exam"},
    "booking": {"booking"},
    "event": {"event"},
}
BOOKING_FIELDS_PER_EMBED = 24  # Discord allows 25 fields; 24 gives full rows of 3


def booking_time(value) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, TZ)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)


def parse_day(text: str | None) -> datetime:
    if not text:
        return datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=TZ)
        except ValueError:
            pass
    raise UserError("Give the date as DD.MM.YYYY or YYYY-MM-DD.")


async def load_polish_bookings(day: datetime) -> list[dict]:
    """Bookings with the configured callsign prefixes (EP by default) active at any moment of the day (UTC).

    The API filters by start date, so the previous day is fetched too - that is how sessions
    running through midnight are caught.
    """
    day_end = day + timedelta(days=1)
    found = {}
    for offset in (-1, 0):
        date = (day + timedelta(days=offset)).strftime("%Y-%m-%d")
        payload = await coreops_bookings(date)
        for item in payload:
            try:
                callsign = str(item["callsign"]).upper()
                if not callsign.startswith(booking_prefixes()):
                    continue
                booking = {
                    "callsign": callsign,
                    "cid": item.get("cid"),
                    "name": str(item.get("name") or "").strip(),
                    "type": str(item.get("type") or "booking").lower(),
                    "begins": booking_time(item["start"]),
                    "ends": booking_time(item["end"]),
                }
            except (KeyError, TypeError, ValueError, OverflowError):
                log.warning("Skipped an invalid booking: %r", item)
                continue
            if booking["begins"] < day_end and booking["ends"] > day:
                key = item.get("id", (callsign, booking["begins"]))
                found[key] = booking
    return sorted(found.values(), key=lambda b: (b["begins"], b["callsign"]))


def booking_marker(kind: str) -> str:
    return BOOKING_TYPES.get(kind, ("🔘", ""))[0]


def booking_hours(b: dict, day: datetime) -> str:
    def clock(moment: datetime) -> str:
        # time; with the date only when the start/end falls outside the day being viewed
        return f"{moment:%H:%M}" if moment.date() == day.date() else f"{moment:%Y-%m-%d %H:%M}"

    return f"{clock(b['begins'])} - {clock(b['ends'])} UTC"


def build_booking_embeds(day: datetime, rows: list[dict]) -> list[discord.Embed]:
    """Embeds laid out as: description with the type legend + fields in rows of 3 (callsign, CID, times)."""
    kinds = [k for k in BOOKING_TYPES if any(b["type"] == k for b in rows)]
    kinds += sorted({b["type"] for b in rows} - set(BOOKING_TYPES))
    description = f"Showing {len(rows)} {'booking' if len(rows) == 1 else 'bookings'} for date `{day:%Y-%m-%d}`."
    if kinds:
        legend = "\n".join(
            f"{booking_marker(k)} {BOOKING_TYPES[k][1] if k in BOOKING_TYPES else k.capitalize()}" for k in kinds
        )
        description += f"\nSession types:\n{legend}"

    now = datetime.now(TZ)
    chunks = [rows[i : i + BOOKING_FIELDS_PER_EMBED] for i in range(0, len(rows), BOOKING_FIELDS_PER_EMBED)]
    embeds = []
    for number, chunk in enumerate(chunks or [[]]):
        embed = discord.Embed(description=description if number == 0 else None, color=EMBED_COLOR, timestamp=now)
        for b in chunk:
            callsign = re.sub(r"[^A-Z0-9_]", "", b["callsign"])
            cid = str(b["cid"] or "")
            who = f"CID {cid}" if cid.isdigit() else "CID unknown"
            if b.get("name"):
                who = f"{b['name'][:40]} ({cid})"
            embed.add_field(
                name=f"{booking_marker(b['type'])} {callsign}",
                value=f"**{who}**\n{booking_hours(b, day)}",
            )
        embeds.append(embed)
    return embeds


@tree.command(name="bookings", description="ATC bookings in Poland (EP callsigns)")
@app_commands.describe(
    date="DD.MM.YYYY or YYYY-MM-DD; default today. The day is in UTC.",
    type="Which bookings to show; default all.",
)
@app_commands.choices(
    type=[
        app_commands.Choice(name="All", value="all"),
        app_commands.Choice(name="Training and exams", value="sessions"),
        app_commands.Choice(name="Normal bookings", value="booking"),
        app_commands.Choice(name="Events", value="event"),
    ]
)
async def bookings(
    interaction: discord.Interaction, date: str | None = None, type: app_commands.Choice[str] | None = None
):
    try:
        day = parse_day(date)
    except UserError as error:
        return await interaction.response.send_message(f"❌ {error}", ephemeral=True)
    await interaction.response.defer()
    try:
        rows = await load_polish_bookings(day)
    except UserError as error:
        return await interaction.followup.send(f"❌ {error}")

    allowed_types = BOOKING_FILTERS.get(type.value if type else str(panel_settings.get("bookings_default_type") or "all"))
    if allowed_types is not None:
        rows = [b for b in rows if b["type"] in allowed_types]

    for embed in build_booking_embeds(day, rows):
        await interaction.followup.send(embed=embed, allowed_mentions=NO_PINGS)


# ----------------------------------------------------------------------------
# PILOT ROLES: /role
# ----------------------------------------------------------------------------
@tree.command(name="role", description="Give yourself the Training Pilot or Exam Pilot role (or remove it)")
@app_commands.guild_only()
@app_commands.describe(pilot="Which role you want", action="Default: Add")
@app_commands.choices(
    pilot=[
        app_commands.Choice(name="Training Pilot", value="training"),
        app_commands.Choice(name="Exam Pilot", value="exam"),
    ],
    action=[app_commands.Choice(name="Add", value="add"), app_commands.Choice(name="Remove", value="remove")],
)
async def role(
    interaction: discord.Interaction,
    pilot: app_commands.Choice[str],
    action: app_commands.Choice[str] | None = None,
):
    await interaction.response.defer(ephemeral=True)
    selected = interaction.guild.get_role(pilot_role_id(pilot.value))
    member = interaction.user
    bot_member = interaction.guild.me
    if selected is None:
        return await interaction.followup.send("The role was not found. Check the configured ID.", ephemeral=True)
    if (
        not bot_member
        or not bot_member.guild_permissions.manage_roles
        or selected >= bot_member.top_role
        or selected.managed
    ):
        return await interaction.followup.send(
            "The bot needs the Manage Roles permission and its role must be above the pilot role.",
            ephemeral=True,
        )
    remove = action is not None and action.value == "remove"
    has_role = selected in member.roles
    if remove and not has_role:
        return await interaction.followup.send(f"You do not have the **{selected.name}** role.", ephemeral=True)
    if not remove and has_role:
        return await interaction.followup.send(f"You already have the **{selected.name}** role.", ephemeral=True)
    try:
        if remove:
            await member.remove_roles(selected, reason="Self-service /role")
        else:
            await member.add_roles(selected, reason="Self-service /role")
        await interaction.followup.send(
            f"✅ {'Removed' if remove else 'Added'} the **{selected.name}** role.",
            ephemeral=True,
            allowed_mentions=NO_PINGS,
        )
    except discord.HTTPException:
        log.exception("Could not change role %s", selected.id)
        await interaction.followup.send(
            "Could not change the role. Check the bot's permissions and try again.", ephemeral=True
        )


# ----------------------------------------------------------------------------
# MODERATION: /mod - cases go to the CoreOPS panel (log, screenshots), the punished person gets a private message
# ----------------------------------------------------------------------------
MOD_ACTIONS = {
    "warn": ("⚠️", "Warning"),
    "mute": ("🔇", "Mute"),
    "unmute": ("🔊", "Mute removed"),
    "timeout": ("⏸️", "Timeout"),
    "kick": ("👢", "Kick"),
    "ban": ("🔨", "Ban"),
    "unban": ("✅", "Ban removed"),
}
# titles of the private message sent to the punished person
MOD_DM_TITLES = {
    "warn": "Warning",
    "mute": "Mute",
    "unmute": "Mute removed",
    "timeout": "Timeout",
    "kick": "Removed from the server",
    "ban": "Ban",
    "unban": "Ban removed",
}
MOD_NEEDS_REASON = {"warn", "mute", "timeout", "kick", "ban"}
MOD_TAKES_DURATION = {"mute", "timeout", "ban"}
MAX_TIMEOUT_SECONDS = 28 * 86400
MAX_DURATION_SECONDS = 365 * 86400
DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text) -> int | None:
    """'30m', '12h', '7d', '2w' -> seconds; empty -> None (permanent)."""
    text = str(text or "").strip().lower()
    if not text:
        return None
    match = re.fullmatch(r"(\d{1,3})\s*([mhdw])", text)
    if not match or int(match.group(1)) == 0:
        raise UserError("Give the duration as a number with a unit: 30m, 12h, 7d or 2w (empty = permanent).")
    seconds = int(match.group(1)) * DURATION_UNITS[match.group(2)]
    if seconds > MAX_DURATION_SECONDS:
        raise UserError("The longest penalty is 365 days.")
    return seconds


def no_permission_text() -> str:
    """The reply to someone who may not moderate - and what the bot knows, so a wrong setup is easy to spot."""
    allowed = [board_role_id(), *sorted(moderator_role_ids())]
    lines = ["You do not have permission to moderate."]
    if moderator_role_ids():
        lines.append("Allowed roles (Board + moderator roles from CoreOPS): " + ", ".join(f"<@&{i}>" for i in allowed) + " - you have none of them.")
    else:
        lines.append(
            "No moderator role is set in CoreOPS (Configuration → Discord bot → Settings), "
            "so only the Board role and administrators may moderate."
        )
    if last_sync_ts:
        lines.append(f"Settings last received from CoreOPS <t:{int(last_sync_ts)}:R>.")
    else:
        lines.append(
            "The bot has not received any settings from CoreOPS yet"
            + (f" ({last_sync_error})" if last_sync_error else "")
            + " - check COREOPS_URL and COREOPS_TOKEN."
        )
    return "\n".join(lines)


def is_moderator(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if not isinstance(user, discord.Member):
        return False
    if is_board(interaction) or user.guild_permissions.administrator:
        return True
    allowed = moderator_role_ids()
    ok = bool(allowed) and any(r.id in allowed for r in user.roles)
    if not ok:
        log.info(
            "Moderation refused for %s (%s): their roles %s; moderator roles %s; Board role %s; settings last received %s",
            user, user.id, [r.id for r in user.roles], sorted(allowed), board_role_id(),
            int(last_sync_ts) or "never",
        )
    return ok


def mod_embed(guild: discord.Guild, action: str, case_id: int, reason: str, expires_ts: int | None, moderator: str):
    emoji = MOD_ACTIONS[action][0]
    title = MOD_DM_TITLES[action]
    embed = discord.Embed(title=f"{emoji} {title}", description=f"Server: **{guild.name}**", color=EMBED_COLOR)
    if reason:
        embed.add_field(name="Reason", value=reason[:1000], inline=False)
    if expires_ts:
        embed.add_field(name="In effect until", value=f"<t:{expires_ts}:F> (<t:{expires_ts}:R>)", inline=False)
    elif action in ("mute", "ban"):
        embed.add_field(name="Duration", value="permanent", inline=False)
    if panel_settings.get("mod_show_moderator") and moderator:
        embed.add_field(name="Moderator", value=moderator[:100], inline=False)
    appeal = str(panel_settings.get("mod_appeal_text") or "").strip()
    if appeal and action not in ("unmute", "unban"):
        embed.add_field(name="Appeal", value=appeal[:500], inline=False)
    embed.set_footer(text=f"{ORG_NAME} • #{case_id}")
    return embed


async def send_mod_dm(user_id: int, embed: discord.Embed) -> str:
    try:
        user = await client.fetch_user(user_id)
        await user.send(embed=embed)
        return "sent"
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        return "failed"


def drop_case(case_id: int) -> None:
    db.execute("DELETE FROM mod_cases WHERE id=?", (case_id,))
    db.commit()


async def apply_moderation(
    guild: discord.Guild,
    action: str,
    target_id: int,
    mod_id: int,
    mod_name: str,
    reason: str,
    duration_s: int | None,
    source: str,
    action_id: str = "",
    mod_member: discord.Member | None = None,
) -> tuple[int, str, str]:
    """Carries out the action, sends the private message and stores the case. Returns (case number, DM status, name)."""
    if action not in MOD_ACTIONS:
        raise UserError("Unknown moderation action.")
    reason = (reason or "").strip()[:500]
    if action in MOD_NEEDS_REASON and not reason:
        raise UserError("Give a reason.")
    if action not in MOD_TAKES_DURATION:
        duration_s = None
    if action == "timeout":
        if duration_s is None:
            raise UserError("A timeout needs a duration, e.g. 30m, 12h, 7d.")
        if duration_s > MAX_TIMEOUT_SECONDS:
            raise UserError("A timeout can last 28 days at most.")
    if (client.user and target_id == client.user.id) or (mod_id and target_id == mod_id):
        raise UserError("This person cannot be punished.")
    me = guild.me
    if me is None:
        raise UserError("The bot is not on the server.")

    member = None
    if action != "unban":
        member = guild.get_member(target_id)
        if member is None:
            try:
                member = await guild.fetch_member(target_id)
            except discord.NotFound:
                member = None
    if member is None and action not in ("ban", "unban"):
        raise UserError("This user is not on the server.")
    if member is not None and action in ("mute", "timeout", "kick", "ban"):
        if member.id == guild.owner_id:
            raise UserError("The server owner cannot be punished.")
        if member.top_role >= me.top_role:
            raise UserError("The bot's role is equal to or lower than this person's role - move the bot's role higher.")
        if mod_member is not None and mod_member.id != guild.owner_id and member.top_role >= mod_member.top_role:
            raise UserError("This person has a role equal to or higher than yours.")

    muted_role = None
    if action in ("mute", "unmute"):
        muted_role = guild.get_role(setting_id("muted_role_id", 0))
        if action == "mute" and muted_role is None:
            raise UserError("Set the Muted role in the CoreOPS panel (Configuration → Discord bot → Settings).")
        if muted_role is not None and (muted_role >= me.top_role or muted_role.managed):
            raise UserError("The bot cannot assign the Muted role - it is above the bot's role.")

    expires_ts = int(time.time()) + duration_s if duration_s else None
    target_name = str(member) if member else ""
    if not target_name:
        try:
            target_name = str(await client.fetch_user(target_id))
        except discord.HTTPException:
            target_name = str(target_id)

    cur = db.execute(
        "INSERT INTO mod_cases (ts, action, target_id, target_name, mod_id, mod_name, reason, duration_s, "
        "expires_ts, source, action_id, guild_id, reported) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2)",
        (int(time.time()), action, target_id, target_name, mod_id, mod_name, reason, duration_s, expires_ts, source, action_id, guild.id),
    )
    db.commit()
    case_id = cur.lastrowid
    embed = mod_embed(guild, action, case_id, reason, expires_ts, mod_name)
    audit_reason = (f"{mod_name}: {reason}" if reason else mod_name)[:400]
    dm = "failed"
    try:
        if action in ("kick", "ban"):
            # after a kick the bot no longer shares a server with the user - the message has to go first
            dm = await send_mod_dm(target_id, embed)
        if action == "mute":
            await member.add_roles(muted_role, reason=audit_reason)
        elif action == "unmute":
            if muted_role is not None and muted_role in member.roles:
                await member.remove_roles(muted_role, reason=audit_reason)
            if member.timed_out_until is not None:
                await member.timeout(None, reason=audit_reason)
        elif action == "timeout":
            await member.timeout(timedelta(seconds=duration_s), reason=audit_reason)
        elif action == "kick":
            await member.kick(reason=audit_reason)
        elif action == "ban":
            await guild.ban(discord.Object(target_id), reason=audit_reason, delete_message_seconds=0)
        elif action == "unban":
            try:
                await guild.unban(discord.Object(target_id), reason=audit_reason)
            except discord.NotFound:
                raise UserError("This user is not banned.")
        if action not in ("kick", "ban"):
            dm = await send_mod_dm(target_id, embed)
    except UserError:
        drop_case(case_id)
        raise
    except discord.Forbidden as error:
        drop_case(case_id)
        raise UserError(
            "The bot lacks permission for this action (check its permissions and the position of its role in the role list)."
        ) from error
    except Exception:
        drop_case(case_id)
        raise

    if action == "unmute":
        db.execute("UPDATE mod_cases SET active=0 WHERE target_id=? AND action='mute' AND active=1", (target_id,))
    elif action == "unban":
        db.execute("UPDATE mod_cases SET active=0 WHERE target_id=? AND action='ban' AND active=1", (target_id,))
    db.execute(
        "UPDATE mod_cases SET dm=?, active=?, reported=0 WHERE id=?",
        (dm, 1 if action in ("mute", "ban") else 0, case_id),
    )
    db.commit()
    return case_id, dm, target_name


def unreported_cases() -> list[dict]:
    rows = db.execute(
        "SELECT id, ts, action, target_id, target_name, mod_id, mod_name, reason, duration_s, expires_ts, dm, source, "
        "action_id FROM mod_cases WHERE reported=0 ORDER BY id LIMIT 50"
    ).fetchall()
    return [
        {
            "case_no": r[0], "ts": r[1], "action": r[2], "target_id": str(r[3]), "target_name": r[4],
            "moderator_id": str(r[5]), "moderator_name": r[6], "reason": r[7], "duration_s": r[8],
            "expires_ts": r[9], "dm": r[10], "source": r[11], "action_id": r[12],
        }
        for r in rows
    ]


def mark_cases_reported(case_ids: list[int]) -> None:
    for case_id in case_ids:
        db.execute("UPDATE mod_cases SET reported=1 WHERE id=? AND reported=0", (case_id,))
    db.commit()


async def run_mod_command(interaction: discord.Interaction, action: str, target, reason: str = "", duration=None):
    if not is_moderator(interaction):
        return await interaction.response.send_message(no_permission_text(), ephemeral=True, allowed_mentions=NO_PINGS)
    await interaction.response.defer(ephemeral=True)
    moderator = interaction.user if isinstance(interaction.user, discord.Member) else None
    try:
        case_id, dm, name = await apply_moderation(
            interaction.guild,
            action,
            target.id,
            interaction.user.id,
            str(interaction.user),
            reason or "",
            parse_duration(duration),
            "discord",
            "",
            moderator,
        )
    except UserError as error:
        return await interaction.followup.send(f"❌ {error}", ephemeral=True)
    except discord.HTTPException as error:
        log.exception("Moderation error")
        return await interaction.followup.send(f"❌ Discord error: {error}", ephemeral=True)
    dm_text = {
        "sent": "Private message sent.",
        "failed": "The private message could not be delivered (the user has DMs blocked).",
    }.get(dm, "")
    await interaction.followup.send(
        f"✅ {MOD_ACTIONS[action][1]}: **{name}** (`{target.id}`), case **#{case_id}**. {dm_text}",
        ephemeral=True,
        allowed_mentions=NO_PINGS,
    )


mod_group = app_commands.Group(name="mod", description="Server moderation", guild_only=True)


@mod_group.command(name="warn", description="Warn a user (with a private message)")
@app_commands.describe(user="Who to warn", reason="Reason")
async def mod_warn(interaction: discord.Interaction, user: discord.Member, reason: str):
    await run_mod_command(interaction, "warn", user, reason)


@mod_group.command(name="mute", description="Mute a user (Muted role)")
@app_commands.describe(user="Who to mute", reason="Reason", duration="Duration: 30m, 12h, 7d, 2w (empty = permanent)")
async def mod_mute(interaction: discord.Interaction, user: discord.Member, reason: str, duration: str | None = None):
    await run_mod_command(interaction, "mute", user, reason, duration)


@mod_group.command(name="unmute", description="Remove a mute and a timeout")
@app_commands.describe(user="Who to unmute", reason="Reason (optional)")
async def mod_unmute(interaction: discord.Interaction, user: discord.Member, reason: str | None = None):
    await run_mod_command(interaction, "unmute", user, reason or "")


@mod_group.command(name="timeout", description="Timeout (Discord's built-in timeout, up to 28 days)")
@app_commands.describe(user="Who", duration="Duration: 30m, 12h, 7d, 2w", reason="Reason")
async def mod_timeout(interaction: discord.Interaction, user: discord.Member, duration: str, reason: str):
    await run_mod_command(interaction, "timeout", user, reason, duration)


@mod_group.command(name="kick", description="Kick from the server")
@app_commands.describe(user="Who to kick", reason="Reason")
async def mod_kick(interaction: discord.Interaction, user: discord.Member, reason: str):
    await run_mod_command(interaction, "kick", user, reason)


@mod_group.command(name="ban", description="Ban a user (optionally for a time)")
@app_commands.describe(user="Who to ban", reason="Reason", duration="Duration: 30m, 12h, 7d, 2w (empty = permanent)")
async def mod_ban(interaction: discord.Interaction, user: discord.User, reason: str, duration: str | None = None):
    await run_mod_command(interaction, "ban", user, reason, duration)


@mod_group.command(name="unban", description="Remove a ban")
@app_commands.describe(user="Who to unban", reason="Reason (optional)")
async def mod_unban(interaction: discord.Interaction, user: discord.User, reason: str | None = None):
    await run_mod_command(interaction, "unban", user, reason or "")


@mod_group.command(name="history", description="A user's penalty history")
@app_commands.describe(user="Whose history to show")
async def mod_history(interaction: discord.Interaction, user: discord.User):
    if not is_moderator(interaction):
        return await interaction.response.send_message(no_permission_text(), ephemeral=True, allowed_mentions=NO_PINGS)
    rows = db.execute(
        "SELECT id, ts, action, reason, mod_name FROM mod_cases WHERE target_id=? AND reported!=2 ORDER BY id DESC LIMIT 10",
        (user.id,),
    ).fetchall()
    lines = [
        f"**#{i}** <t:{ts}:d> {MOD_ACTIONS.get(action, ('', action))[1]}: {(reason or '-')[:80]} ({mod[:30]})"
        for i, ts, action, reason, mod in rows
    ]
    await interaction.response.send_message(
        "\n".join(lines) or "No cases for this user.", ephemeral=True, allowed_mentions=NO_PINGS
    )


tree.add_command(mod_group)


@tasks.loop(seconds=30)
async def check_mod_expiry():
    """Automatic removal of mutes and temporary bans."""
    rows = db.execute(
        "SELECT id, action, target_id, guild_id FROM mod_cases WHERE active=1 AND expires_ts IS NOT NULL AND expires_ts<=?",
        (int(time.time()),),
    ).fetchall()
    for case_id, action, target_id, case_guild_id in rows:
        guild = (client.get_guild(case_guild_id) if case_guild_id else None) or get_guild()
        if guild is None:
            continue
        try:
            if action == "mute":
                role = guild.get_role(setting_id("muted_role_id", 0))
                member = guild.get_member(target_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(target_id)
                    except discord.NotFound:
                        member = None
                if member is not None and role is not None and role in member.roles:
                    await member.remove_roles(role, reason="The mute has expired")
                undo = "unmute"
            elif action == "ban":
                try:
                    await guild.unban(discord.Object(target_id), reason="The ban has expired")
                except discord.NotFound:
                    pass
                undo = "unban"
            else:
                undo = ""
        except discord.Forbidden:
            log.warning("No permission to remove the penalty automatically (case #%s)", case_id)
            undo = ""
        except discord.HTTPException:
            log.exception("Could not remove the penalty (case #%s), will retry", case_id)
            continue
        db.execute("UPDATE mod_cases SET active=0 WHERE id=?", (case_id,))
        if undo:
            db.execute(
                "INSERT INTO mod_cases (ts, action, target_id, mod_name, reason, source, guild_id, reported) "
                "VALUES (?, ?, ?, 'Automatic', ?, 'auto', ?, 0)",
                (int(time.time()), undo, target_id, f"The penalty of case #{case_id} has expired", guild.id),
            )
        db.commit()


@check_mod_expiry.before_loop
async def _before_check_mod_expiry():
    await client.wait_until_ready()


# ----------------------------------------------------------------------------
# CUSTOM ROLE COMMANDS: configured in CoreOPS (Configuration -> Discord bot -> Role commands)
# ----------------------------------------------------------------------------
RESERVED_COMMANDS = {"wallpaper", "metar", "taf", "bookings", "role", "mod"}
# roles with these permissions are never handed out by a command - a mistake in the panel must not give anyone admin
DANGEROUS_PERMISSIONS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks", "manage_messages",
    "kick_members", "ban_members", "moderate_members", "mention_everyone", "view_audit_log", "manage_nicknames",
)
dynamic_commands: dict[str, app_commands.Command] = {}
dynamic_signature = ""


def role_problem(guild: discord.Guild, role: discord.Role | None) -> str | None:
    if role is None:
        return "there is no such role on the server"
    if role.is_default() or role.managed:
        return "it is a system or integration role"
    reserved = {i for i in (board_role_id(), setting_id("muted_role_id", 0), *moderator_role_ids()) if i}
    if role.id in reserved:
        return "this role is reserved (Board / moderator / Muted)"
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return "the bot lacks the Manage Roles permission"
    if role >= me.top_role:
        return "the role is above the bot's role"
    if any(getattr(role.permissions, name, False) for name in DANGEROUS_PERMISSIONS):
        return "the role has administrative permissions - it cannot be handed out by a command"
    return None


def valid_role_specs() -> list[dict]:
    specs, seen = [], set()
    for raw in panel_role_commands:
        if not raw.get("enabled", True):
            continue
        name = str(raw.get("name") or "").strip().lower()
        role_id = str(raw.get("role_id") or "").strip()
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", name) or name in RESERVED_COMMANDS or name in seen or not role_id.isdigit():
            continue
        seen.add(name)
        mode = raw.get("mode") if raw.get("mode") in ("toggle", "add", "remove") else "toggle"
        required = str(raw.get("requires_role_id") or "").strip()
        specs.append(
            {
                "name": name,
                "description": str(raw.get("description") or "").strip()[:100] or "Give yourself a role",
                "role_id": int(role_id),
                "mode": mode,
                "requires_role_id": int(required) if required.isdigit() else 0,
            }
        )
    return specs[:20]


def role_command_report() -> list[dict]:
    guild = get_guild()
    out = []
    for spec in valid_role_specs():
        problem = role_problem(guild, guild.get_role(spec["role_id"])) if guild else "the bot cannot see the server"
        out.append({"name": spec["name"], "ok": problem is None, "problem": problem or ""})
    return out


async def handle_role_command(interaction: discord.Interaction, spec: dict):
    guild, member = interaction.guild, interaction.user
    if guild is None or not isinstance(member, discord.Member):
        return await interaction.response.send_message("This command only works on a server.", ephemeral=True)
    role = guild.get_role(spec["role_id"])
    problem = role_problem(guild, role)
    if problem:
        return await interaction.response.send_message(f"I cannot give this role: {problem}.", ephemeral=True)
    needed = spec["requires_role_id"]
    if needed and not any(r.id == needed for r in member.roles):
        return await interaction.response.send_message("This command is only available to members with the required role.", ephemeral=True)
    has_role = role in member.roles
    remove = spec["mode"] == "remove" or (spec["mode"] == "toggle" and has_role)
    if remove and not has_role:
        return await interaction.response.send_message(f"You do not have the **{role.name}** role.", ephemeral=True)
    if not remove and has_role:
        return await interaction.response.send_message(f"You already have the **{role.name}** role.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        if remove:
            await member.remove_roles(role, reason=f"Command /{spec['name']}")
        else:
            await member.add_roles(role, reason=f"Command /{spec['name']}")
    except discord.HTTPException:
        log.exception("Could not change role %s", role.id)
        return await interaction.followup.send("Could not change the role. Try again later.", ephemeral=True)
    await interaction.followup.send(
        f"✅ {'Removed' if remove else 'Added'} the **{role.name}** role.", ephemeral=True, allowed_mentions=NO_PINGS
    )


def make_role_command(spec: dict) -> app_commands.Command:
    async def callback(interaction: discord.Interaction):
        await handle_role_command(interaction, spec)

    callback = app_commands.guild_only(callback)
    return app_commands.Command(name=spec["name"], description=spec["description"], callback=callback)


async def refresh_role_commands(sync: bool) -> None:
    """Registers the role commands from the panel (adds new ones, removes deleted ones) and syncs with Discord when something changed."""
    global dynamic_signature
    specs = valid_role_specs()
    signature = json.dumps(specs, sort_keys=True)
    if signature == dynamic_signature:
        return
    guild = discord.Object(int(GUILD_ID)) if GUILD_ID.isdigit() else None
    for name in list(dynamic_commands):
        tree.remove_command(name, guild=guild)
        del dynamic_commands[name]
    for spec in specs:
        command = make_role_command(spec)
        tree.add_command(command, guild=guild, override=True)
        dynamic_commands[spec["name"]] = command
    dynamic_signature = signature
    if sync:
        try:
            await sync_commands()
        except discord.HTTPException:
            log.exception("Could not sync the role commands")


# ----------------------------------------------------------------------------
# BOOKINGS: from the Core VACC API, through CoreOPS (the Core VACC token stays in CoreOPS)
# ----------------------------------------------------------------------------
_booking_cache: dict[str, tuple[float, list]] = {}


async def coreops_bookings(date: str) -> list[dict]:
    if not (COREOPS_URL and COREOPS_TOKEN):
        raise UserError("Bookings come from Core VACC through CoreOPS - set COREOPS_URL and COREOPS_TOKEN in the bot's .env.")
    hit = _booking_cache.get(date)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        async with client.web.post(
            f"{COREOPS_URL}/api/bot.php",
            json={"op": "bookings", "date": date},
            headers={"Authorization": f"Bearer {COREOPS_TOKEN}"},
        ) as response:
            status = response.status
            data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
        log.warning("Bookings: no connection to CoreOPS: %s: %s", type(error).__name__, error)
        raise UserError("Could not connect to CoreOPS. Try again in a moment.") from error
    if status != 200 or not isinstance(data, dict) or not isinstance(data.get("bookings"), list):
        detail = str(data.get("detail") or data.get("error") or "") if isinstance(data, dict) else ""
        log.warning("Bookings from CoreOPS: HTTP %s %s", status, detail)
        raise UserError("Could not fetch the bookings from Core VACC (check the Core VACC API settings in CoreOPS).")
    if len(_booking_cache) > 50:
        _booking_cache.clear()
    _booking_cache[date] = (time.monotonic() + 60, data["bookings"])
    return data["bookings"]


# ----------------------------------------------------------------------------
# COREOPS PANEL: settings, state and commands (every SYNC_SECONDS)
# ----------------------------------------------------------------------------
pending_results: list[dict] = []  # command results to send back at the next exchange
last_sync_ts = 0.0  # when the settings were last received from CoreOPS
last_sync_error = ""  # why the last exchange failed (empty = it worked)
last_logged_rev = ""  # the settings revision that was last written to the log


_commands_copied = False


commands_error = ""  # why the slash commands could not be registered (empty = fine)


async def sync_commands() -> None:
    global _commands_copied, commands_error
    try:
        if GUILD_ID:
            guild = discord.Object(int(GUILD_ID))
            if not _commands_copied:  # copy_global_to must not be called twice (CommandAlreadyRegistered)
                tree.copy_global_to(guild=guild)
                _commands_copied = True
            await tree.sync(guild=guild)
        else:
            await tree.sync()
        commands_error = ""
    except (discord.HTTPException, app_commands.AppCommandError) as error:
        # do not let this kill the bot: it still has to connect to CoreOPS so the problem is visible in the panel
        commands_error = f"{type(error).__name__}: {error}"[:300]
        log.error("Could not register the slash commands: %s", commands_error)
        if GUILD_ID:
            log.error(
                "Is the bot on server %s, and was it invited with the 'bot' and 'applications.commands' scopes?", GUILD_ID
            )


async def guild_for_user(user_id: int) -> discord.Guild | None:
    """The configured server; with several servers and none configured: the only one the user is a member of."""
    guild = get_guild()
    if guild is not None:
        return guild
    found = []
    for candidate in client.guilds:
        member = candidate.get_member(user_id)
        if member is None:
            try:
                member = await candidate.fetch_member(user_id)
            except discord.HTTPException:
                member = None
        if member is not None:
            found.append(candidate)
    return found[0] if len(found) == 1 else None


def get_guild() -> discord.Guild | None:
    """The server the panel actions and the moderation timers work on: the Server ID from CoreOPS, else GUILD_ID from .env,
    else the only server the bot is on."""
    guild_id = setting_id("guild_id", 0) or (int(GUILD_ID) if GUILD_ID.isdigit() else 0)
    guild = client.get_guild(guild_id) if guild_id else None
    if guild is None and len(client.guilds) == 1:  # a wrong or missing ID does not matter when the bot is on one server only
        return client.guilds[0]
    return guild


def role_status(guild: discord.Guild | None, role_id: int) -> dict:
    role = guild.get_role(role_id) if guild else None
    me = guild.me if guild else None
    ok = bool(role and me and me.guild_permissions.manage_roles and role < me.top_role and not role.managed)
    return {"id": str(role_id), "name": role.name if role else None, "ok": ok}


def effective_roles() -> dict:
    """The roles the bot actually uses (after the settings from CoreOPS), with their names on the server."""
    guild = get_guild()
    out = {}
    for key, role_ids in (
        ("board", [board_role_id()]),
        ("moderator", role_id_list("moderator_role_id")),
        ("muted", [setting_id("muted_role_id", 0)]),
    ):
        role_ids = [i for i in role_ids if i]
        roles = [guild.get_role(i) if guild else None for i in role_ids]
        out[key] = {
            "id": ",".join(str(i) for i in role_ids),
            "name": ", ".join(r.name for r in roles if r) or None,
            "missing": [str(i) for i, r in zip(role_ids, roles) if r is None],
        }
    return out


def build_report() -> dict:
    guild = get_guild()
    latency = client.latency
    polls = [
        {"id": r[0], "channel_id": str(r[1]), "end_ts": r[2], "status": r[3], "entries": r[4]}
        for r in db.execute(
            "SELECT p.id, p.channel_id, p.end_ts, p.status, "
            "(SELECT COUNT(*) FROM entries e WHERE e.poll_id = p.id) "
            "FROM polls p ORDER BY (p.status = 'active') DESC, p.end_ts DESC LIMIT 20"
        )
    ]
    access = []
    for (uid,) in db.execute("SELECT user_id FROM permissions ORDER BY user_id"):
        user = client.get_user(uid)
        access.append({"user_id": str(uid), "name": str(user) if user else ""})
    return {
        "bot": {
            "name": str(client.user) if client.user else "",
            "version": VERSION,
            "started_at": STARTED_AT,
            "latency_ms": None if latency is None or math.isnan(latency) else round(latency * 1000),
            "guild": {"name": guild.name, "members": guild.member_count} if guild else None,
            "guilds": [{"id": str(g.id), "name": g.name} for g in client.guilds[:25]],  # to pick the Server ID from
            "commands_error": commands_error,
        },
        "polls": polls,
        "access": access,
        "roles": {kind: role_status(guild, pilot_role_id(kind)) for kind in PILOT_ROLES},
        "applied_rev": panel_rev,
        "effective": effective_roles(),
        "sync": {"last_ok_ts": int(last_sync_ts) or None, "last_error": last_sync_error},
        "role_commands": role_command_report(),
        "cases": unreported_cases(),
        "results": list(pending_results),
    }

async def run_action(action: dict) -> tuple[bool, str]:
    """Carries out a panel command. Returns (success, message shown in the panel)."""
    kind = str(action.get("type") or "")
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    if kind == "resync":
        await sync_commands()
        return True, "Slash commands re-registered."
    if kind == "poll_create":
        guild = get_guild()
        if guild is None:  # several servers and none configured: the publishing channel tells which one
            first = str(payload.get("channels", "")).split(",")[0].strip()
            guild = getattr(client.get_channel(int(first)), "guild", None) if first.isdigit() else None
        if guild is None:
            return False, "The bot cannot find the server - set the Server ID in CoreOPS (Discord bot -> Settings) or GUILD_ID in the bot's .env."
        poll_id, channel, _ = await create_poll(
            guild,
            0,  # 0 = created from the panel
            str(payload.get("entries", "")),
            str(payload.get("end", "")),
            str(payload.get("channels", "")),
            None,
            None,
        )
        return True, f"Poll #{poll_id} started in #{channel.name}."
    if kind in ("poll_cancel", "poll_finish"):
        try:
            poll_id = int(payload.get("poll_id", 0))
        except (TypeError, ValueError):
            return False, "Wrong poll number."
        row = db.execute("SELECT 1 FROM polls WHERE id=? AND status='active'", (poll_id,)).fetchone()
        if row is None:
            return False, f"There is no active poll #{poll_id}."
        if kind == "poll_cancel":
            db.execute("UPDATE polls SET status='cancelled' WHERE id=? AND status='active'", (poll_id,))
            db.commit()
            return True, f"Poll #{poll_id} cancelled."
        await finish_poll(poll_id)
        return True, f"Poll #{poll_id} finished, results posted."
    if kind in ("access_grant", "access_revoke"):
        uid = str(payload.get("user_id", ""))
        if not re.fullmatch(r"\d{17,20}", uid):
            return False, "Wrong user ID."
        if kind == "access_grant":
            db.execute("INSERT OR REPLACE INTO permissions (user_id, added_by) VALUES (?, 0)", (int(uid),))
        else:
            db.execute("DELETE FROM permissions WHERE user_id=?", (int(uid),))
        db.commit()
        return True, f"Poll access {'granted to' if kind == 'access_grant' else 'removed from'} {uid}."
    if kind == "mod_action":
        uid = str(payload.get("user_id", ""))
        if not re.fullmatch(r"\d{17,20}", uid):
            return False, "Wrong user ID."
        guild = await guild_for_user(int(uid))
        if guild is None:
            return False, "The bot is on several servers - set the Server ID in CoreOPS (Discord bot -> Settings) or GUILD_ID in the bot's .env."
        case_id, dm, name = await apply_moderation(
            guild,
            str(payload.get("action", "")),
            int(uid),
            0,  # 0 = from the CoreOPS panel
            f"CoreOPS: {str(payload.get('moderator') or 'panel')[:60]}",
            str(payload.get("reason", "")),
            parse_duration(payload.get("duration")),
            "panel",
            str(action.get("id") or ""),
        )
        dm_text = {"sent": "DM sent", "failed": "DM could not be delivered"}.get(dm, "")
        return True, f"Case #{case_id}: {name} ({uid}). {dm_text}".strip()
    return False, "Unknown command."

async def coreops_request(report: dict) -> dict | None:
    global last_sync_error
    try:
        async with client.web.post(
            f"{COREOPS_URL}/api/bot.php", json=report, headers={"Authorization": f"Bearer {COREOPS_TOKEN}"}
        ) as response:
            if response.status != 200:
                hint = {
                    401: " - wrong COREOPS_TOKEN (it must equal bot.api_token in config.php)",
                    403: " - the host firewall or allowed_ips in CoreOPS blocks this connection",
                    406: " - the CoreOPS host firewall blocked the request",
                    500: " - error on the CoreOPS side (details below and in the server log)",
                    503: " - bot.api_token is missing in CoreOPS config.php (min. 32 characters)",
                }.get(response.status, "")
                detail = ""
                try:
                    body = await response.json(content_type=None)
                    detail = str(body.get("detail") or body.get("error") or "")[:200]
                except (aiohttp.ClientError, ValueError, AttributeError):
                    pass
                log.warning("CoreOPS answered with code %s%s %s", response.status, hint, detail)
                last_sync_error = f"HTTP {response.status}{hint} {detail}".strip()
                return None
            data = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
        log.warning("No connection to CoreOPS: %s: %s", type(error).__name__, error)
        last_sync_error = f"no connection: {type(error).__name__}"
        return None
    if isinstance(data, dict) and data.get("ok"):
        return data
    last_sync_error = "CoreOPS answered, but not with a valid reply"
    return None


async def sync_with_coreops() -> int:
    """One exchange with the panel. Returns the number of commands carried out."""
    global last_sync_ts, last_sync_error
    report = build_report()
    sent = len(report["results"])
    data = await coreops_request(report)
    if data is None:
        return 0
    last_sync_ts, last_sync_error = time.time(), ""
    del pending_results[:sent]
    mark_cases_reported([c["case_no"] for c in report.get("cases", [])])
    config = data.get("config")
    if isinstance(config, dict):
        apply_panel_config(config)
        global last_logged_rev
        if panel_rev != last_logged_rev:
            last_logged_rev = panel_rev
            log.info(
                "Settings received from CoreOPS (revision %s): moderator roles %s, Board role %s, Muted role %s, server ID %s, functions off: %s",
                panel_rev,
                sorted(moderator_role_ids()) or "none",
                board_role_id(),
                setting_id("muted_role_id", 0) or "none",
                setting_id("guild_id", 0) or "none",
                [k for k, v in panel_features.items() if not v] or "none",
            )
        await refresh_role_commands(sync=True)
        try:
            CONFIG_CACHE.write_text(json.dumps(config), encoding="utf-8")
        except OSError:
            log.warning("Cannot write %s", CONFIG_CACHE)
    done = 0
    for action in data.get("actions") or []:
        if not isinstance(action, dict) or not action.get("id"):
            continue
        try:
            ok, message = await run_action(action)
        except UserError as error:
            ok, message = False, str(error)
        except Exception as error:  # one bad command must not stop the synchronisation
            log.exception("Panel command (%s) failed", action.get("type"))
            ok, message = False, f"Bot error: {type(error).__name__}"
        pending_results.append({"id": str(action["id"]), "ok": ok, "message": message[:280]})
        done += 1
    return done


@tasks.loop(seconds=SYNC_SECONDS)
async def coreops_sync():
    global last_sync_error
    if not (COREOPS_URL and COREOPS_TOKEN):
        return
    try:
        if await sync_with_coreops():
            await sync_with_coreops()  # send the results back at once so the panel does not wait another 15 s
    except Exception as error:
        log.exception("Synchronisation with CoreOPS failed")
        last_sync_error = f"{type(error).__name__}: {error}"[:200]


@coreops_sync.before_loop
async def _before_coreops_sync():
    await client.wait_until_ready()


# ----------------------------------------------------------------------------
# ERROR HANDLING AND START
# ----------------------------------------------------------------------------
@tree.error
async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        message = f"Try again in {error.retry_after:.0f} s."
    else:
        log.error("The command ended with an error", exc_info=error)
        message = "The command could not be carried out. Check the bot's log."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in the .env file next to bot.py.")
    if COREOPS_URL and not COREOPS_TOKEN:
        log.warning("COREOPS_URL is set but COREOPS_TOKEN is missing - the CoreOPS panel will not work.")
    if COREOPS_URL.startswith("http://") and not re.match(r"http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)", COREOPS_URL):
        log.warning("COREOPS_URL uses http - the API token travels in clear text. Use https.")
    client.run(TOKEN)