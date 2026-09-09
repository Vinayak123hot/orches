"""Compare the two ways the Foundry client gets built, in one process.

Authentication can succeed on its own yet still fail through a client, because the
client decides which scope is requested and which transport options are applied.
This builds the client both ways against the same endpoint and the same credential,
records every scope that gets requested, and reports which construction works.

    python scripts/foundry_probe.py

Reads AZURE_FOUNDRY_PROJECT_ENDPOINT from the environment or from a .env at the
repository root. Prints no secrets and no token values.
"""
import os
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

ENDPOINT = os.environ.get("AZURE_FOUNDRY_PROJECT_ENDPOINT", "").strip()
if not ENDPOINT:
    sys.exit("AZURE_FOUNDRY_PROJECT_ENDPOINT is not set (environment or repo-root .env).")

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

REQUESTED_SCOPES = []


class ScopeRecordingCredential:
    """Wraps a credential and records every scope asked of it."""

    def __init__(self, inner):
        self._inner = inner

    def get_token(self, *scopes, **kwargs):
        REQUESTED_SCOPES.append((scopes, sorted(kwargs)))
        print(f"      get_token(scopes={scopes}, kwargs={sorted(kwargs)})")
        return self._inner.get_token(*scopes, **kwargs)

    def get_token_info(self, *scopes, **kwargs):
        REQUESTED_SCOPES.append((scopes, sorted(kwargs)))
        print(f"      get_token_info(scopes={scopes}, kwargs={sorted(kwargs)})")
        return self._inner.get_token_info(*scopes, **kwargs)

    def close(self):
        try:
            self._inner.close()
        except Exception:
            pass


def attempt(label, build_client, build_openai):
    """Build a client, create a conversation, and report what happened."""
    print()
    print("=" * 74)
    print(label)
    print("=" * 74)
    REQUESTED_SCOPES.clear()
    credential = ScopeRecordingCredential(DefaultAzureCredential())
    try:
        print("  building AIProjectClient ...")
        project_client = build_client(credential)
        print("  calling get_openai_client() ...")
        openai_client = build_openai(project_client)
        print("  calling conversations.create() ...")
        conversation = openai_client.conversations.create()
        print(f"  SUCCESS - conversation id: {getattr(conversation, 'id', '?')}")
        return True
    except Exception as exc:
        print(f"  FAILED - {type(exc).__name__}")
        first = str(exc).strip().splitlines()
        for line in first[:4]:
            print(f"    {line[:110]}")
        if os.environ.get("PROBE_TRACEBACK"):
            traceback.print_exc()
        return False
    finally:
        credential.close()


print(f"endpoint : {ENDPOINT}")
import importlib.metadata as md
for pkg in ("azure-ai-projects", "openai", "azure-identity"):
    try:
        print(f"{pkg:<18} {md.version(pkg)}")
    except Exception:
        print(f"{pkg:<18} NOT INSTALLED")

a = attempt(
    "A. allow_preview=True, no transport timeouts, get_openai_client()",
    lambda cred: AIProjectClient(endpoint=ENDPOINT, credential=cred, allow_preview=True),
    lambda pc: pc.get_openai_client(),
)

b = attempt(
    "B. no allow_preview, connection/read timeouts, get_openai_client(timeout, max_retries)",
    lambda cred: AIProjectClient(credential=cred, endpoint=ENDPOINT,
                                 connection_timeout=20, read_timeout=20),
    lambda pc: pc.get_openai_client(timeout=20, max_retries=1),
)

c = attempt(
    "C. allow_preview=True PLUS the timeouts (isolates which difference matters)",
    lambda cred: AIProjectClient(endpoint=ENDPOINT, credential=cred, allow_preview=True,
                                 connection_timeout=20, read_timeout=20),
    lambda pc: pc.get_openai_client(timeout=20, max_retries=1),
)

print()
print("=" * 74)
print("RESULT")
print("=" * 74)
print(f"  A (V2 style)              : {'works' if a else 'fails'}")
print(f"  B (orches-main style)     : {'works' if b else 'fails'}")
print(f"  C (preview + timeouts)    : {'works' if c else 'fails'}")
print()
if a and not b:
    print("  -> the difference is in construction B.")
    print("     If C works, allow_preview=True is what B is missing.")
    print("     If C also fails, the transport timeouts are the cause.")
elif a and b:
    print("  -> both work here; the earlier failure was environmental, not the code.")
else:
    print("  -> neither works; the problem is upstream of the construction.")
