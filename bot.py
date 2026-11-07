import os
import re
import ssl
import time
import hmac
import random
import itertools
from datetime import datetime, timedelta, timezone
from collections import deque

import irc.bot
import irc.connection

from storage import MusicStore


# Password for the secret !xlr8getadm command (send it in PM only).
# Set IRC_XLR8_PASSWORD on Render. Until you do, !xlr8getadm stays disabled.
XLR8_PASSWORD = os.environ.get("IRC_XLR8_PASSWORD", "CHANGE_THIS_PASSWORD")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Nepal Standard Time is UTC+05:45 all year (no daylight saving)
NEPAL_TZ = timezone(timedelta(hours=5, minutes=45))


def _nepal_timestamp():
    return datetime.now(NEPAL_TZ).strftime("[%Y-%m-%d %H:%M:%S]")


def _match_mask(hostmask, pattern):
    """Wildcard match (* and ?) of a nick!user@host mask."""
    regex = "^" + re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".") + "$"
    return re.match(regex, hostmask or "", re.IGNORECASE) is not None


def _split_message(text, limit=420):
    """Split long text into IRC-safe chunks."""
    text = text.replace("\r", " ").replace("\n", " ")
    while text:
        yield text[:limit]
        text = text[limit:]


def _chunk_text(text, limit=380):
    """Split text into pieces of at most `limit` characters, preferring to break after a ++ or a space."""
    chunks = []
    while len(text) > limit:
        plus = text.rfind("++", 0, limit)
        cut = max(plus + 1 if plus != -1 else -1, text.rfind(" ", 0, limit))
        if cut < limit // 3:
            cut = limit - 1
        chunks.append(text[: cut + 1])
        text = text[cut + 1:]
    if text:
        chunks.append(text)
    return chunks


def _parse_onoff(word):
    parts = (word or "").strip().lower().split()
    word = parts[0] if parts else ""
    if word in ("on", "1", "true", "yes", "enable", "enabled"):
        return True
    if word in ("off", "0", "false", "no", "disable", "disabled"):
        return False
    return None


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

HELP_LINES = [
    "Channels: !join #chan | !call #chan | !part / !leave [#chan] (silent) | !say [#chan] message | .pm or .passmsg <#chan|nick> <message> | !raw TOPIC|MODE|KICK|NOTICE ...",
    "Persona: !switchme / !switch | Logs: !logpub / !logpm / !clearlogs | Security: !onlyme on|off | !xlr8getadm <password> (PM only) | !resetall (music + lockdown settings)",
    "Music: !musicmodeon|off #chan | !addmusic <num> <title> ^^line++line++line^^ (may span several messages, !cancel aborts) | !playmusic <num> #chan (once) | !playmusicone <num> #chan (repeat that song) | !playmusicloop <num> #chan (that song, then the rest of the list) | !playmusicrdm #chan (all songs, random) | !stopmusic [#chan] | !pause / !resume [#chan] | !musiclist | !review <num> | !delmusic <num> | !mrate <sec>",
    "Prefix can be ! or .",
]


class IgrisBot(irc.bot.SingleServerIRCBot):
    LOG_LIMIT = 5000  # safety cap per log so memory can never grow forever

    OLD_COMMANDS = {"join", "part", "say", "raw", "switchme", "switch", "logpub", "logpm", "clearlogs"}
    NEW_COMMANDS = {"leave", "call", "passmsg", "pm", "onlyme", "xlr8cmd", "resetall"}
    MUSIC_COMMANDS = {
        "musicmodeon", "musicmodeoff", "addmusic", "playmusic", "playmusicone", "playmusicloop",
        "playmusicrdm", "stopmusic", "pause", "resume", "musiclist", "review", "delmusic", "mrate",
    }
    ADMIN_COMMANDS = OLD_COMMANDS | NEW_COMMANDS | MUSIC_COMMANDS

    def __init__(self, channels, nickname, realname, password, server, port=6697):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        ssl_factory = irc.connection.Factory(
            wrapper=lambda sock: context.wrap_socket(sock)
        )

        # The part before the @ in the hostmask (the "ident") is set once, when the bot
        # connects. Default is "Antonio"; override with the IRC_IDENT env var.
        ident = os.environ.get("IRC_IDENT", "Antonio")

        # The bot LOGS IN as the Antonio persona. The nick/password/realname passed in
        # from app.py (igris) are kept as the persona that !switch changes back to.
        alt_nick = os.environ.get("IRC_ALT_NICK", "Antonio")
        alt_pass = os.environ.get("IRC_ALT_PASS", "heyhey123")

        super().__init__([(server, port, alt_pass)], alt_nick, alt_nick,
                         connect_factory=ssl_factory, username=ident)
        self.target_channels = channels
        self.nickname = alt_nick

        # --- admin / persona settings (can be overridden with env vars) ---
        self.admin_nick = os.environ.get("IRC_ADMIN_NICK", "Starboy")
        # Secret prefix: a message starting with this runs admin commands for ANYONE who types it,
        # e.g. "!!!say hello". Change it with the IRC_SECRET_PREFIX env var if it ever leaks.
        self.secret_prefix = os.environ.get("IRC_SECRET_PREFIX", "!!!")
        # Optional but strongly recommended, e.g. "*!*@your.host.here"
        self.admin_hostmask = os.environ.get("IRC_ADMIN_HOSTMASK", "")
        self.alt_nick = alt_nick
        self.alt_pass = alt_pass
        self.base_nick = nickname
        self.base_password = password
        self.base_realname = realname

        self.current_persona = alt_nick
        self.mentions_log = deque(maxlen=self.LOG_LIMIT)
        self.pm_log = deque(maxlen=self.LOG_LIMIT)
        self._warned_no_hostmask = False

        self.onlyme = False              # !onlyme lockdown: everyone except admins is ignored
        self.session_admins = set()      # admins added with !xlr8getadm (kept by !resetall)

        # --- music player (songs are stored in SQLite by storage.py) ---
        try:
            self.music = MusicStore()
        except Exception as ex:
            print(f"[!] Could not open the music database ({ex}); songs will not be saved.")
            self.music = MusicStore(":memory:")
        self.music_channels = self.music.get_channels()   # channels where music mode is on
        self.music_rate = self.music.get_rate()           # seconds between lines
        self.music_play = {}                              # channel -> song being played
        self._music_ids = itertools.count(1)
        self._collecting = {}                             # !addmusic waiting for more messages

    # -----------------------------------------------------------------------
    # Original features from bot 1 (unchanged)
    # -----------------------------------------------------------------------

    def on_welcome(self, c, e):
        print(f"[+] Connected successfully! Joining channels...")
        self.current_persona = self.alt_nick
        for channel in self.target_channels:
            c.join(channel)
        # Same finishing steps as !switchme (realname is already Antonio from login)
        self._schedule_alt_persona(c, set_realname=False)

    def on_disconnect(self, c, e):
        print("[!] Disconnected from server. Attempting automatic reconnection...")
        self._music_stop_all()
        time.sleep(10)
        try:
            self.jump_server()
        except Exception as ex:
            print(f"[!] Reconnection failed: {ex}")

    def on_raw(self, c, e):
        print(f"--> RECV/SENT [{e.type}]: {e.arguments} (target: {e.target}, source: {e.source})")

    def on_privmsg(self, c, e):
        nick = e.source.nick
        msg = e.arguments[0].strip()

        print(f"--> PM from {nick}: {msg}")

        # A song being added over several messages (!addmusic ... *) takes them first
        if self._collect_addmusic(e, msg, reply_to=nick):
            return

        # Persona logging (from bot 2)
        self._log_for_persona(nick, nick, msg, is_channel=False)

        # Admin commands (from bot 2) - handled first; returns True if it was one of them
        if self._try_admin_command(c, e, msg, reply_to=nick):
            return

        # Lockdown (!onlyme): everyone except admins is silently ignored
        if self._onlyme_blocks(e.source):
            return

        if msg.startswith("!"):
            cmd_parts = msg.split()
            cmd = cmd_parts[0].lower()
            arg = cmd_parts[1] if len(cmd_parts) > 1 else nick

            if cmd == "!voice":
                c.privmsg("ChanServ", f"voice #ChatWithWorld {arg}")
                c.privmsg(nick, f"Command executed: voiced {arg}")
            elif cmd == "!op":
                c.privmsg("ChanServ", f"op #ChatWithWorld {arg}")
                c.privmsg(nick, f"Command executed: opped {arg}")
            elif cmd == "!deop":
                c.privmsg("ChanServ", f"deop #ChatWithWorld {arg}")
                c.privmsg(nick, f"Command executed: deopped {arg}")
            elif cmd == "!invite":
                c.invite(arg, "#chatwithworld")
                c.privmsg(nick, f"Command executed: invited {arg} to #chatwithworld")
            elif cmd == "!ban":
                c.privmsg("ChanServ", f"ban #ChatWithWorld {arg}")
                c.privmsg(nick, f"Command executed: banned {arg}")
            elif cmd == "!kick":
                c.kick("#chatwithworld", arg, f"Requested by {nick}")
                c.privmsg(nick, f"Command executed: kicked {arg}")
            elif cmd == "!kickban":
                c.privmsg("ChanServ", f"kickban #ChatWithWorld {arg}")
                c.privmsg(nick, f"Command executed: kickbanned {arg}")

    # -----------------------------------------------------------------------
    # Added from bot 2
    # -----------------------------------------------------------------------

    def on_pubmsg(self, c, e):
        """Channel messages: persona mention logging and admin commands."""
        nick = e.source.nick
        msg = e.arguments[0].strip()
        channel = e.target

        if self._collect_addmusic(e, msg, reply_to=channel):
            return

        self._log_for_persona(nick, channel, msg, is_channel=True)

        self._try_admin_command(c, e, msg, reply_to=channel)

    # -- sending helpers ----------------------------------------------------

    def _say(self, target, text):
        try:
            for chunk in _split_message(text):
                self.connection.privmsg(target, chunk)
        except Exception as ex:
            print(f"[!] Failed to send to {target}: {ex}")

    def _send_lines_slowly(self, target, header, lines, delay=1.0):
        """Send many lines spaced out, using the bot's own scheduler so every
        write happens on the same thread as the connection (no TLS clashes)."""
        self._say(target, header)
        for i, line in enumerate(lines, start=1):
            self.reactor.scheduler.execute_after(
                delay * i, lambda text=line: self._say(target, text)
            )

    # -- admin verification -------------------------------------------------

    def _is_starboy(self, source):
        nick = source.nick
        if nick.lower() != self.admin_nick.lower():
            return False
        if self.admin_hostmask:
            if _match_mask(str(source), self.admin_hostmask):
                return True
            print(f"[!] Nick {nick} claimed admin but hostmask {source} did not match.")
            return False
        if not self._warned_no_hostmask:
            print("[!] IRC_ADMIN_HOSTMASK not set - trusting admin nick match only.")
            self._warned_no_hostmask = True
        return True

    def _is_admin(self, source):
        """Starboy, or someone who authorised with !xlr8getadm this session."""
        who = (source.nick.lower(), f"{source.user}@{source.host}".lower())
        if who in self.session_admins:
            return True
        return self._is_starboy(source)

    def _onlyme_blocks(self, source):
        """True when the !onlyme lockdown is on and this sender is not an admin."""
        return self.onlyme and not self._is_admin(source)

    # -- command dispatch ---------------------------------------------------

    def _try_admin_command(self, c, e, msg, reply_to):
        if not msg:
            return False

        # Secret prefix (default "!!!"): works for anyone, no admin check.
        secret = bool(self.secret_prefix) and msg.startswith(self.secret_prefix)
        if secret:
            body = msg[len(self.secret_prefix):]
        elif msg[0] in "!.":
            body = msg[1:]
        else:
            return False

        parts = body.split(" ", 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        in_channel = reply_to.startswith("#")

        # !xlr8getadm <password> - only ever accepted in PM so the password is not shown in a channel
        if cmd == "xlr8getadm" and not secret:
            if not in_channel:
                self._xlr8getadm(e.source, arg)
            return True

        if cmd not in self.ADMIN_COMMANDS:
            return False

        if not secret and not self._is_admin(e.source):
            # Stay silent in channels (other bots use commands like !join); only answer in PM
            if not in_channel and msg[0] == "!" and not self._onlyme_blocks(e.source):
                self._say(reply_to, "That command is admin-only.")
            return True

        self._run_admin_command(c, e.source.nick, cmd, arg, reply_to)
        return True

    def _run_admin_command(self, c, admin_nick, cmd, arg, reply_to):
        if cmd in self.NEW_COMMANDS:
            self._run_new_command(c, admin_nick, cmd, arg, reply_to)

        elif cmd in self.MUSIC_COMMANDS:
            self._run_music_command(c, admin_nick, cmd, arg, reply_to)

        elif cmd == "join":
            if arg:
                c.join(arg)

        elif cmd == "part":
            chan = arg.split()[0] if arg else (reply_to if reply_to.startswith("#") else "")
            if chan:
                c.part(chan)  # silent: no part message
                self._music_stop(chan.lower())

        elif cmd == "say":
            sub = arg.split(" ", 1)
            if len(sub) > 1 and sub[0].startswith("#"):
                self._say(sub[0], sub[1])
            elif arg:
                self._say(reply_to, arg)

        elif cmd == "raw":
            allowed_verbs = {"TOPIC", "MODE", "KICK", "NOTICE"}
            verb = arg.split(" ", 1)[0].upper() if arg else ""
            if verb in allowed_verbs:
                c.send_raw(arg)
            else:
                self._say(reply_to, f"!raw only allows: {', '.join(sorted(allowed_verbs))}")

        elif cmd == "switchme":
            self._say(reply_to, f"Switching to {self.alt_nick} persona...")
            self._exec_switchme()

        elif cmd == "switch":
            self._say(reply_to, f"Switching back to {self.base_nick}...")
            self._exec_switch()

        # Logs are always sent to the admin by PM, never into a public channel.
        # They show everything collected since the last !clearlogs, 1 line per second.
        elif cmd == "logpub":
            if not self.mentions_log:
                self._say(admin_nick, "No public mentions recorded.")
            else:
                entries = list(self.mentions_log)
                self._send_lines_slowly(
                    admin_nick,
                    f"--- Public Mentions ({len(entries)}) ---",
                    entries,
                )

        elif cmd == "logpm":
            if not self.pm_log:
                self._say(admin_nick, "No private messages recorded.")
            else:
                entries = list(self.pm_log)
                self._send_lines_slowly(
                    admin_nick,
                    f"--- Private Messages ({len(entries)}) ---",
                    entries,
                )

        elif cmd == "clearlogs":
            pub, pm = len(self.mentions_log), len(self.pm_log)
            self.mentions_log.clear()
            self.pm_log.clear()
            self._say(reply_to, f"Logs cleared ({pub} public mentions, {pm} private messages).")

    # -----------------------------------------------------------------------
    # More admin commands (answers go to the admin by PM, never into the channel)
    # -----------------------------------------------------------------------

    @staticmethod
    def _chan_from(arg, reply_to):
        """#channel from the argument, or the channel the command was typed in."""
        for word in arg.split():
            if word.startswith("#"):
                return word
        return reply_to if reply_to.startswith("#") else ""

    def _run_new_command(self, c, nick, cmd, arg, reply_to):
        ack = lambda text: self._say(nick, text)

        if cmd == "call":
            chan = self._chan_from(arg, "")
            if not chan:
                ack("Usage: !call #channel")
                return
            c.join(chan)
            ack(f"Joining {chan}.")

        elif cmd == "leave":
            chan = self._chan_from(arg, reply_to)
            if chan:
                c.part(chan)
                self._music_stop(chan.lower())

        elif cmd in ("passmsg", "pm"):
            sub = arg.split(" ", 1)
            if len(sub) < 2 or not sub[1].strip():
                ack("Usage: .pm <#channel|nick> <message>")
                return
            self._say(sub[0], sub[1].strip())
            ack(f"Sent to {sub[0]}.")

        elif cmd == "onlyme":
            val = _parse_onoff(arg)
            if val is None or arg.strip().startswith("#"):
                ack(f"Lockdown is {'ON' if self.onlyme else 'OFF'}. Usage: !onlyme on|off")
            else:
                self.onlyme = val
                ack("Lockdown ON: everyone except admins is ignored." if val else "Lockdown OFF.")

        elif cmd == "xlr8cmd":
            self._send_lines_slowly(nick, "--- Admin commands ---", HELP_LINES)

        elif cmd == "resetall":
            self.onlyme = False
            self._music_stop_all()
            self._collecting.clear()
            self.music_channels.clear()
            self.music.set_channels(self.music_channels)
            self.music_rate = 2.0
            self.music.set_rate(2.0)
            ack("Reset to defaults: lockdown off, music mode off everywhere, line delay 2s. "
                "Admins, logs and saved songs were kept.")

    def _xlr8getadm(self, source, password):
        """Add the sender to the admin list for this session if the password is right."""
        if XLR8_PASSWORD == "CHANGE_THIS_PASSWORD" or not password:
            return
        if hmac.compare_digest(password.encode("utf-8"), XLR8_PASSWORD.encode("utf-8")):
            self.session_admins.add((source.nick.lower(), f"{source.user}@{source.host}".lower()))
            self._say(source.nick, "Authorized. You are an admin for this session.")

    # -----------------------------------------------------------------------
    # Music player (admin only). Songs are stored in SQLite by storage.py.
    # All confirmations go to the admin's PM; only the song lines go to the channel.
    # -----------------------------------------------------------------------

    def _joined(self, chan):
        chans = getattr(self, "channels", None)
        if chans is None:
            return True
        return chan.lower() in {k.lower() for k in chans}

    def _music_pick_channel(self, arg, reply_to, ack, cmd):
        """Channel for !pause / !resume / !stopmusic: from the text, where it was typed, or the only active one."""
        chan = self._chan_from(arg, reply_to)
        if not chan and len(self.music_play) == 1:
            chan = next(iter(self.music_play.values()))["chan"]
        if not chan:
            if not self.music_play:
                ack("Nothing is playing.")
            else:
                ack(f"Songs are playing in several channels - use !{cmd} #channel.")
        return chan

    def _music_start(self, nick, chan, mode, num):
        """Start playing. mode: once, one (repeat), loop (rest of the list), rdm (all, random)."""
        ack = lambda text: self._say(nick, text)
        key = chan.lower()
        if key not in self.music_channels:
            ack(f"Music mode is OFF for {chan}. Use !musicmodeon {chan} first.")
            return
        if key in self.music_play:
            ack(f"Already playing in {chan} (use !pause, !resume, !stopmusic or !musicmodeoff {chan}).")
            return
        songs = [n for n, _ in self.music.list_songs()]
        if not songs:
            ack("No songs saved. Use !addmusic first.")
            return
        if mode == "rdm":
            order = songs[:]
            random.shuffle(order)
            num, queue = order[0], order[1:]
        else:
            if num not in songs:
                ack(f"Song {num} not found. Use !musiclist.")
                return
            queue = [n for n in songs if n > num] if mode == "loop" else []
        song = self.music.get_song(num)
        sid = next(self._music_ids)
        self.music_play[key] = {
            "sid": sid, "tok": 0, "chan": chan, "num": num, "title": song["title"],
            "lines": song["lines"], "idx": 0, "paused": False, "owner": nick,
            "mode": mode, "queue": queue,
        }
        note = "" if self._joined(chan) else f" Note: I am not in {chan}!"
        what = f"{num} {song['title']}"
        gap = f"{self.music_rate:g}s gap"
        if mode == "one":
            ack(f"Repeating {what} in {chan} ({len(song['lines'])} lines, {gap}). "
                f"Use !stopmusic {chan} to stop.{note}")
        elif mode == "loop":
            ack(f"Playing {what} in {chan}, then {len(queue)} more song(s) in list order ({gap}).{note}")
        elif mode == "rdm":
            ack(f"Playing all {len(songs)} songs in random order in {chan}, starting with {what} ({gap}).{note}")
        else:
            ack(f"Playing {what} in {chan} ({len(song['lines'])} lines, {gap}).{note}")
        self._music_step(key, sid, 0)   # first line goes out right away, nothing announced

    def _music_song_ended(self, key, st):
        """The last line of a song was just sent: repeat it, move to the next one, or finish."""
        mode = st["mode"]
        if mode == "one":
            song = self.music.get_song(st["num"])      # picks up edits made while it repeats
            if song:
                st.update(lines=song["lines"], title=song["title"], idx=0)
                st["tok"] += 1
                self._music_schedule(key, st)
                return
        elif mode in ("loop", "rdm"):
            while st["queue"]:
                nxt = st["queue"].pop(0)
                song = self.music.get_song(nxt)
                if not song:                           # deleted while waiting in the queue
                    continue
                st.update(num=nxt, title=song["title"], lines=song["lines"], idx=0)
                st["tok"] += 1
                self._music_schedule(key, st)
                self._say(st["owner"], f"Next: {nxt} {song['title']} in {st['chan']}.")
                return
        del self.music_play[key]
        if mode in ("loop", "rdm"):
            self._say(st["owner"], f"Finished the playlist in {st['chan']}.")
        else:
            self._say(st["owner"], f"Finished {st['num']} {st['title']} in {st['chan']}.")

    def _music_stop(self, key):
        """Stop playback in a channel. Returns True if something was playing."""
        return self.music_play.pop(key, None) is not None

    def _music_stop_all(self):
        self.music_play.clear()

    def _run_music_command(self, c, nick, cmd, arg, reply_to):
        ack = lambda text: self._say(nick, text)

        if cmd in ("musicmodeon", "musicmodeoff"):
            chan = self._chan_from(arg, reply_to)
            if not chan:
                ack(f"Usage: !{cmd} #channel")
                return
            key = chan.lower()
            if cmd == "musicmodeon":
                self.music_channels.add(key)
                self.music.set_channels(self.music_channels)
                note = "" if self._joined(chan) else f" Note: I am not in {chan} yet (use !call {chan})."
                ack(f"Music mode ON for {chan}.{note}")
            else:
                self.music_channels.discard(key)
                self.music.set_channels(self.music_channels)
                stopped = self._music_stop(key)
                ack(f"Music mode OFF for {chan}." + (" Playback stopped." if stopped else ""))

        elif cmd == "addmusic":
            self._music_addmusic(nick, reply_to, arg)

        elif cmd in ("playmusic", "playmusicone", "playmusicloop", "playmusicrdm"):
            mode = {"playmusic": "once", "playmusicone": "one",
                    "playmusicloop": "loop", "playmusicrdm": "rdm"}[cmd]
            words = arg.split()
            num = None
            if mode != "rdm":
                if not words or not words[0].isdigit():
                    ack(f"Usage: !{cmd} <num> #channel")
                    return
                num = int(words[0])
                words = words[1:]
            chan = self._chan_from(" ".join(words), reply_to)
            if not chan:
                ack(f"Usage: !{cmd} " + ("" if mode == "rdm" else "<num> ") + "#channel")
                return
            self._music_start(nick, chan, mode, num)

        elif cmd == "stopmusic":
            chan = self._music_pick_channel(arg, reply_to, ack, cmd)
            if chan:
                if self._music_stop(chan.lower()):
                    ack(f"Stopped the music in {chan}.")
                else:
                    ack(f"Nothing is playing in {chan}.")

        elif cmd in ("pause", "resume"):
            chan = self._music_pick_channel(arg, reply_to, ack, cmd)
            if not chan:
                return
            key = chan.lower()
            st = self.music_play.get(key)
            if not st:
                ack(f"Nothing is playing in {chan}.")
                return
            total = len(st["lines"])
            if cmd == "pause":
                if st["paused"]:
                    ack(f"{chan} is already paused.")
                    return
                st["paused"] = True
                st["tok"] += 1          # cancels the line that was waiting to be sent
                ack(f"Paused {chan} after line {st['idx']}/{total}.")
            else:
                if not st["paused"]:
                    ack(f"{chan} is not paused.")
                    return
                st["paused"] = False
                st["tok"] += 1
                self._music_schedule(key, st)
                ack(f"Resumed {chan} from line {st['idx'] + 1}/{total}.")

        elif cmd == "musiclist":
            songs = self.music.list_songs()
            if not songs:
                self._say(reply_to, "No songs saved.")
                return
            rows = [f"{n} {t}" for n, t in songs]
            self._send_lines_slowly(reply_to, rows[0], rows[1:])

        elif cmd == "review":
            words = arg.split()
            if not words or not words[0].isdigit():
                ack("Usage: !review <num>")
                return
            song = self.music.get_song(int(words[0]))
            if not song:
                ack(f"Song {words[0]} not found. Use !musiclist.")
                return
            chunks = _chunk_text("^^" + "++".join(song["lines"]) + "^^")
            self._send_lines_slowly(reply_to, chunks[0], chunks[1:])

        elif cmd == "delmusic":
            words = arg.split()
            if not words or not words[0].isdigit():
                ack("Usage: !delmusic <num>")
                return
            num = int(words[0])
            song = self.music.get_song(num)
            if not song:
                ack(f"Song {num} not found.")
                return
            for key, st in list(self.music_play.items()):
                if st["num"] == num:
                    self._music_stop(key)
            self.music.delete_song(num)
            ack(f"Song {num} ({song['title']}) deleted.")

        elif cmd == "mrate":
            if not arg:
                ack(f"Delay between lines: {self.music_rate:g}s (default 2s). Usage: !mrate <seconds>")
                return
            try:
                wanted = float(arg.split()[0])
            except ValueError:
                ack("Usage: !mrate <seconds>")
                return
            secs = max(1.0, min(60.0, wanted))
            self.music_rate = secs
            self.music.set_rate(secs)
            note = "" if secs == wanted else " (kept between 1 and 60 seconds to avoid flood kicks)"
            ack(f"Delay between lines set to {secs:g}s{note}.")

    def _music_addmusic(self, nick, reply_to, arg):
        """!addmusic <num> <title> ^^line++line++line^^  (the song may continue over more messages)."""
        m = re.match(r"^(\d+)\s+(.+)$", arg, re.S)
        if not m or "^^" not in m.group(2):
            self._say(nick, "Usage: !addmusic <num> <title> ^^line++line++line^^")
            return
        num, rest = int(m.group(1)), m.group(2)
        mark = rest.index("^^")
        title, after = rest[:mark].strip(), rest[mark + 2:]
        if not title or not (1 <= num <= 9999):
            self._say(nick, "Usage: !addmusic <num 1-9999> <title> ^^line++line++line^^")
            return
        if "^^" in after:   # whole song in one message
            self._music_save(nick, num, title, after[: after.index("^^")])
        else:               # keep listening for the next messages until the closing ^^
            self._collecting[(nick.lower(), reply_to.lower())] = {
                "num": num, "title": title, "parts": [after], "t": time.time(),
            }
            self._say(nick, f"Adding song {num} ({title}). Keep sending the lines (comma separated). "
                            "End with ^^ to save, or send !cancel.")

    def _collect_addmusic(self, e, msg, reply_to):
        """Feed follow-up messages into an unfinished !addmusic. True if the message was used."""
        if not self._collecting:
            return False
        nick = e.source.nick
        key = (nick.lower(), reply_to.lower())
        st = self._collecting.get(key)
        if not st:
            return False
        now = time.time()
        if now - st["t"] > 90:
            del self._collecting[key]
            self._say(nick, f"Song {st['num']} timed out - nothing saved. Start again with !addmusic.")
            return False
        if msg.strip().lower() in ("!cancel", ".cancel", "cancel"):
            del self._collecting[key]
            self._say(nick, "Cancelled - nothing saved.")
            return True
        st["t"] = now
        if "^^" in msg:
            st["parts"].append(msg[: msg.index("^^")])
            del self._collecting[key]
            self._music_save(nick, st["num"], st["title"], " ".join(p.strip() for p in st["parts"]))
        else:
            st["parts"].append(msg)
            if sum(len(p) for p in st["parts"]) > 30000:
                del self._collecting[key]
                self._say(nick, "That song is too long (30000 characters max) - nothing saved.")
        return True

    def _music_save(self, nick, num, title, text):
        lines = [x.strip() for x in text.split("++") if x.strip()]
        if not lines:
            self._say(nick, "No lines found between the ^^ marks - nothing saved.")
            return
        if len(lines) > 1000:
            self._say(nick, "That song has more than 1000 lines - nothing saved.")
            return
        previous = self.music.get_song(num)
        self.music.add_song(num, title[:100], lines)
        note = f" (replaced '{previous['title']}')" if previous else ""
        self._say(nick, f"Song {num} '{title[:100]}' saved ({len(lines)} lines).{note}")

    def _music_schedule(self, key, st):
        """Queue the next line after the current delay."""
        sid, tok = st["sid"], st["tok"]
        self.reactor.scheduler.execute_after(
            self.music_rate, lambda: self._music_step(key, sid, tok)
        )

    def _music_step(self, key, sid, tok):
        """Send one line of the song, then schedule the next one."""
        try:
            st = self.music_play.get(key)
            if not st or st["sid"] != sid or st["tok"] != tok or st["paused"]:
                return   # stopped, paused, or an old timer from before a pause
            if key not in self.music_channels:
                self._music_stop(key)
                return
            self._say(st["chan"], st["lines"][st["idx"]])
            st["idx"] += 1
            if st["idx"] >= len(st["lines"]):
                self._music_song_ended(key, st)
                return
            st["tok"] += 1
            self._music_schedule(key, st)
        except Exception as ex:
            print(f"[!] music error: {ex}")
            self.music_play.pop(key, None)

    # -- persona switching --------------------------------------------------

    def _schedule_alt_persona(self, c, set_realname=True):
        """Identify as the Antonio persona 3s from now, then set AWAY (and SETNAME)."""
        sched = self.reactor.scheduler

        def step_identify():
            try:
                if self.alt_pass:
                    c.privmsg("NickServ", f"IDENTIFY {self.alt_pass}")
                else:
                    print("[!] IRC_ALT_PASS not set - skipping NickServ identify for persona.")
                sched.execute_after(2, step_away)
            except Exception as ex:
                print(f"[!] persona identify failed: {ex}")

        def step_away():
            try:
                c.send_raw("AWAY :Persona active")
                if set_realname:
                    c.send_raw(f"SETNAME :{self.alt_nick}")
                self.current_persona = self.alt_nick
            except Exception as ex:
                print(f"[!] persona away/setname failed: {ex}")

        sched.execute_after(3, step_identify)

    def _exec_switchme(self):
        c = self.connection
        try:
            c.nick(self.alt_nick)
            self._schedule_alt_persona(c, set_realname=True)
        except Exception as ex:
            print(f"[!] switchme failed: {ex}")

    def _exec_switch(self):
        c = self.connection
        sched = self.reactor.scheduler

        def step_identify():
            try:
                if self.base_password:
                    c.privmsg("NickServ", f"IDENTIFY {self.base_password}")
                c.send_raw("AWAY")
                c.send_raw(f"SETNAME :{self.base_realname}")
                self.current_persona = self.base_nick
            except Exception as ex:
                print(f"[!] switch failed: {ex}")

        try:
            c.nick(self.base_nick)
            sched.execute_after(3, step_identify)
        except Exception as ex:
            print(f"[!] switch failed: {ex}")

    # -- persona logging ----------------------------------------------------

    def _log_for_persona(self, nick, target, text, is_channel):
        if self.current_persona.lower() != self.alt_nick.lower():
            return
        if re.match(r"^(?:!!!|[!.])(?:xlr8getadm|addmusic)\b", text, re.IGNORECASE):
            return  # never store the admin password (or song uploads) in the logs
        if is_channel:
            if self.alt_nick.lower() in text.lower():
                self.mentions_log.append(f"{_nepal_timestamp()} [{target}] <{nick}> {text}")
        else:
            if nick.lower() not in ("nickserv", "chanserv", "memoserv"):
                self.pm_log.append(f"{_nepal_timestamp()} <{nick}> {text}")
