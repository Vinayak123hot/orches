"""Connect to an Azure AI Foundry agent and get one response (new v1 API).

Uses the OpenAI-compatible client from AIProjectClient, creates a conversation
(data-plane), then calls the Responses API against a named agent using that
conversation id so the turn is persisted server-side.

Run:  python agent_chat.py
"""

import os

from dotenv import load_dotenv
from azure.identity import ClientSecretCredential
from azure.ai.projects import AIProjectClient

load_dotenv()

endpoint = os.environ.get("AZURE_FOUNDRY_PROJECT_ENDPOINT")
agent_name = os.environ.get("AGENT_NAME")  # the agent's name from the Foundry portal
prompt = "Hello - please introduce yourself in one sentence."

if not endpoint:
    raise SystemExit("AZURE_FOUNDRY_PROJECT_ENDPOINT not set in .env")
if not agent_name:
    raise SystemExit("AGENT_NAME not set in .env (the agent's name in the Foundry portal)")

# 1) Authenticate + get the OpenAI-compatible client
credential = ClientSecretCredential(
    tenant_id=os.environ["AZURE_SP_TENANT_ID"],
    client_id=os.environ["AZURE_SP_CLIENT_ID"],
    client_secret=os.environ["AZURE_SP_CLIENT_SECRET"],
)
project_client = AIProjectClient(credential=credential, endpoint=endpoint)
client = project_client.get_openai_client()

# 2) Create a conversation (proves data-plane access, keeps turn history)
conv = client.conversations.create()
print("Conversation id:", conv.id)

# 3) Call the agent via the Responses API, bound to this conversation
resp = client.responses.create(
    input=prompt,
    conversation=conv.id,
    extra_body={"agent": {"type": "agent_reference", "name": agent_name}},
)

print("\nAgent reply:\n" + (resp.output_text or "").strip())
