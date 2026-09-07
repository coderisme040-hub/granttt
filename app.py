import os
import threading
from flask import Flask
from bot import IgrisBot

app = Flask(__name__)

@app.route('/')
def home():
    return "Igris IRC Bot is running 24/7! 🚀"

def run_irc():
    IRC_SERVER = "irc.hybridirc.com"
    IRC_PORT = 6697
    IRC_CHANNELS = ["#chatwithworld", "#chatindian", "#games", "#cwwhelp"]
    IRC_NICK = "igris"
    IRC_REALNAME = "igris"
    IRC_PASSWORD = "PAheyhey123"
    
    while True:
        try:
            bot = IgrisBot(IRC_CHANNELS, IRC_NICK, IRC_REALNAME, IRC_PASSWORD, IRC_SERVER, IRC_PORT)
            bot.start()
        except Exception as e:
            print(f"[!] IRC Bot crashed with error: {e}. Restarting in 15 seconds...")
            import time
            time.sleep(15)

if __name__ == "__main__":
    # Start IRC bot in a separate background thread
    irc_thread = threading.Thread(target=run_irc)
    irc_thread.daemon = True
    irc_thread.start()
    
    # Run Flask app for Render / UptimeRobot 24/7 uptime
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
