import time
import requests
from urllib.parse import quote

ROOM = "jonathan-flop-test"
NICK = "python-agent"
since = 7

print("Agent started. Waiting for new messages...")

while True:
    read_url = f"https://technocore.chat/r/{ROOM}?since={since}"
    response = requests.get(read_url)
    text = response.text

    if "(no new messages)" not in text:
        print(text)

        # récupère le dernier numéro de message
        lines = text.splitlines()

        for line in lines:
            if line.startswith("["):
                number = line.split("]")[0].replace("[", "")
                since = int(number)

        reply = "message received by python agent"

        send_url = (
            f"https://technocore.chat/r/{ROOM}/say/"
            f"{NICK}/{quote(reply)}"
        )

        requests.get(send_url)

    time.sleep(5)
