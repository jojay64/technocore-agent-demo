import time
import requests
from urllib.parse import quote
from openai import OpenAI

ROOM = "flop-agent-lab"
NICK = "critic-agent"
since = 54
MAX_REPLIES = 3
replies_sent = 0

client = OpenAI()

print("Critic agent started...")

while True:
    url = f"https://technocore.chat/r/{ROOM}?since={since}&wait=10"
    response = requests.get(url)
    text = response.text

    for line in text.splitlines():
        if not line.startswith("["):
            continue

        number = line.split("]")[0].replace("[", "")
        since = int(number)

        # Critic responds only to research-agent
        if "<~research-agent>" not in line:
            continue

        if replies_sent >= MAX_REPLIES:
            print("Critic agent reached reply limit.")
            exit()

        message = line.split("> ", 1)[-1]

        ai_response = client.responses.create(
            model="gpt-5.4-nano",
            instructions=(
                "You are critic-agent in a two-agent Technocore experiment. "
                "Read research-agent's message and reply in maximum 3 short bullet points. "
                "Focus only on what is missing, unclear, or could be improved. "
                "Do not invent facts. Do not write long explanations."
            ),
            input=message,
        )

        reply = ai_response.output_text.strip().replace("\n", " ")

        send_url = (
            f"https://technocore.chat/r/{ROOM}/say/"
            f"{NICK}/{quote(reply)}"
        )

        send_response = requests.get(send_url)

        print("Technocore status:", send_response.status_code)
        print(send_response.text)

        if send_response.status_code == 200:
            replies_sent += 1
            print("Critic agent replied:", reply)
        else:
            print("Critic agent message was NOT sent.")

    time.sleep(1)