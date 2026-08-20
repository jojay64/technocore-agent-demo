import requests

ROOM = "jonathan-flop-test"
NICK = "agent-b"
SINCE = 3

url = f"https://technocore.chat/r/{ROOM}?since={SINCE}"

response = requests.get(url)

print(response.text)
