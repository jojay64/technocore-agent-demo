import requests
from urllib.parse import quote

ROOM = "jonathan-flop-test"
NICK = "python-agent"

message = "hello from my python agent"

url = f"https://technocore.chat/r/{ROOM}/say/{NICK}/{quote(message)}"

response = requests.get(url)

print(response.text)
