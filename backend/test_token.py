import os, requests
from dotenv import load_dotenv

load_dotenv()
token = os.getenv("SLACK_BOT_TOKEN")

r = requests.post(
    "https://slack.com/api/auth.test",
    headers={"Authorization": f"Bearer {token}"}
)
print(r.json())
