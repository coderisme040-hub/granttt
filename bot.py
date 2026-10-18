import os
import re
import ssl
import time
from datetime import datetime, timedelta, timezone
from collections import deque

import irc.bot
import irc.connection


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


class IgrisBot(irc.bot.SingleServerIRCBot):
    # Commands taken from the second bot. Admin (Starboy) only.
    LOG_LIMIT = 5000  # safety cap per log so memory can never grow forever
    ADMIN_COMMANDS = {"join", "part", "say", "raw", "switchme", "switch", "logpub", "logpm", "clearlogs"}

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

        super().__init__([(server, port, password)], nickname, realname,
                         connect_factory=ssl_factory, username=ident)
        self.target_channels = channels
        self.nickname = nickname

        # --- admin / persona settings (can be overridden with env vars) ---
        self.admin_nick = os.environ.get("IRC_ADMIN_NICK", "Starboy")
        # Optional but strongly recommended, e.g. "*!*@your.host.here"
        self.admin_hostmask = os.environ.get("IRC_ADMIN_HOSTMASK", "")
        self.alt_nick = os.environ.get("IRC_ALT_NICK", "Antonio")
        self.alt_pass = os.environ.get("IRC_ALT_PASS", "heyhey123")
        self.base_nick = nickname
        self.base_password = password
        self.base_realname = realname

        self.current_persona = nickname
        self.mentions_log = deque(maxlen=self.LOG_LIMIT)
        self.pm_log = deque(maxlen=self.LOG_LIMIT)
        self._warned_no_hostmask = False

    # -----------------------------------------------------------------------
    # Original features from bot 1 (unchanged)
    # -----------------------------------------------------------------------

    def on_welcome(self, c, e):
        print(f"[+] Connected successfully! Joining channels...")
        self.current_persona = self.base_nick
        for channel in self.target_channels:
            c.join(channel)

    def on_disconnect(self, c, e):
        print("[!] Disconnected from server. Attempting automatic reconnection...")
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

        # Persona logging (from bot 2)
        self._log_for_persona(nick, nick, msg, is_channel=False)

        # Admin commands (from bot 2) - handled first; returns True if it was one of them
        if msg.startswith("!") and self._try_admin_command(c, e, msg, reply_to=nick):
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
        """Channel messages: persona mention logging + admin commands."""
        nick = e.source.nick
        msg = e.arguments[0].strip()
        channel = e.target

        self._log_for_persona(nick, channel, msg, is_channel=True)

        if msg.startswith("!"):
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

    def _is_admin(self, source):
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

    # -- command dispatch ---------------------------------------------------

    def _try_admin_command(self, c, e, msg, reply_to):
        parts = msg.split(" ", 1)
        cmd = parts[0][1:].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd not in self.ADMIN_COMMANDS:
            return False

        if not self._is_admin(e.source):
            self._say(reply_to, "That command is admin-only.")
            return True

        self._run_admin_command(c, e.source.nick, cmd, arg, reply_to)
        return True

    def _run_admin_command(self, c, admin_nick, cmd, arg, reply_to):
        if cmd == "join":
            if arg:
                c.join(arg)

        elif cmd == "part":
            if arg:
                c.part(arg)

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

    # -- persona switching --------------------------------------------------

    def _exec_switchme(self):
        c = self.connection
        sched = self.reactor.scheduler

        def step_identify():
            try:
                if self.alt_pass:
                    c.privmsg("NickServ", f"IDENTIFY {self.alt_pass}")
                else:
                    print("[!] IRC_ALT_PASS not set - skipping NickServ identify for persona.")
                sched.execute_after(2, step_away)
            except Exception as ex:
                print(f"[!] switchme failed: {ex}")

        def step_away():
            try:
                c.send_raw("AWAY :Persona active")
                c.send_raw(f"SETNAME :{self.alt_nick}")
                self.current_persona = self.alt_nick
            except Exception as ex:
                print(f"[!] switchme failed: {ex}")

        try:
            c.nick(self.alt_nick)
            sched.execute_after(3, step_identify)
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
        if is_channel:
            if self.alt_nick.lower() in text.lower():
                self.mentions_log.append(f"{_nepal_timestamp()} [{target}] <{nick}> {text}")
        else:
            if nick.lower() not in ("nickserv", "chanserv", "memoserv"):
                self.pm_log.append(f"{_nepal_timestamp()} <{nick}> {text}")
