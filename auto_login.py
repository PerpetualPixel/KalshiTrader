#!/usr/bin/env python3
"""
Auto-login script for KalshiTrader dashboard.
Set your PIN once, then use it to auto-login without entering your password each time.
"""

import os
import sys
import json
import requests
import secrets
from pathlib import Path

CONFIG_FILE = Path("~/.kalshi_login.json").expanduser()
API_BASE = "http://127.0.0.1:8000"


def setup_pin():
    """First-time setup: save your PIN."""
    print("🔐 KalshiTrader Auto-Login Setup")
    print("-" * 40)

    # Get password and PIN
    import getpass
    password = getpass.getpass("Enter your dashboard password: ")
    pin = getpass.getpass("Set a login PIN (4-6 digits): ")

    if not pin.isdigit() or len(pin) < 4:
        print("❌ PIN must be 4-6 digits")
        sys.exit(1)

    # Save to config
    config = {
        "password": password,
        "pin": pin,
    }

    CONFIG_FILE.write_text(json.dumps(config))
    CONFIG_FILE.chmod(0o600)  # Only owner can read

    print(f"✓ PIN saved securely to {CONFIG_FILE}")
    print(f"✓ Next time, run: python auto_login.py {pin}")


def login_with_pin(pin):
    """Login using PIN and return session token."""

    # Read config
    if not CONFIG_FILE.exists():
        print("❌ No PIN configured. Run: python auto_login.py --setup")
        sys.exit(1)

    config = json.loads(CONFIG_FILE.read_text())

    # Verify PIN
    if config["pin"] != pin:
        print("❌ Invalid PIN")
        sys.exit(1)

    # Login
    try:
        resp = requests.post(
            f"{API_BASE}/api/auth/login",
            json={"password": config["password"]},
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ Login failed: {e}")
        print(f"   Is uvicorn running at {API_BASE}?")
        sys.exit(1)

    # Extract session cookie
    cookies = resp.cookies
    session_token = cookies.get("session_token")

    if not session_token:
        print("❌ No session token received")
        print(f"   Cookies received: {dict(cookies)}")
        print(f"   Response status: {resp.status_code}")
        print(f"   Response headers: {dict(resp.headers)}")
        print(f"   Try logging in via browser at http://127.0.0.1:8000")
        sys.exit(1)

    print(f"✓ Logged in!")
    print(f"✓ Session token: {session_token}")
    print(f"\nUse this in scripts:")
    print(f'  headers = {{"Cookie": "session_token={session_token}"}}')

    return session_token


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python auto_login.py --setup        (first time setup)")
        print("  python auto_login.py <PIN>          (login with PIN)")
        sys.exit(1)

    if sys.argv[1] == "--setup":
        setup_pin()
    else:
        login_with_pin(sys.argv[1])


if __name__ == "__main__":
    main()
