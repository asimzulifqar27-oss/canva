import asyncio
from playwright.async_api import async_playwright
import requests
import random
import string
import datetime
import os
import subprocess

DUOLINGO_CDP_PORT = int(os.getenv("DUOLINGO_CDP_PORT", "9223"))
DUOLINGO_CDP_URL = os.getenv("DUOLINGO_CDP_URL", f"http://localhost:{DUOLINGO_CDP_PORT}")

def get_temp_email():
    """Generates a mathematically unique email using the catch-all domain."""
    unique_prefix = generate_random_string(12)
    return f"{unique_prefix}@asim.dev"

def generate_random_string(length=8):
    letters = string.ascii_lowercase
    return ''.join(random.choice(letters) for _ in range(length))

def get_credit_card():
    """Reads the first credit card from creditcards.txt."""
    try:
        with open('creditcards.txt', 'r') as f:
            lines = f.readlines()
            if lines:
                return lines[0].strip() # Assuming format: card_number|mm|yy|cvv
    except FileNotFoundError:
        print("creditcards.txt not found. Please create it and add cards in format: number|mm|yy|cvv")
    return None

def remove_credit_card():
    """Removes the first credit card from creditcards.txt after it's successfully used."""
    try:
        with open('creditcards.txt', 'r') as f:
            lines = f.readlines()
        
        if lines:
            with open('creditcards.txt', 'w') as f:
                f.writelines(lines[1:])
            print("Used credit card removed from list.")
    except Exception as e:
        print(f"Failed to remove credit card: {e}")

def save_account_details(email, password):
    """Saves generated account info."""
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    with open('accounts.txt', 'a') as f:
        f.write(f"Email: {email} | Password: {password} | Subscription Date: {date_str}\n")
    print("Account saved to accounts.txt")

async def run():
    try:
        num_accounts_str = input("\nHow many accounts do you want to create? ")
        num_accounts = int(num_accounts_str)
        if num_accounts < 1:
            print("Number must be at least 1.")
            return
    except ValueError:
        print("Invalid number. Exiting.")
        return

    # Using native Playwright! No stealth dependencies required.
    async with async_playwright() as p:
        try:
            print("Connecting to live Chrome browser...")
            browser = await p.chromium.connect_over_cdp(DUOLINGO_CDP_URL)
            context = browser.contexts[0]
            page = await context.new_page()
        except Exception as e:
            print("======================================================")
            print("ERROR: Could not connect to your running Chrome browser.")
            print(f"Please run start_duolingo.bat so it can start Chrome on port {DUOLINGO_CDP_PORT}.")
            print("======================================================")
            return

        for act_num in range(num_accounts):
            print(f"\n==========================================")
            print(f"STARTING ACCOUNT CREATION {act_num + 1} OF {num_accounts}")
            print(f"==========================================")

            # Instantly destroy browser cookies. Duolingo goes back to logged out!
            await context.clear_cookies()
            
            email = get_temp_email()
            if not email:
                print("Failed to get temp email. Skipping to next attempt.")
                continue

            password = generate_random_string(12) + "A1!"
            name = generate_random_string(6).capitalize()
            age = str(random.randint(18, 40))
            card_info = get_credit_card()
            
            if not card_info:
                print("No credit cards left in creditcards.txt! Stopping early.")
                break

            print(f"Generated Info -> Name: {name}, Age: {age}, Email: {email}, Password: {password}")

            try:
                print("Navigating to Duolingo Super page...")
                await page.goto("https://www.duolingo.com/super", timeout=60000)

                print("Clicking Start My Free Week...")
                await page.get_by_role("button", name="Start my").first.click()
                
                print("Entering age...")
                await page.wait_for_selector('input[data-test="age-input"]', timeout=10000)
                await page.fill('input[data-test="age-input"]', age)
                await page.locator('button[data-test="continue-button"]').first.click()

                print("Entering name...")
                await page.wait_for_selector('input[data-test="full-name-input"]', timeout=10000)
                await page.fill('input[data-test="full-name-input"]', name)
                
                print("Entering email...")
                await page.locator('input[data-test="email-input"]').press_sequentially(email, delay=50)
                await page.wait_for_timeout(500)
                
                print("Entering password...")
                await page.locator('input[data-test="password-input"]').press_sequentially(password, delay=50)
                await page.wait_for_timeout(1000)
                
                print("Submitting the form...")
                await page.locator('input[data-test="password-input"]').press("Enter")
                await page.wait_for_timeout(1000)
                await page.locator('button[data-test="register-button"]').first.click(force=True)

                print("Waiting for registration to process...")

                print("Selecting Family Plan...")
                await page.wait_for_timeout(5000)
                
                await page.get_by_text("Family Plan", exact=True).first.click()
                await page.wait_for_timeout(1000)
                
                print("Confirming Plan...")
                await page.get_by_role("button", name="START MY FREE 7 DAYS").first.click()

                print("Entering payment details...")
                await page.wait_for_timeout(5000)
                
                card_parts = card_info.split('|')
                if len(card_parts) != 4:
                    print("Invalid credit card format. Skipping account.")
                    continue
                card_number, mm, yy, cvv = card_parts
                yy_short = yy[-2:] # Convert 2031 to 31 for Stripe
                
                print("Filling Stripe payment iframe...")
                await page.wait_for_selector('iframe[title="Secure payment input frame"]', timeout=20000)
                payment_frame = page.frame_locator('iframe[title="Secure payment input frame"]')
                
                await payment_frame.locator('input[name="number"]').press_sequentially(card_number, delay=50)
                await payment_frame.locator('input[name="expiry"]').press_sequentially(f"{mm}{yy_short}", delay=50)
                await payment_frame.locator('input[name="cvc"]').press_sequentially(cvv, delay=50)
                
                await page.wait_for_timeout(1000)
                print("Clicking Start my free 7 days...")
                await page.locator('button[data-test="cc-submit-button"]').first.click()
                
                print("Payment submitted. Waiting for confirmation...")
                await page.wait_for_timeout(10000) # Wait for processing
                
                # Check for error messages indicating a declined card
                try:
                    page_text = await page.locator("body").inner_text()
                except:
                    page_text = ""
                    
                try:
                    iframe_text = await payment_frame.locator("body").inner_text()
                except:
                    iframe_text = ""
                    
                combined_text = (page_text + " " + iframe_text).lower()
                
                if "declined" in combined_text or "unsuccessful" in combined_text or "card was" in combined_text or "try a different" in combined_text or "invalid" in combined_text:
                    print(f"Payment failed for card ending in {card_number[-4:]}: Decline or error message detected.")
                    print("Account will not be saved and card will remain in list.")
                else:
                    print("Payment successful! No decline message detected.")
                    save_account_details(email, password)
                    remove_credit_card()

            except Exception as e:
                print(f"Automation encountered an error on account {act_num + 1}: {e}")
                error_file = f"error_acc{act_num+1}.png"
                try:
                    await page.screenshot(path=error_file, full_page=True)
                except: pass
                print(f"Saved screenshot to '{error_file}'.")

        print("\nAll requested account generation cycles have finished!")
        print("Closing browser connection...")
        try:
            await browser.close()
        except: pass

if __name__ == '__main__':
    asyncio.run(run())
