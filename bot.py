import os
import socket
import threading
import time
from flask import Flask

app = Flask(__name__)

# IRC Configuration
SERVER = "irc.hybridirc.com"  # Replace with your actual IRC network server
PORT = 6697
NICK = "igris"
REALNAME = "igris"
PASSWORD = "PAheyhey123"
CHANNELS = ["#chatwithworld", "#chatindian", "#games", "#cwwhelp"]

def send_msg(sock, msg):
    sock.send(f"{msg}\r\n".encode("utf-8"))

def irc_bot():
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((SERVER, PORT))
            
            # Authentication and registration
            send_msg(sock, f"PASS {PASSWORD}")
            send_msg(sock, f"NICK {NICK}")
            send_msg(sock, f"USER {NICK} 0 * :{REALNAME}")
            
            buffer = ""
            while True:
                buffer += sock.recv(2048).decode("utf-8", errors="ignore")
                lines = buffer.split("\r\n")
                buffer = lines.pop()
                
                for line in lines:
                    parts = line.split()
                    if not parts:
                        continue
                        
                    # Handle Ping-Pong to stay connected
                    if parts[0] == "PING":
                        send_msg(sock, f"PONG {parts[1]}")
                        continue
                        
                    # Join channels after successful connection
                    if len(parts) > 1 and parts[1] == "001":
                        for channel in CHANNELS:
                            send_msg(sock, f"JOIN {channel}")
                    
                    # Parse PRIVMSG for commands
                    if "PRIVMSG" in line:
                        try:
                            prefix = parts[0]
                            nick = prefix.split("!")[0][1:]
                            target = parts[2]
                            message = " ".join(parts[3:]).lstrip(":")
                            
                            # CRITICAL CHECK: Only process commands if target is the bot itself (PM)
                            if target.lower() == NICK.lower():
                                if message.startswith("!"):
                                    cmd_parts = message.split()
                                    cmd = cmd_parts[0].lower()
                                    arg = cmd_parts[1] if len(cmd_parts) > 1 else nick
                                    
                                    if cmd == "!voice":
                                        send_msg(sock, f"PRIVMSG ChanServ :voice #ChatWithWorld {arg}")
                                        send_msg(sock, f"PRIVMSG {nick} :Command executed: voiced {arg}")
                                    elif cmd == "!op":
                                        send_msg(sock, f"PRIVMSG ChanServ :op #ChatWithWorld {arg}")
                                        send_msg(sock, f"PRIVMSG {nick} :Command executed: opped {arg}")
                                    elif cmd == "!deop":
                                        send_msg(sock, f"PRIVMSG ChanServ :deop #ChatWithWorld {arg}")
                                        send_msg(sock, f"PRIVMSG {nick} :Command executed: deopped {arg}")
                                    elif cmd == "!invite":
                                        send_msg(sock, f"INVITE {arg} #chatwithworld")
                                        send_msg(sock, f"PRIVMSG {nick} :Command executed: invited {arg} to #chatwithworld")
                                    elif cmd == "!ban":
                                        send_msg(sock, f"PRIVMSG ChanServ :ban #ChatWithWorld {arg}")
                                        send_msg(sock, f"PRIVMSG {nick} :Command executed: banned {arg}")
                                    elif cmd == "!kick":
                                        send_msg(sock, f"KICK #chatwithworld {arg} :Requested by {nick}")
                                        send_msg(sock, f"PRIVMSG {nick} :Command executed: kicked {arg}")
                        except Exception as e:
                            print(f"Error handling command: {e}")
                            
        except Exception as e:
            print(f"Connection lost: {e}, reconnecting in 10 seconds...")
            time.sleep(10)

@app.route("/")
def home():
    return "Igris IRC Bot is running 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    bot_thread = threading.Thread(target=irc_bot, daemon=True)
    bot_thread.start()
    run_flask()
