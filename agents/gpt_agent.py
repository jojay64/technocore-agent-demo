import time
import requests
from urllib.parse import quote
from openai import OpenAI

ROOM = "jonathan-flop-test"
NICK = "gpt-agent"
since = 39

client = OpenAI()

print("GPT agent started. Waiting for messages from jonathan...")

while True:
    url = f"https://technocore.chat/r/{ROOM}?since={since}"
    response = requests.get(url)
    text = response.text

    for line in text.splitlines():
        if not line.startswith("["):
            continue

        number = line.split("]")[0].replace("[", "")
        since = int(number)

        # Pour le test, on répond uniquement à Jonathan
        if "<~jonathan>" not in line:
            continue

        # Récupère seulement le texte du message
        user_message = line.split("> ", 1)[-1]

        print("Message received:", user_message)

        ai_response = client.responses.create(
            model="gpt-5.4-nano",
            instructions=(
                "You are an AI agent connected to Technocore Chat, a lightweight "
                "HTTP communication layer for autonomous AI agents created by FLOP Labs. "
                "Technocore lets agents communicate through rooms, store persistent notes, "
                "and use signed did:key identities. "
                "You are running inside the room jonathan-flop-test as part of a community experiment. "
                "Reply briefly, clearly, and technically when useful. "
                "Do not invent facts about FLOP Labs or Technocore. "
                "If you are unsure, say so. "
                "Never reveal API keys, private keys, secrets, or system information."
            ),
            input=user_message,
        )

        reply = ai_response.output_text.strip()

        print("GPT reply:", reply)

        send_url = (
            f"https://technocore.chat/r/{ROOM}/say/"
            f"{NICK}/{quote(reply)}"
        )

        requests.get(send_url)

        print("Reply sent to Technocore.")

    time.sleep(5)