import time
import requests
from urllib.parse import quote
from openai import OpenAI

ROOM = "flop-agent-lab"
NICK = "research-agent"
since = 54
MAX_REPLIES = 3
replies_sent = 0

client = OpenAI()

print("Research agent started...")

while True:
    url = f"https://technocore.chat/r/{ROOM}?since={since}&wait=10"
    response = requests.get(url)
    text = response.text

    for line in text.splitlines():
        if not line.startswith("["):
            continue

        number = line.split("]")[0].replace("[", "")
        since = int(number)

        # Research responds only to critic-agent
        if "<~critic-agent>" not in line:
            continue

        if replies_sent >= MAX_REPLIES:
            print("Research agent reached reply limit.")
            exit()

        message = line.split("> ", 1)[-1]

        ai_response = client.responses.create(
            model="gpt-5.4-nano",
            instructions=(
                "You are research-agent in a two-agent Technocore experiment. "
                "Improve your analysis using critic-agent's feedback. "
                "Reply in maximum 4 short bullet points. "
                "Be concrete and concise. "
                "Do not invent facts."
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
            print("Research agent replied:", reply)
        else:
            print("Research agent message was NOT sent.")

    time.sleep(1)