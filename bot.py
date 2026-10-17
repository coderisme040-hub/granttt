import os
import re
import ssl
import time
import threading
from collections import deque

import irc.bot
import irc.connection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    ADMIN_COMMANDS = {"join", "part", "say", "raw", "switchme", "switch", "logpub", "logpm"}

    def __init__(self, channels, nickname, realname, password, server, port=6697):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        ssl_factory = irc.connection.Factory(
            wrapper=lambda sock: context.wrap_socket(sock)
        )

        super().__init__([(server, port, password)], nickname, realname, connect_factory=ssl_factory)
        self.target_channels = channels
        self.nickname = nickname

        # --- admin / persona settings (can be overridden with env vars) ---
        self.admin_nick = os.environ.get("IRC_ADMIN_NICK", "Starboy")
        # Optional but strongly recommended, e.g. "*!*@your.host.here"
        self.admin_hostmask = os.environ.get("IRC_ADMIN_HOSTMASK", "")
        self.alt_nick = os.environ.get("IRC_ALT_NICK", "Antonio")
        self.alt_pass = os.environ.get("IRC_ALT_PASS", "")
        self.base_nick = nickname
        self.base_password = password

        self.current_persona = nickname
        self.mentions_log = deque(maxlen=200)
        self.pm_log = deque(maxlen=200)
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

    def _send_lines_slowly(self, target, header, lines, delay=0.8):
        """Send many lines from a worker thread so we don't flood the server."""
        def worker():
            self._say(target, header)
            for line in lines:
                time.sleep(delay)
                self._say(target, line)

        threading.Thread(target=worker, daemon=True).start()

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
            threading.Thread(target=self._exec_switchme, daemon=True).start()
            self._say(reply_to, f"Switching to {self.alt_nick} persona...")

        elif cmd == "switch":
            threading.Thread(target=self._exec_switch, daemon=True).start()
            self._say(reply_to, f"Switching back to {self.base_nick}...")

        # Logs are always sent to the admin by PM, never into a public channel.
        elif cmd == "logpub":
            if not self.mentions_log:
                self._say(admin_nick, "No public mentions recorded.")
            else:
                entries = list(self.mentions_log)[-20:]
                self._send_lines_slowly(
                    admin_nick,
                    f"--- Public Mentions (last {len(entries)} of {len(self.mentions_log)}) ---",
                    entries,
                )

        elif cmd == "logpm":
            if not self.pm_log:
                self._say(admin_nick, "No private messages recorded.")
            else:
                entries = list(self.pm_log)[-20:]
                self._send_lines_slowly(
                    admin_nick,
                    f"--- Private Messages (last {len(entries)} of {len(self.pm_log)}) ---",
                    entries,
                )

    # -- persona switching --------------------------------------------------

    def _exec_switchme(self):
        c = self.connection
        try:
            c.nick(self.alt_nick)
            time.sleep(2)
            if self.alt_pass:
                c.privmsg("NickServ", f"IDENTIFY {self.alt_pass}")
                time.sleep(2)
            else:
                print("[!] IRC_ALT_PASS not set - skipping NickServ identify for persona.")
            c.send_raw("AWAY :Persona active")
            self.current_persona = self.alt_nick
            self.mentions_log.clear()
            self.pm_log.clear()
        except Exception as ex:
            print(f"[!] switchme failed: {ex}")

    def _exec_switch(self):
        c = self.connection
        try:
            c.nick(self.base_nick)
            time.sleep(2)
            if self.base_password:
                c.privmsg("NickServ", f"IDENTIFY {self.base_password}")
            c.send_raw("AWAY")
            self.current_persona = self.base_nick
        except Exception as ex:
            print(f"[!] switch failed: {ex}")

    # -- persona logging ----------------------------------------------------

    def _log_for_persona(self, nick, target, text, is_channel):
        if self.current_persona.lower() != self.alt_nick.lower():
            return
        if is_channel:
            if self.alt_nick.lower() in text.lower():
                self.mentions_log.append(f"[{target}] <{nick}> {text}")
        else:
            if nick.lower() not in ("nickserv", "chanserv", "memoserv"):
                self.pm_log.append(f"<{nick}> {text}")
