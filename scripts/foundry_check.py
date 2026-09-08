"""Quick check that the Azure AI Foundry connection works.

Reads AZURE_SP_* + AZURE_FOUNDRY_PROJECT_ENDPOINT from .env, authenticates the
same way the app does, then creates a test conversation (proves data-plane
access) and makes a tiny model call.

Run:  python foundry_check.py
"""

import os

from dotenv import load_dotenv
from azure.identity import ClientSecretCredential
from azure.ai.projects import AIProjectClient

load_dotenv()

endpoint = os.environ.get("AZURE_FOUNDRY_PROJECT_ENDPOINT")
print("Endpoint:", endpoint)
if not endpoint:
    raise SystemExit("AZURE_FOUNDRY_PROJECT_ENDPOINT not set in .env")

print("1) Building credential + project client...")
try:
    credential = ClientSecretCredential(
        tenant_id=os.environ["AZURE_SP_TENANT_ID"],
        client_id=os.environ["AZURE_SP_CLIENT_ID"],
        client_secret=os.environ["AZURE_SP_CLIENT_SECRET"],
    )
    project_client = AIProjectClient(credential=credential, endpoint=endpoint)
    client = project_client.get_openai_client()
except KeyError as e:
    raise SystemExit(f"Missing env var: {e}")
except Exception as e:
    raise SystemExit(f"Client setup FAILED: {type(e).__name__}: {e}")

print("2) Creating a test conversation (checks data-plane access)...")
try:
    conv = client.conversations.create()
    print("   OK - conversation id:", conv.id)
except Exception as e:
    raise SystemExit(f"Conversation create FAILED: {type(e).__name__}: {e}")

model = os.getenv("SUMMARY_MODEL", "gpt-4.1-mini")
print(f"3) Testing model '{model}' with a tiny prompt...")
try:
    resp = client.responses.create(model=model, input="Reply with the single word: OK")
    print("   Model replied:", (resp.output_text or "").strip())
except Exception as e:
    print(f"   Model call FAILED (conversation still worked): {type(e).__name__}: {e}")
    print("   -> check that SUMMARY_MODEL matches a real deployment name in this Foundry project")

print("\nFoundry check complete.")
