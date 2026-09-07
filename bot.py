import ssl
import time
import irc.bot
import irc.connection

class IgrisBot(irc.bot.SingleServerIRCBot):
    def __init__(self, channels, nickname, realname, password, server, port=6697):
        # Configure SSL factory for secure port 6697 with relaxed cert checks (ideal for IRC servers)
        ssl_factory = irc.connection.Factory(
            wrapper=lambda sock: ssl.wrap_socket(
                sock,
                cert_reqs=ssl.CERT_NONE
            )
        )
        
        # Initialize with server spec, password, and the SSL connection factory
        super().__init__([(server, port, password)], nickname, realname, connect_factory=ssl_factory)
        self.target_channels = channels
        self.nickname = nickname

    def on_welcome(self, c, e):
        print(f"[+] Connected successfully! Joining channels...")
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
        """Logs every incoming and outgoing message between bot and IRC server"""
        print(f"--> RECV/SENT [{e.type}]: {e.arguments} (target: {e.target}, source: {e.source})")

    def on_privmsg(self, c, e):
        nick = e.source.nick
        msg = e.arguments[0].strip()
        
        print(f"--> PM from {nick}: {msg}")
        
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
                c.privmsg(nick, f"Command executed: invited {arg} to #chatwithworld")
            elif cmd == "!ban":
                c.privmsg("ChanServ", f"ban #ChatWithWorld {arg}")
                c.privmsg(nick, f"Command executed: banned {arg}")
            elif cmd == "!kick":
                c.privmsg("#chatwithworld", f"KICK #chatwithworld {arg} :Requested by {nick}")
                c.privmsg(nick, f"Command executed: kicked {arg}")
