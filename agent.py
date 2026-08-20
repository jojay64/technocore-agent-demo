import time
import requests
from urllib.parse import quote

ROOM = "jonathan-flop-test"
NICK = "python-agent"
since = 18

print("Agent started. Waiting for messages from jonathan...")

while True:
    url = f"https://technocore.chat/r/{ROOM}?since={since}"
    response = requests.get(url)
    text = response.text

    for line in text.splitlines():
        if not line.startswith("["):
            continue

        # récupère le numéro du message
        number = line.split("]")[0].replace("[", "")
        since = int(number)

        # répond UNIQUEMENT aux messages de jonathan
        if "<~jonathan>" not in line:
            continue

        print("Message received:", line)

        reply = "message received by python agent"

        send_url = (
            f"https://technocore.chat/r/{ROOM}/say/"
            f"{NICK}/{quote(reply)}"
        )

        requests.get(send_url)

        print("Reply sent.")

    time.sleep(5)