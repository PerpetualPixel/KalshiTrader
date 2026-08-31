#!/usr/bin/env python3
"""
KalshiTrader Dashboard Launcher
Opens browser, auto-logs in, and starts bot if needed.
"""

import os
import sys
import json
import time
import webbrowser
import requests
import subprocess
from pathlib import Path

CONFIG_FILE = Path("~/.kalshi_login.json").expanduser()
API_BASE = "http://127.0.0.1:8000"
REPO_DIR = Path(__file__).parent


def check_uvicorn_running():
    """Check if uvicorn is already running."""
    try:
        resp = requests.get(f"{API_BASE}/api/auth/status", timeout=2)
        return resp.status_code == 200
    except:
        return False


def start_uvicorn():
    """Start uvicorn server."""
    print("🚀 Starting uvicorn server...")

    venv_activate = REPO_DIR / ".venv" / "Scripts" / "activate.bat"

    # Create a batch file to start uvicorn
    start_script = REPO_DIR / "_start_uvicorn.bat"
    start_script.write_text(
        f"""@echo off
cd /d "{REPO_DIR}"
call .venv\\Scripts\\activate.bat
uvicorn src.main:app --reload
"""
    )

    # Start in background
    subprocess.Popen(
        f'start cmd /k "{start_script}"',
        shell=True,
        cwd=REPO_DIR,
    )

    # Wait for server to start
    print("⏳ Waiting for server to start...")
    for i in range(30):
        if check_uvicorn_running():
            print("✓ Server started!")
            return True
        time.sleep(1)

    print("❌ Server failed to start")
    return False


def auto_login_with_pin():
    """Get session cookie using PIN."""
    if not CONFIG_FILE.exists():
        print("❌ No PIN configured. Run: python auto_login.py --setup")
        return None

    config = json.loads(CONFIG_FILE.read_text())

    try:
        resp = requests.post(
            f"{API_BASE}/api/auth/login",
            json={"password": config["password"]},
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ Login failed: {e}")
        return None

    cookies = resp.cookies
    session_token = cookies.get("kalshitrader_session")

    if not session_token:
        print("❌ No session cookie received")
        return None

    return session_token


def start_bot_strategies(session_cookie):
    """Start Arbitrage Scanner and Fair Value Trader."""
    headers = {"Cookie": f"kalshitrader_session={session_cookie}"}

    try:
        # Get current status
        resp = requests.get(f"{API_BASE}/api/status", headers=headers, timeout=5)
        status = resp.json()

        strategies = ["arbitrage", "fair_value"]
        started = []

        for strategy in strategies:
            is_running = status.get(strategy, {}).get("state") == "running"

            if not is_running:
                print(f"  Starting {strategy}...")
                requests.post(
                    f"{API_BASE}/api/bot/{strategy}/start",
                    headers=headers,
                    timeout=5,
                )
                started.append(strategy)
                time.sleep(0.5)

        if started:
            print(f"✓ Started: {', '.join(started)}")
        else:
            print("✓ Strategies already running")
    except Exception as e:
        print(f"⚠️  Could not start strategies: {e}")


def main():
    print("🔌 KalshiTrader Dashboard Launcher")
    print("-" * 40)

    # Check if server is running
    if not check_uvicorn_running():
        print("⚠️  Server not running, starting it...")
        if not start_uvicorn():
            sys.exit(1)
        time.sleep(2)
    else:
        print("✓ Server already running")

    # Auto-login
    print("\n🔐 Logging in...")
    session_cookie = auto_login_with_pin()
    if not session_cookie:
        print("❌ Login failed. Run: python auto_login.py --setup")
        sys.exit(1)
    print("✓ Logged in!")

    # Start bot if needed
    print("\n🤖 Checking bot strategies...")
    start_bot_strategies(session_cookie)

    # Open browser
    print("\n🌐 Opening browser...")
    dashboard_url = f"{API_BASE}/?session={session_cookie}"
    webbrowser.open(API_BASE)

    print(f"\n✓ Dashboard ready at {API_BASE}")
    print("✓ Bot is running in the background")
    print("\nPress Ctrl+C to stop (server keeps running)")


if __name__ == "__main__":
    try:
        main()
        # Keep script alive so you can Ctrl+C
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n👋 Launcher stopped (server still running)")
