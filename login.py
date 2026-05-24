"""
One-time login script. Run this ONCE on the VPS:
    python login.py

A browser window opens. Log into your Surfshark account manually
(email + password, solve any captcha, complete 2FA if any).
Once you land on the dashboard, come back to the terminal and press Enter.
Your session is saved to storage_state.json so the bot can reuse it
without logging in every time.

Re-run this script if the bot ever reports the session has expired.
"""
import asyncio
from playwright.async_api import async_playwright

STORAGE = "storage_state.json"
LOGIN_URL = "https://my.surfshark.com/account/log-in"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(LOGIN_URL)
        print("\n>>> Log into Surfshark in the browser window.")
        print(">>> When you see your account dashboard, return here and press Enter.\n")
        input("Press Enter once logged in... ")
        await context.storage_state(path=STORAGE)
        print(f"Saved session to {STORAGE}")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
