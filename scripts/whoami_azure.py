"""Report which Azure credential is actually available in THIS shell.

Run it in the terminal where V2 works, then in the terminal where orches-main fails,
and compare the two outputs. Nothing is written and no secret is printed.

    python whoami_azure.py
"""
import os
import shutil
import subprocess
import sys

print("=" * 74)
print("PYTHON / PACKAGES")
print("=" * 74)
print(f"  executable : {sys.executable}")
print(f"  version    : {sys.version.split()[0]}")
for pkg in ("azure-identity", "azure-ai-projects", "openai", "azure-identity-broker"):
    try:
        import importlib.metadata as md
        print(f"  {pkg:<22} {md.version(pkg)}")
    except Exception:
        print(f"  {pkg:<22} NOT INSTALLED")

print()
print("=" * 74)
print("AZURE CLI")
print("=" * 74)
az = shutil.which("az") or shutil.which("az.cmd")
print(f"  az on PATH : {az or 'NOT FOUND  <-- AzureCliCredential cannot work'}")
if az:
    try:
        out = subprocess.run([az, "account", "show", "--query", "user.name", "-o", "tsv"],
                             capture_output=True, text=True, timeout=30)
        signed_in = out.stdout.strip()
        print(f"  signed in  : {signed_in or 'NO ACTIVE LOGIN  <-- run: az login'}")
        if out.returncode and out.stderr.strip():
            print(f"  az said    : {out.stderr.strip().splitlines()[0][:90]}")
    except Exception as exc:
        print(f"  az failed  : {type(exc).__name__}: {exc}")

print()
print("=" * 74)
print("SERVICE-PRINCIPAL ENVIRONMENT VARIABLES  (presence only, never values)")
print("=" * 74)
for name in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET",
             "AZURE_SP_TENANT_ID", "AZURE_SP_CLIENT_ID", "AZURE_SP_CLIENT_SECRET"):
    raw = os.environ.get(name)
    if raw is None:
        state = "not set"
    elif not raw.strip():
        state = "SET BUT EMPTY  <-- this breaks EnvironmentCredential"
    else:
        state = f"set ({len(raw)} chars)"
    print(f"  {name:<26} {state}")

print()
print("=" * 74)
print("WHICH CREDENTIAL ACTUALLY WORKS")
print("=" * 74)
try:
    import logging
    logging.disable(logging.WARNING)          # keep the chain's own noise out of the way
    from azure.identity import DefaultAzureCredential
    cred = DefaultAzureCredential()
    token = cred.get_token("https://ai.azure.com/.default")
    print("  SUCCESS - a token was issued")
    print(f"  expires on : {token.expires_on}")
except Exception as exc:
    first = str(exc).splitlines()[0]
    print(f"  FAILED - {type(exc).__name__}")
    print(f"  {first[:100]}")
    print()
    print("  Fix with EITHER:")
    print("    a) install the Azure CLI, then: az login")
    print("    b) set AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET in the root .env")
