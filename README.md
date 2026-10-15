# Polish VACC Discord bot

Commands: `/wallpaper` (wallpaper of the month polls), `/metar`, `/taf`, `/bookings`, `/role`, `/mod ...` and custom role commands.
The bot is controlled from the CoreOPS portal: **Configuration → Discord bot** (functions, settings, polls, allowed users, role commands, status)
and **Configuration → Bot moderation** (case log and screenshots).

> The bot is a permanently running Python process. It cannot run on web hosting or in GitHub Actions - use a bot host
> (e.g. bot-hosting.net), a VPS or a computer that is always on.

## Commands

| Command | What it does |
| --- | --- |
| `/wallpaper` | wallpaper of the month polls; after the results the bot **deletes the voting messages** (the winning images are re-uploaded in the results) |
| `/metar`, `/taf` | weather from aviationweather.gov |
| `/bookings` | ATC bookings from the **Core VACC API** (through CoreOPS - the Core VACC token stays in CoreOPS) |
| `/role` | Training Pilot / Exam Pilot roles |
| `/mod warn / mute / unmute / timeout / kick / ban / unban / history` | moderation: the penalty, a private message to the punished person, a case in the CoreOPS log |
| custom role commands | defined in CoreOPS (Configuration → Discord bot → Role commands) |

**Moderation.** `/mod` commands are for the Board role, the moderator role (set in CoreOPS) and administrators. Every penalty gets a case number,
shows up in **CoreOPS → Configuration → Bot moderation** (where screenshots can be attached) and is sent to the punished person in a private message.
`mute` uses the Muted role (its ID is set in CoreOPS; the role has to deny sending messages and speaking in your channels), `timeout` is Discord's
built-in timeout (up to 28 days). Mutes and temporary bans are removed automatically. Bot permissions: Manage Roles, Kick Members, Ban Members,
Moderate Members; the bot's role must be **above** the roles of the people it punishes and above the Muted role.

**Custom role commands.** In CoreOPS you define the command name, description, role and mode (toggle / add only / remove only), optionally a role
required to use it. The bot registers them with Discord after you save. Roles with administrative permissions (Administrator, Manage Server / Roles /
Channels / Messages, kick, ban, timeout...) are never handed out by a command.

## Deployment on bot-hosting.net (step by step)

1. Create a server: language **Python**, version **3.10 or newer** (if the panel lets you choose).
2. **Startup** tab:
   - repository: `https://github.com/plvacc-ops/Discord-bot`, branch `main` (the repository is public - no login or token needed),
   - startup file (Bot Python file): `bot.py`.

   For a GitHub import the server has to be empty - delete the default files, then **Reinstall Server**.
   Alternative: upload the files by hand (File Manager / SFTP) to the root directory of the server.
3. The packages (`discord.py`, `aiohttp`, `python-dotenv`) are installed from `requirements.txt`. If not, enter them in "Additional Python packages":
   `discord.py aiohttp python-dotenv`.
4. Secrets - **the only thing to type in by hand**. In the File Manager create a `.env` file (template: `.env.example`) or add the same names in the
   panel's variable manager:
   ```
   DISCORD_TOKEN=...        (Discord Developer Portal -> Bot -> Reset Token)
   GUILD_ID=...             (ID of the Discord server)
   COREOPS_URL=https://address-of-the-coreops-portal
   COREOPS_TOKEN=...        (the same as 'bot' => ['api_token' => ...] in CoreOPS config/config.php)
   ```
5. Start the server. In CoreOPS (Configuration → Discord bot) the status changes to **online** within a few seconds.

In the Discord Developer Portal switch on the **Message Content Intent**. On the server the bot needs: send messages, embed links, attach files,
add reactions, Manage Roles (for `/role`, role commands and mutes), Kick Members, Ban Members and Moderate Members.

## Connection with CoreOPS

The bot **connects to CoreOPS itself** (every 15 s, `POST /api/bot.php`, header `Authorization: Bearer <token>`): it sends its state (running,
polls, allowed users, cases) and receives the settings and commands from the panel. No ports have to be opened and the Discord token never goes
to CoreOPS. If CoreOPS is unreachable the bot keeps working with the last settings (`coreops_config.json`).
Without `COREOPS_URL` and `COREOPS_TOKEN` the bot runs on its own (all functions on; `/bookings` needs CoreOPS).

Generate the API token in CoreOPS' shell: `php -r "echo bin2hex(random_bytes(32));"` and put it in the portal's `config/config.php` and in the bot's `.env`.

## Files

| File | Meaning |
| --- | --- |
| `bot.py` | the whole bot |
| `requirements.txt` | Python packages |
| `.env` | secrets - you create it on the server, **it never goes to GitHub** (`.gitignore`) |
| `tapeta.db` | SQLite database of polls, allowed users and moderation cases (created automatically). **"Reinstall Server" wipes the files - back it up first** |
| `coreops_config.json` | the last settings from the panel (created automatically) |

## Updating

Code changes reach the server after a restart (bot-hosting.net pulls the repository on every start). `.env` and `tapeta.db` are not in the
repository, so an update does not overwrite them.
