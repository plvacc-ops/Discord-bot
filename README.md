# Bot Discord Polish VACC

Funkcje: `/tapeta` (głosowania na tapetę miesiąca), `/metar`, `/taf`, `/bookings`, `/rola`.
Bot jest sterowany z portalu CoreOPS: **Configuration → Discord bot** (włączanie funkcji, ustawienia, głosowania, lista uprawnionych, status).

> Bot to stale działający proces Pythona. Nie uruchamia się go na hostingu WWW ani w GitHub Actions - tylko na hostingu botów
> (np. bot-hosting.net), VPS albo komputerze, który jest włączony cały czas.

## Wdrożenie na bot-hosting.net (krok po kroku)

1. Załóż serwer: język **Python**, wersja **3.10 lub nowsza** (jeśli panel pozwala wybrać).
2. Zakładka **Startup**:
   - repozytorium: `https://github.com/plvacc-ops/Discord-bot`, gałąź `main` (repozytorium jest publiczne - login i token niepotrzebne),
   - plik startowy (Bot Python file): `bot.py`.
   Przy imporcie z GitHuba serwer musi być pusty - usuń domyślne pliki, potem **Reinstall Server**.
   Alternatywa: wgraj pliki ręcznie (File Manager / SFTP) do katalogu głównego serwera.
3. Pakiety (`discord.py`, `aiohttp`, `python-dotenv`) instalują się z `requirements.txt`. Jeśli nie, wpisz je w „Additional Python packages”:
   `discord.py aiohttp python-dotenv`.
4. Sekrety - **to jedyna rzecz, którą trzeba wpisać ręcznie**. W File Manager utwórz plik `.env` (wzór: `.env.example`) albo dodaj te same nazwy w menedżerze zmiennych:
   ```
   DISCORD_TOKEN=...        (Discord Developer Portal -> Bot -> Reset Token)
   GUILD_ID=...             (ID serwera Discord)
   COREOPS_URL=https://adres-portalu-coreops
   COREOPS_TOKEN=...        (ten sam, co 'bot' => ['api_token' => ...] w config/config.php CoreOPS)
   ```
5. Uruchom serwer. W CoreOPS (Configuration → Discord bot) status zmieni się na **online** w ciągu kilkunastu sekund.

W Discord Developer Portal włącz **Message Content Intent**. Bot potrzebuje na serwerze: wysyłanie wiadomości, osadzanie linków,
załączniki, dodawanie reakcji i (dla `/rola`) Zarządzanie rolami - jego rola musi być ponad rolami Training/Exam Pilot.

## Połączenie z CoreOPS

Bot **sam łączy się** z CoreOPS (co 15 s, `POST /api/bot.php`, nagłówek `Authorization: Bearer <token>`): wysyła stan (czy działa,
głosowania, lista uprawnionych), a odbiera ustawienia i polecenia z panelu. Nie trzeba otwierać żadnych portów, a token Discorda
nigdy nie trafia do CoreOPS. Gdy CoreOPS jest niedostępny, bot działa dalej z ostatnimi ustawieniami (`coreops_config.json`).
Bez `COREOPS_URL` i `COREOPS_TOKEN` bot działa samodzielnie (wszystkie funkcje włączone).

Token API wygeneruj w CoreOPS: `php -r "echo bin2hex(random_bytes(32));"` i wpisz go w `config/config.php` portalu oraz w `.env` bota.

## Pliki

| Plik | Znaczenie |
| --- | --- |
| `bot.py` | cały bot |
| `requirements.txt` | pakiety Pythona |
| `.env` | tajne ustawienia - tworzysz sam na serwerze, **nigdy nie trafia do GitHuba** (`.gitignore`) |
| `tapeta.db` | baza SQLite głosowań i uprawnień (powstaje sama). **„Reinstall Server” czyści pliki - zrób wcześniej kopię** |
| `coreops_config.json` | ostatnie ustawienia z panelu (powstaje sama) |

## Aktualizacja

Zmiany w kodzie trafiają na serwer po restarcie (bot-hosting.net pobiera repozytorium przy każdym starcie). `.env` i `tapeta.db`
nie są w repozytorium, więc aktualizacja ich nie nadpisuje.
