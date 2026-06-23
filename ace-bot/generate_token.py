"""
generate_token.py — Run this ONCE on your Mac to create a new Google OAuth token
with full calendar write access. Follow the steps in the printed instructions.

Usage:
  1. Set GOOGLE_CREDENTIALS_JSON environment variable (copy from Railway)
  2. Run:  python3 generate_token.py
  3. A browser window will open — log in and approve all permissions
  4. Copy the JSON printed to your terminal
  5. Paste it into Railway as GOOGLE_TOKEN_JSON (replacing the old value)
  6. Redeploy Ace on Railway
"""

import json
import os
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("\n❌ Missing package. Run first:\n  pip install google-auth-oauthlib\n")
    sys.exit(1)

# All scopes Ace needs — calendar write is the new one
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar",          # ← NEW: full calendar read+write
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/drive",
]


def main():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        print("\n❌ GOOGLE_CREDENTIALS_JSON not set.")
        print("   Copy it from Railway → ace-bot service → Variables → GOOGLE_CREDENTIALS_JSON")
        print("   Then run:  GOOGLE_CREDENTIALS_JSON='<paste here>' python3 generate_token.py\n")
        sys.exit(1)

    try:
        creds_data = json.loads(creds_json)
    except json.JSONDecodeError as e:
        print(f"\n❌ GOOGLE_CREDENTIALS_JSON is not valid JSON: {e}\n")
        sys.exit(1)

    print("\n🔐 Opening browser for Google authorization...")
    print("   → Log in as bradymcgraw@platinumfortuneimpact.com")
    print("   → Click Allow on ALL permission screens\n")

    flow = InstalledAppFlow.from_client_config(creds_data, SCOPES)
    creds = flow.run_local_server(port=0)

    token_data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes),
    }

    token_json = json.dumps(token_data, indent=2)

    print("\n✅ Authorization successful!\n")
    print("=" * 60)
    print("COPY THIS ENTIRE JSON (including the { and }):")
    print("=" * 60)
    print(token_json)
    print("=" * 60)
    print("\nNEXT STEPS:")
    print("  1. Go to Railway → calm-unity project → ace-bot service")
    print("  2. Click Variables")
    print("  3. Find GOOGLE_TOKEN_JSON → click Edit")
    print("  4. Delete the old value, paste the JSON above")
    print("  5. Click Save → Railway will redeploy automatically")
    print("  6. Test Ace: 'Add a reminder to my calendar for tomorrow at 10am'\n")


if __name__ == "__main__":
    main()
