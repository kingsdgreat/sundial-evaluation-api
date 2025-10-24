import re
import time
import logging
import random
import math
import statistics
import numpy as np
import asyncio
from typing import Tuple, List, Dict, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
import requests
import traceback
import redis
import json
import hashlib
import os
import subprocess
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from bs4 import BeautifulSoup
from DrissionPage import ChromiumOptions, WebPage
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from DrissionPage.errors import BrowserConnectError, AlertExistsError
from functools import lru_cache
from pydantic_settings import BaseSettings
from pydantic import Field

# Global rate limiting to prevent PropStream blocking
last_request_time = 0
MIN_REQUEST_INTERVAL = 0.3  # Optimized: 0.3 seconds for speed and reliability  


# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Constants
LOGIN_URL = "https://login.propstream.com/"
ADDRESS_FORMAT = "APN# {0}, {1}, {2}"
EMAIL = "kingsdgreatest@gmail.com"
PASSWORD = "Kanayo147*"
MILES_TO_DEGREES = 1.0 / 69
INITIAL_SEARCH_RADIUS_MILES = 3.0  # Start with 3 miles for faster results
MAX_SEARCH_RADIUS_MILES = 10.0  # Reduced to 8 miles for speed while maintaining good coverage
SEARCH_RADIUS_INCREMENT = 1.0  # Smaller increment for faster convergence  
MIN_COMPARABLE_PROPERTIES = 3  # Need at least 3 for good average 
MIN_ACREAGE_RATIO = 0.2
MAX_ACREAGE_RATIO = 5.0
PRICE_THRESHOLD = 100000

class PropertyRequest(BaseModel):
    apn: str
    county: str
    state: str

class ComparableProperty(BaseModel):
    address: str
    price: float
    price_text: str
    acreage: float
    price_per_acre: float
    beds: Optional[str]  
    baths: Optional[str]   
    sqft: Optional[str] 

class ValuationStats(BaseModel):
    min: float
    max: float
    avg: float
    median: float
    std_dev: float

class ValuationResponse(BaseModel):
    target_property: str
    target_acreage: float
    target_latitude: Optional[float] = None  
    target_longitude: Optional[float] = None 
    search_radius_miles: float
    total_comparables_found: int 
    comparable_count: int
    estimated_value_avg: Optional[float]
    estimated_value_median: Optional[float]
    price_per_acre_stats: Optional[ValuationStats]
    comparable_properties: List[ComparableProperty]
    outlier_properties: List[ComparableProperty]
    search_url: Optional[str] = None
    data_source: Optional[str] = None
    processing_time_seconds: Optional[float] = None

class Settings(BaseSettings):
    CACHE_EXPIRATION: int = 24 * 60 * 60  # 24 hours
    MAX_CACHE_SIZE: int = 1000
    API_RETRY_ATTEMPTS: int = 3
    
    regrid_api_token: str = Field(default="")
    zillow_rapid_api_key: str = Field(default="")
    
    model_config = {
        "extra": "allow",
        "env_file": ".env"
    }

settings = Settings()

# Import browser pool
from browser_pool import browser_pool

# Add semaphore to limit concurrent requests
REQUEST_LOCK = asyncio.Lock()  # For sequential processing

# Removed old browser tracking - now using persistent session in browser_pool

async def ensure_logged_in(page: WebPage):
    """Ensure the browser is logged in, with enhanced session validation"""
    # Check if we're already on a logged-in page
    current_url = page.url
    if current_url and "app.propstream.com" in current_url:
        logging.info("🔄 Already logged in - session is valid")
        browser_pool.mark_session_valid()
        return
    
    # Check if session is marked as valid
    if browser_pool.session_valid:
        # Try to navigate to search page to verify session
        try:
            page.get('https://app.propstream.com/search')
            time.sleep(3)
            
            # Check if we got redirected to login page (session expired)
            if "login.propstream.com" in page.url:
                logging.warning("❌ Session expired - redirected to login page")
                browser_pool.invalidate_session()
                # Force immediate re-login instead of retrying
                raise Exception("Session expired - need fresh login")
            elif "app.propstream.com" in page.url:
                logging.info("✅ Persistent session is still valid")
                return
            else:
                logging.warning("❌ Unexpected redirect during session validation")
                browser_pool.invalidate_session()
        except Exception as e:
            logging.warning(f"Error checking persistent session: {e}")
            browser_pool.invalidate_session()
    
    # Need to login
    logging.info("🔑 Performing login...")
    await asyncio.get_event_loop().run_in_executor(
        None, login_to_propstream, page, EMAIL, PASSWORD
    )
    browser_pool.mark_session_valid()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await browser_pool.initialize()
    logging.info("🚀 Browser pool initialized with persistent session support")
    
    yield
    # Shutdown
    await browser_pool.cleanup()

app = FastAPI(
    title="Property Valuation API",
    description="API for real estate property valuation based on comparable properties",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Redis connection - use service name for containerized Redis
# redis_client = redis.Redis(host='redis', port=6379, db=0, decode_responses=True)
# redis_client = redis.Redis(host='127.0.0.1', port=6379, db=0, decode_responses=True)
redis_url = os.getenv('REDIS_URL')
if redis_url:
    redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
else:
    # Default to docker-compose service name when env var is not provided
    redis_client = redis.Redis(host='redis', port=6379, db=0, decode_responses=True)

# Update rate limiter to use Redis for distributed limiting
from rate_limiter import api_rate_limiter
api_rate_limiter.redis_client = redis_client

def generate_cache_key(property_request: PropertyRequest) -> str:
    cache_key_str = f"{property_request.apn}_{property_request.county}_{property_request.state}"
    return hashlib.md5(cache_key_str.encode()).hexdigest()

STATE_ABBREVIATIONS = {
    'Alabama': 'AL', 'Alaska': 'AK', 'Arizona': 'AZ', 'Arkansas': 'AR', 'California': 'CA',
    'Colorado': 'CO', 'Connecticut': 'CT', 'Delaware': 'DE', 'Florida': 'FL', 'Georgia': 'GA',
    'Hawaii': 'HI', 'Idaho': 'ID', 'Illinois': 'IL', 'Indiana': 'IN', 'Iowa': 'IA',
    'Kansas': 'KS', 'Kentucky': 'KY', 'Louisiana': 'LA', 'Maine': 'ME', 'Maryland': 'MD',
    'Massachusetts': 'MA', 'Michigan': 'MI', 'Minnesota': 'MN', 'Mississippi': 'MS', 'Missouri': 'MO',
    'Montana': 'MT', 'Nebraska': 'NE', 'Nevada': 'NV', 'New Hampshire': 'NH', 'New Jersey': 'NJ',
    'New Mexico': 'NM', 'New York': 'NY', 'North Carolina': 'NC', 'North Dakota': 'ND', 'Ohio': 'OH',
    'Oklahoma': 'OK', 'Oregon': 'OR', 'Pennsylvania': 'PA', 'Rhode Island': 'RI', 'South Carolina': 'SC',
    'South Dakota': 'SD', 'Tennessee': 'TN', 'Texas': 'TX', 'Utah': 'UT', 'Vermont': 'VT',
    'Virginia': 'VA', 'Washington': 'WA', 'West Virginia': 'WV', 'Wisconsin': 'WI', 'Wyoming': 'WY'
}

def get_state_abbreviation(state: str) -> str:
    if len(state) == 2 and state.upper() in STATE_ABBREVIATIONS.values():
        return state.upper()
    
    state_name = state.strip().title()
    abbr = STATE_ABBREVIATIONS.get(state_name)
    
    if not abbr:
        logging.info(f"State conversion: Input '{state}' -> Using as-is")
        return state.upper()
    
    logging.info(f"State conversion: Input '{state}' -> Converted to '{abbr}'")
    return abbr

def take_screenshot(page: WebPage, filename: str, description: str = "", property_info: str = "") -> None:
    """Take a screenshot for debugging purposes - DISABLED FOR SPEED"""
    # Screenshots disabled for maximum performance
    return

def login_to_propstream(page: WebPage, email: str, password: str) -> None:
    try:
        page.get(LOGIN_URL)
        logging.info("Accessing login page")
        
        # Wait for document ready
        page.wait.load_start()
        page.wait.doc_loaded(timeout=5)
        time.sleep(0.5)  # Minimal wait for maximum speed
        
        # Take screenshot of login page
        # take_screenshot(page, "01_login_page.png", "Login page loaded")
        
        # Handle cookie popup first (this was blocking the login!)
        logging.info("=== HANDLING COOKIE POPUP ===")
        try:
            # Look for cookie popup and accept/reject it
            cookie_selectors = [
                'xpath://button[contains(text(), "Accept All")]',
                'xpath://button[contains(text(), "Reject All")]',
                'xpath://button[contains(@class, "cookie") and contains(text(), "Accept")]',
                'xpath://*[contains(@class, "cookie")]//button[contains(text(), "Accept")]',
                'xpath://button[text()="Accept All"]',
                'xpath://button[text()="Reject All"]'
            ]
            
            cookie_handled = False
            for selector in cookie_selectors:
                try:
                    cookie_button = page.ele(selector, timeout=3)
                    if cookie_button:
                        cookie_button.click()
                        logging.info(f"✓ Cookie popup handled with selector: {selector}")
                        time.sleep(2)
                        # take_screenshot(page, "01b_after_cookie_popup.png", "After handling cookie popup")
                        cookie_handled = True
                        break
                except Exception:
                    continue
            
            if not cookie_handled:
                logging.info("No cookie popup found or already handled")
        except Exception as e:
            logging.warning(f"Error handling cookie popup: {e}")
        
        # Analyze the login page structure
        logging.info("=== LOGIN PAGE ANALYSIS ===")
        
        # Check what input fields are available
        all_inputs = page.eles('xpath://input')
        logging.info(f"Found {len(all_inputs)} input fields on login page")
        for i, inp in enumerate(all_inputs[:10]):
            try:
                inp_type = inp.attr('type') or 'text'
                inp_name = inp.attr('name') or 'unknown'
                inp_placeholder = inp.attr('placeholder') or 'none'
                logging.info(f"Input {i+1}: type='{inp_type}', name='{inp_name}', placeholder='{inp_placeholder}'")
            except Exception:
                pass
        
        # Check what buttons are available
        all_buttons = page.eles('xpath://button')
        logging.info(f"Found {len(all_buttons)} buttons on login page")
        for i, btn in enumerate(all_buttons[:5]):
            try:
                btn_text = btn.text.strip()
                btn_type = btn.attr('type') or 'button'
                logging.info(f"Button {i+1}: text='{btn_text}', type='{btn_type}'")
            except Exception:
                pass
        
        # Try multiple selectors for username/password
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                # Wait for page to fully load before looking for inputs
                time.sleep(3)
                
                username_el = page.ele("@name=username", timeout=15)
                if not username_el:
                    username_el = page.ele('xpath://input[@type="email" or @name="username" or contains(@placeholder, "Email") or contains(@placeholder, "username")]', timeout=15)
                if not username_el:
                    username_el = page.ele('xpath://input[contains(@class, "email") or contains(@class, "username")]', timeout=15)
                    
                password_el = page.ele("@name=password", timeout=15)
                if not password_el:
                    password_el = page.ele('xpath://input[@type="password" or @name="password" or contains(@placeholder, "Password")]', timeout=15)
                if not password_el:
                    password_el = page.ele('xpath://input[contains(@class, "password")]', timeout=15)
                    
                if not username_el or not password_el:
                    logging.error(f"Login inputs not found - Username: {username_el is not None}, Password: {password_el is not None}")
                    raise Exception("Login inputs not found")
                
                # Clear and enter credentials with verification
                # Use direct value setting to avoid input corruption
                try:
                    # Method 1: Direct attribute setting (most reliable)
                    username_el.set.value(email)
                    # Human-like delay with randomization
                    time.sleep(random.uniform(1.0, 2.5))
                    logging.info("Email set via direct value attribute")
                    
                    # Verify it was set correctly
                    entered_email = username_el.value
                    if entered_email == email:
                        logging.info("✅ Email verification successful")
                    else:
                        logging.warning(f"❌ Direct value setting failed: '{entered_email}' vs expected '{email}'")
                        # Fallback: Clear and input method
                        username_el.clear()
                        time.sleep(0.5)
                        username_el.input(email)
                        time.sleep(0.5)
                        logging.info("🔄 Used fallback input method")
                        
                except Exception as e:
                    logging.warning(f"Error setting email field: {e}")
                    # Final fallback: standard input
                    try:
                        username_el.clear()
                        time.sleep(0.5)
                        username_el.input(email)
                        time.sleep(0.5)
                        logging.info("🔄 Used standard input as final fallback")
                    except Exception as e2:
                        logging.error(f"All email input methods failed: {e2}")
                
                # Final verification of email field
                final_email = username_el.value
                if final_email == email:
                    logging.info(f"✅ Final email verification successful: '{final_email}'")
                else:
                    logging.error(f"❌ Final email verification failed: '{final_email}' vs expected '{email}'")
                
                password_el.clear()
                time.sleep(random.uniform(1.5, 3.0))  # Human-like delay
                password_el.input(password)
                time.sleep(random.uniform(0.5, 1.5))  # Small delay after password
                logging.info("Password entered")
                
                # Wait a moment before clicking login
                time.sleep(random.uniform(2.0, 4.0))  # Human-like delay
                
                # Try multiple login button selectors
                login_button = None
                login_selectors = [
                    'xpath://*[@id="form-content"]//button',
                    'xpath://button[contains(text(), "Log")]',
                    'xpath://button[contains(@class, "login") or contains(., "Sign In")]',
                    'xpath://button[@type="submit"]',
                    'xpath://input[@type="submit"]',
                    'xpath://button[contains(text(), "Login") or contains(text(), "Sign In")]',
                    'xpath://*[contains(@class, "submit")]',
                    'xpath://button[contains(@class, "btn")]',
                    'xpath://button[contains(@class, "gradient-btn")]',
                    'xpath://button'
                ]
                
                for i, selector in enumerate(login_selectors):
                    try:
                        login_button = page.ele(selector, timeout=5)
                        if login_button:
                            logging.info(f"✅ Found login button with selector {i+1}: {selector}")
                            break
                    except Exception as e:
                        logging.info(f"❌ Login button selector {i+1} failed: {e}")
                        continue
                
                if login_button:
                    # Scroll to button to ensure it's visible
                    try:
                        login_button.scroll.to_see()
                        time.sleep(1)
                    except Exception:
                        pass
                    
                    login_button.click()
                    logging.info("Login button clicked")
                else:
                    raise Exception("Login button not found")
                
                # Wait for navigation with longer timeout for login
                page.wait.doc_loaded(timeout=15)
                time.sleep(random.uniform(3.0, 5.0))  # Longer wait for login to complete
                
                # Handle any browser dialogs/alerts that might be blocking
                logging.info("=== HANDLING BROWSER DIALOGS ===")
                dialog_handled = False
                
                # Minimal delay to prevent rate limiting
                time.sleep(0.5)  # Minimal wait for maximum speed
                
                # Handle session conflict popups specifically
                try:
                    # Look for session conflict popup
                    session_popup_selectors = [
                        'xpath://div[contains(text(), "username you are using is currently still logged in")]',
                        'xpath://div[contains(text(), "prior session not being properly logged out")]',
                        'xpath://div[contains(text(), "another user is using the same account credentials")]',
                        'xpath://*[contains(text(), "PROCEED") and contains(text(), "Log Out")]',
                        'xpath://button[contains(text(), "Proceed")]',
                        'xpath://button[contains(text(), "Log Out")]'
                    ]
                    
                    session_popup_found = False
                    for selector in session_popup_selectors:
                        try:
                            popup_element = page.ele(selector, timeout=3)
                            if popup_element:
                                logging.info(f"🔍 Found session conflict popup with selector: {selector}")
                                session_popup_found = True
                                break
                        except Exception:
                            continue
                    
                    if session_popup_found:
                        # Click "Proceed" to continue with current session
                        try:
                            proceed_button = page.ele('xpath://button[contains(text(), "Proceed")]', timeout=5)
                            if proceed_button:
                                proceed_button.click()
                                logging.info("✅ Clicked 'Proceed' on session conflict popup")
                                time.sleep(3)
                                dialog_handled = True
                        except Exception as e:
                            logging.warning(f"Could not click Proceed button: {e}")
                            
                            # Fallback: try to click any button in the popup
                            try:
                                popup_buttons = page.eles('xpath://div[contains(@class, "modal") or contains(@class, "popup") or contains(@class, "dialog")]//button')
                                if popup_buttons:
                                    popup_buttons[0].click()  # Click first button (usually Proceed)
                                    logging.info("✅ Clicked first button in session conflict popup")
                                    time.sleep(3)
                                    dialog_handled = True
                            except Exception as e2:
                                logging.warning(f"Could not click any popup button: {e2}")
                
                except Exception as e:
                    logging.warning(f"Error handling session conflict popup: {e}")
                
                # Try multiple methods to handle other dialogs/alerts
                if not dialog_handled:
                    try:
                        # Method 1: DrissionPage alert handling
                        page.handle_alert(accept=True, timeout=3)
                        logging.info("✅ Handled browser alert with handle_alert")
                        dialog_handled = True
                        time.sleep(5)  # Increased delay
                    except Exception as e:
                        logging.info(f"handle_alert method failed: {e}")
                
                if not dialog_handled:
                    try:
                        # Method 2: Direct alert property access
                        if hasattr(page, 'alert'):
                            page.alert.accept()
                            logging.info("✅ Handled alert with direct alert.accept")
                            dialog_handled = True
                            time.sleep(3)
                    except Exception as e:
                        logging.info(f"Direct alert.accept failed: {e}")
                
                if not dialog_handled:
                    try:
                        # Method 3: Try to dismiss any blocking dialogs
                        page.handle_alert(accept=False, timeout=2)
                        logging.info("✅ Dismissed blocking dialog")
                        time.sleep(2)
                    except Exception as e:
                        logging.info(f"Dialog dismiss failed: {e}")
                
                if not dialog_handled:
                    logging.info("No browser dialogs found or all methods failed")
                
                # Take screenshot after login (generic, no property info needed)
                take_screenshot(page, "02_after_login.png", "After login button clicked")
                
                # Look for proceed button with multiple selectors targeting the exact structure
                proceed_selectors = [
                    # Target the exact structure from user's HTML - most specific first
                    'xpath://button[contains(@class, "src-components-Button-style__cuWaY__button") and contains(@class, "src-components-Button-style__FdLlt__solid")]',
                    'xpath://button[contains(@class, "src-components-Button-style__FdLlt__solid")]//div[contains(@class, "src-components-Button-style__FABy8__content") and text()="Proceed"]/..',
                    'xpath://button[contains(@class, "src-components-Button-style__cuWaY__button")]//div[text()="Proceed"]/..',
                    'xpath://div[contains(@class, "src-app-components-SessionOverlay-style")]//button[contains(@class, "solid")]',
                    'xpath://div[contains(@class, "SessionOverlay")]//button[.//div[text()="Proceed"]]',
                    # More direct approach - look for button with Proceed text anywhere
                    'xpath://button[contains(., "Proceed")]',
                    'xpath://button[.//div[text()="Proceed"]]',
                    'xpath://*[contains(@class, "FABy8__content") and text()="Proceed"]/..',
                    "@text():Proceed",
                    'xpath://button[text()="Proceed"]',
                    'xpath://button[contains(text(), "Proceed")]',
                    'xpath://*[text()="Proceed"]',
                    'xpath://*[contains(text(), "Proceed")]'
                ]
                
                # First, let's debug what buttons are available
                logging.info("=== DEBUGGING PROCEED BUTTON SEARCH ===")
                all_buttons_after_login = page.eles('xpath://button')
                logging.info(f"Found {len(all_buttons_after_login)} buttons after login")
                
                for i, btn in enumerate(all_buttons_after_login[:10]):
                    try:
                        btn_text = btn.text.strip()
                        btn_class = btn.attr('class') or 'no-class'
                        btn_html = btn.html[:200] if hasattr(btn, 'html') else 'no-html'
                        logging.info(f"Button {i+1}: text='{btn_text}', class='{btn_class[:100]}', html='{btn_html}'")
                    except Exception:
                        pass
                
                proceed_button = None
                for i, selector in enumerate(proceed_selectors):
                    try:
                        logging.info(f"🔍 Trying Proceed selector {i+1}: {selector}")
                        proceed_button = page.ele(selector, timeout=3)
                        if proceed_button:
                            logging.info(f"✅ Found Proceed button with selector {i+1}: {selector}")
                            break
                    except Exception as e:
                        logging.info(f"❌ Proceed selector {i+1} failed: {e}")
                        continue
                
                if proceed_button:
                    proceed_button.click()
                    logging.info("Proceed button clicked")
                    time.sleep(3)  # Wait longer after proceed
                    take_screenshot(page, "03_after_proceed.png", "After proceed button clicked")
                else:
                    logging.warning("⚠️  No proceed button found - checking if already past login...")
                
                # Verify we're successfully logged in by checking the current URL
                final_url = page.url
                logging.info(f"Final URL after login process: {final_url}")
                
                if "login.propstream.com" in final_url:
                    # Try to navigate to the main app to see if we're actually logged in
                    try:
                        logging.info("🔍 Attempting to navigate to main app to verify login...")
                        page.get("https://app.propstream.com/")
                        page.wait.doc_loaded(timeout=10)
                        time.sleep(3)
                        app_url = page.url
                        logging.info(f"URL after navigating to app: {app_url}")
                        
                        if "login" in app_url.lower():
                            logging.error("🚨 LOGIN FAILED - Still on login page after navigation")
                            take_screenshot(page, f"03b_still_on_login_attempt_{attempt}.png", f"Still on login page (attempt {attempt})")
                        else:
                            logging.info("✅ Successfully logged in - able to access app")
                            break  # Exit the retry loop since login was successful
                    except Exception as nav_e:
                        logging.error(f"🚨 LOGIN FAILED - Could not navigate to app: {nav_e}")
                        take_screenshot(page, f"03b_still_on_login_attempt_{attempt}.png", f"Still on login page (attempt {attempt})")
                    
                    # Check for error messages on the login page
                    error_messages = page.eles('xpath://*[contains(@class, "error") or contains(@class, "alert") or contains(text(), "Invalid") or contains(text(), "incorrect")]')
                    account_lockout_detected = False
                    if error_messages:
                        for i, error in enumerate(error_messages[:3]):
                            try:
                                error_text = error.text.strip()
                                if error_text:
                                    logging.error(f"Login error message {i+1}: {error_text}")
                                    # Check for account lockout warning
                                    if "attempt remaining" in error_text.lower() or "account is locked" in error_text.lower():
                                        account_lockout_detected = True
                                        logging.critical("🚨 ACCOUNT LOCKOUT WARNING DETECTED!")
                            except Exception:
                                pass
                    
                    # If account lockout is detected, wait much longer
                    if account_lockout_detected:
                        lockout_delay = random.uniform(300, 600)  # 5-10 minutes
                        logging.critical(f"⚠️  Account lockout warning - waiting {lockout_delay/60:.1f} minutes before retry")
                        time.sleep(lockout_delay)
                    
                    # Check if we need to solve CAPTCHA or other verification
                    captcha = page.ele('xpath://*[contains(@class, "captcha") or contains(@class, "recaptcha")]', timeout=2)
                    if captcha:
                        logging.error("CAPTCHA detected - manual intervention may be required")
                    
                    # Enhanced retry logic for login failures
                    if attempt < max_attempts:
                        retry_delay = random.uniform(15.0, 30.0)  # Longer delay between login attempts
                        logging.warning(f"⚠️  Login attempt {attempt} failed - waiting {retry_delay:.1f}s before retry")
                        time.sleep(retry_delay)
                        
                        # Clear browser cache/cookies before retry
                        try:
                            page.clear_cache()
                            page.clear_cookies()
                            logging.info("🧹 Cleared browser cache and cookies for retry")
                        except Exception as e:
                            logging.warning(f"Could not clear cache/cookies: {e}")
                        
                        # Refresh page and try again
                        page.refresh()
                        page.wait.doc_loaded(timeout=20)
                        time.sleep(5)  # Wait for page to fully load
                        continue
                    else:
                        logging.error("Login failed on final attempt - check credentials or account status")
                        raise Exception("Login failed - check credentials")
                else:
                    logging.info("✓ Successfully logged in and navigated away from login page")
                    
                    # Take screenshot of successful login destination
                    # take_screenshot(page, f"03c_successful_login_attempt_{attempt}.png", f"Successful login destination (attempt {attempt})")
                    
                    # Minimal delay after successful login
                    delay = random.uniform(1.0, 2.0)  # Minimal wait for maximum speed
                    logging.info(f"⏱️  Adding {delay:.1f}s delay after successful login")
                    time.sleep(delay)
                    
                break
            except Exception as e:
                logging.warning(f"Login attempt {attempt} failed: {e}")
                if attempt == max_attempts:
                    raise
                time.sleep(3)
                page.refresh()
                page.wait.doc_loaded(timeout=20)
                time.sleep(2)
        
    except Exception as e:
        logging.error(f"Login failed with error: {str(e)}")
        raise

def logout_from_propstream(page: WebPage) -> None:
    """
    Logout from PropStream to prevent session conflicts.
    """
    try:
        logging.info("🚪 Attempting to logout from PropStream...")
        
        # Look for the logout link in the sidebar
        logout_selectors = [
            'xpath://a[@href="/logout"]',
            'xpath://a[contains(text(), "Log Out")]',
            'xpath://a[contains(text(), "Logout")]',
            'xpath://*[contains(text(), "Log Out")]',
            'xpath://button[contains(text(), "Log Out")]',
            'xpath://button[contains(text(), "Logout")]'
        ]
        
        logout_clicked = False
        for selector in logout_selectors:
            try:
                logout_element = page.ele(selector, timeout=5)
                if logout_element:
                    logging.info(f"✅ Found logout element with selector: {selector}")
                    logout_element.click()
                    time.sleep(3)  # Wait for logout to complete
                    logging.info("✅ Logout element clicked successfully")
                    logout_clicked = True
                    break
            except Exception as e:
                logging.debug(f"Logout selector {selector} failed: {e}")
                continue
        
        if not logout_clicked:
            logging.warning("⚠️ Could not find logout element - trying alternative approach")
            # Try to navigate directly to logout URL
            try:
                page.get("https://app.propstream.com/logout")
                time.sleep(3)
                logging.info("✅ Navigated to logout URL directly")
            except Exception as e:
                logging.warning(f"Could not navigate to logout URL: {e}")
        
        # Verify logout was successful by checking if we're redirected to login page
        time.sleep(2)
        current_url = page.url
        if "login.propstream.com" in current_url or "logout" in current_url:
            logging.info("✅ Logout successful - redirected to login page")
        else:
            logging.warning(f"⚠️ Logout may not have been successful - current URL: {current_url}")
        
        # Enhanced cleanup after logout
        try:
            # Clear all cookies and cache to ensure clean state
            page.clear_cookies()
            page.clear_cache()
            logging.info("🧹 Cleared cookies and cache after logout")
        except Exception as e:
            logging.warning(f"Could not clear cookies/cache: {e}")
        
        # Wait a bit longer to ensure complete logout
        time.sleep(5)
            
    except Exception as e:
        logging.error(f"Error during logout: {str(e)}")
        # Don't raise the exception - just log it and continue
        # This prevents logout failures from breaking the entire process

@lru_cache(maxsize=1000)
def get_cached_state_abbreviation(state: str) -> str:
    return get_state_abbreviation(state)

def search_property(page: WebPage, address_format: str, apn: str, county: str, state: str) -> None:
    """
    Search for a property on Propstream with enhanced session validation
    """
    state_abbr = get_cached_state_abbreviation(state)
    county = county.strip()
    
    # Validate county format before proceeding
    if not validate_county_format(county, state):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid county format: {county}. Please use proper county name format."
        )
    
    # Create property_info for screenshots
    property_info = f"{apn}_{county}_{state_abbr}".replace(" ", "_").replace(",", "")
    
    # Format county name properly without duplication
    county = format_county_name(county)
    logging.info(f"County name formatted to: {county}")

    address = address_format.format(apn, county, state_abbr)
    
    max_retries = 1  # Single attempt for maximum speed
    retry_delay = 2  # Minimal retry delay
    
    for attempt in range(max_retries):
        try:
            logging.info(f"Searching for address: {address} (Attempt {attempt + 1}/{max_retries})")
            
            # Navigate to the search page (correct URL based on the images)
            page.get("https://app.propstream.com/search")
            page.wait.load_start()
            logging.info("Navigated to search page")
            
            # Wait for document and dynamic content to load
            page.wait.doc_loaded(timeout=5)
            time.sleep(0.5)  # Minimal wait for speed
            
            # Check if we got redirected back to login immediately
            current_url_before_search = page.url
            logging.info(f"URL after navigating to search page: {current_url_before_search}")
            
            if "login.propstream.com" in current_url_before_search:
                logging.error("⚠️  Redirected to login page when trying to access search - session expired!")
                # Mark session as invalid and force retry with fresh browser
                browser_pool.invalidate_session()
                raise Exception(f"Session expired on attempt {attempt + 1} - will retry with fresh browser")
            
            # Take screenshot of search page to debug
            take_screenshot(page, f"04_search_page_attempt_{attempt+1}.png", f"Search page loaded (attempt {attempt+1})", property_info)
            logging.info(f"Successfully on search page: {current_url_before_search}")
            
            # Log page state
            logging.debug(f"Page title: {page.title}")
            logging.debug(f"Page URL: {page.url}")
            
            # Wait for page to fully load before looking for search input
            logging.info("⏳ Waiting for page to fully load...")
            
            # Wait for page to fully load (inline function)
            start_time = time.time()
            timeout = 60
            while time.time() - start_time < timeout:
                if page.title and page.title != "Loading...":
                    break
                time.sleep(1)
            else:
                logging.warning("⚠️ Page still loading after 60 seconds")

            # Minimal wait for JavaScript to initialize
            time.sleep(1)  # Minimal wait for maximum speed

            # Check if page is still loading
            if page.title == "Loading...":
                logging.warning("⚠️ Page still loading after wait - trying to refresh")
                page.refresh()
                time.sleep(5)
                # Wait again after refresh
                start_time = time.time()
                timeout = 30
                while time.time() - start_time < timeout:
                    if page.title and page.title != "Loading...":
                        break
                    time.sleep(1)
            
            # Locate search input field - based on the image, it's a prominent search bar
            search_input = page.ele('xpath://input[@placeholder="Enter County, City, Zip Code(s) or APN #"]', timeout=15)
            if not search_input:
                # Fallback to more generic selectors
                search_input = page.ele('xpath://input[@type="text" or @name="search" or contains(@class, "search") or @placeholder[contains(., "search")]]', timeout=15)
                if not search_input:
                    # Take screenshot to see what page we're actually on
                    take_screenshot(page, f"05_no_search_input_attempt_{attempt+1}.png", f"No search input found (attempt {attempt+1})", property_info)
                    logging.error(f"Search input not found. Page HTML: {page.html[:2000]}...")
                    logging.error(f"Page title: {page.title}")
                    logging.error(f"Current URL: {page.url}")
                    raise Exception("Search input field not found")
            
            # Clear any existing text and enter the address
            search_input.clear()
            search_input.input(address)
            logging.info("Address entered in search")
            
            # Minimal wait for dropdown suggestions
            time.sleep(0.5)  # Minimal wait for maximum speed
            
            # Look for dropdown suggestion that matches our APN
            logging.info("🔍 Looking for dropdown suggestion...")
            dropdown_suggestions = page.eles('xpath://*[contains(@class, "dropdown") or contains(@class, "suggestion") or contains(@class, "autocomplete")]//div | //*[contains(text(), "APN")]')
            
            suggestion_clicked = False
            if dropdown_suggestions:
                logging.info(f"Found {len(dropdown_suggestions)} dropdown suggestions")
                
                for i, suggestion in enumerate(dropdown_suggestions[:5]):
                    try:
                        suggestion_text = suggestion.text.strip()
                        logging.info(f"📋 Suggestion {i+1}: '{suggestion_text}'")
                        
                        # Check if this suggestion contains our APN
                        if apn in suggestion_text or "APN" in suggestion_text:
                            logging.info(f"🎯 Clicking on matching suggestion: {suggestion_text}")
                            suggestion.click()
                            suggestion_clicked = True
                            time.sleep(2)
                            break
                    except Exception as e:
                        logging.warning(f"Could not click suggestion {i+1}: {e}")
            
            # Always perform the search after dropdown selection or manual entry
            if not suggestion_clicked:
                logging.info("No matching dropdown suggestion found, pressing Enter to perform search...")
                search_input.input('\n')  # Simulate Enter key
            else:
                logging.info("✅ Clicked on dropdown suggestion")
            
            # Additional step: Look for and click the Search button to ensure search is executed
            logging.info("🔍 Looking for Search button to execute the search...")
            search_button_selectors = [
                # Based on the actual HTML structure: <span class="src-app-Search-Header-style__GatsT__searchText">Search</span>
                'xpath://span[contains(@class, "src-app-Search-Header-style__GatsT__searchText") and text()="Search"]/..',
                'xpath://span[contains(@class, "GatsT__searchText") and text()="Search"]/..',
                'xpath://span[text()="Search"]/..',
                'xpath://*[contains(@class, "searchText") and text()="Search"]/..',
                # Fallback selectors
                'xpath://button[contains(text(), "Search")]',
                'xpath://button[@type="submit"]',
                'xpath://*[contains(@class, "search")]//button',
                'xpath://button[contains(@class, "search")]',
                'xpath://input[@type="submit"]'
            ]
            
            search_button_clicked = False
            for i, selector in enumerate(search_button_selectors):
                try:
                    logging.info(f"🔍 Trying Search button selector {i+1}: {selector}")
                    search_button = page.ele(selector, timeout=3)
                    if search_button:
                        logging.info(f"✅ Found Search button with selector {i+1}: {selector}")
                        search_button.click()
                        logging.info("🎯 Search button clicked!")
                        search_button_clicked = True
                        time.sleep(0.5)  # Minimal wait for maximum speed
                        break
                except Exception as e:
                    logging.info(f"❌ Search button selector {i+1} failed: {e}")
                    continue
            
            if not search_button_clicked:
                logging.info("No explicit Search button found - relying on dropdown selection or Enter key")
            
            # Wait for search results to load and the page to update with results
            page.wait.doc_loaded(timeout=5)
            time.sleep(1)  # Minimal wait for maximum speed
            
            # Wait for the search page to be updated with search results
            logging.info("=== WAITING FOR SEARCH PAGE TO UPDATE WITH RESULTS ===")
            max_wait_attempts = 5  # Reduced to 5 attempts for maximum speed
            search_results_loaded = False
            
            for wait_attempt in range(max_wait_attempts):
                logging.info(f"Waiting for page to update with search results... attempt {wait_attempt + 1}/{max_wait_attempts}")
                
                # Check if the page has been updated with search results
                # Look for the specific indicators that show search results are loaded
                search_indicators = [
                    'xpath://*[contains(text(), "Nearby")]',  # "Nearby" section header
                    'xpath://table//td[contains(text(), "$")]',  # Price cells in results table
                    'xpath://a[contains(@href, "/search/")]',  # Details anchors with /search/ href
                    'xpath://*[contains(text(), "Estimated") and contains(text(), "Value")]',  # Estimated Value column
                    'xpath://table//a[contains(., "Details")]',  # Details anchors in table
                    f'xpath://*[contains(text(), "{apn}")]',  # Our specific APN in results
                    'xpath://table//tr[position()>1]',  # Table rows with data
                    'xpath://*[contains(text(), "Sale Amount")]',  # Sale Amount column header
                    'xpath://*[contains(text(), "Sale Date")]',  # Sale Date column header
                ]
                
                # Also try scrolling down to ensure search results are visible
                if wait_attempt == 2:  # On 3rd attempt, try scrolling
                    logging.info("📜 Scrolling down to check for search results...")
                    try:
                        page.scroll.to_bottom()
                        time.sleep(2)
                        page.scroll.to_top()
                        time.sleep(2)
                    except Exception as e:
                        logging.warning(f"Could not scroll: {e}")
                
                for indicator in search_indicators:
                    try:
                        elements = page.eles(indicator, timeout=2)
                        if elements:
                            logging.info(f"✅ Found search results indicator: {indicator} ({len(elements)} elements)")
                            search_results_loaded = True
                            break
                    except Exception:
                        continue
                
                if search_results_loaded:
                    logging.info("✅ Search results have been loaded on the page!")
                    break
                
                logging.info(f"Page not yet updated with results, waiting 1 more second...")
                time.sleep(1)  # Minimal wait for maximum speed
            
            if not search_results_loaded:
                logging.warning("⚠️ Search results may not have loaded properly after maximum wait time")
            
            # Minimal wait for dynamic content
            time.sleep(0.5)  # Minimal wait for maximum speed
            
            # Take screenshot after search to see the results
            logging.info(f"URL after search: {page.url}")
            
            # Take screenshot of search results immediately after search
            take_screenshot(page, "01_search_results.png", "Search results page", property_info)
            
            # Check for and handle the specific session conflict dialog
            logging.info("🔍 Checking for session conflict dialog...")
            try:
                # Look for session conflict popups with multiple selectors
                session_popup_selectors = [
                    'xpath://div[contains(@class, "src-app-components-SessionOverlay-style__oBRns__popup")]',
                    'xpath://div[contains(text(), "username you are using is currently still logged in")]',
                    'xpath://div[contains(text(), "prior session not being properly logged out")]',
                    'xpath://div[contains(text(), "another user is using the same account credentials")]',
                    'xpath://*[contains(text(), "PROCEED") and contains(text(), "Log Out")]',
                    'xpath://div[contains(@class, "modal") or contains(@class, "popup") or contains(@class, "dialog")]',
                    'xpath://button[contains(text(), "Proceed")]',
                    'xpath://button[contains(text(), "Log Out")]'
                ]
                
                session_popup_found = False
                for selector in session_popup_selectors:
                    try:
                        popup_element = page.ele(selector, timeout=3)
                        if popup_element:
                            logging.info(f"⚠️ Found session conflict popup with selector: {selector}")
                            session_popup_found = True
                            break
                    except Exception:
                        continue
                
                if session_popup_found:
                    # Click "Proceed" to continue with current session
                    try:
                        proceed_button = page.ele('xpath://button[contains(text(), "Proceed")]', timeout=5)
                        if proceed_button:
                            proceed_button.click()
                            logging.info("✅ Clicked 'Proceed' on session conflict popup")
                            time.sleep(3)
                        else:
                            # Fallback: try to click any button in the popup
                            popup_buttons = page.eles('xpath://div[contains(@class, "modal") or contains(@class, "popup") or contains(@class, "dialog")]//button')
                            if popup_buttons:
                                popup_buttons[0].click()  # Click first button (usually Proceed)
                                logging.info("✅ Clicked first button in session conflict popup")
                                time.sleep(3)
                    except Exception as e:
                        logging.warning(f"Could not handle session conflict popup: {e}")
                else:
                    logging.info("✅ No session conflict dialog found")
            except Exception as e:
                logging.warning(f"Could not check for session conflict dialog: {e}")
            
            # Try to switch from map view to list/table view to find Details buttons
            logging.info("🗺️ Attempting to switch from map view to list view...")
            try:
                # Look for view toggle buttons (map/list view) - more aggressive search
                view_selectors = [
                    'xpath://button[contains(text(), "List") or contains(text(), "Table") or contains(text(), "View") or contains(text(), "Toggle")]',
                    'xpath://button[contains(@title, "List") or contains(@title, "Table") or contains(@title, "View")]',
                    'xpath://button[contains(@aria-label, "List") or contains(@aria-label, "Table") or contains(@aria-label, "View")]',
                    'xpath://div[contains(@class, "view") or contains(@class, "toggle")]//button',
                    'xpath://button[contains(@class, "view") or contains(@class, "toggle")]',
                    'xpath://*[contains(text(), "List View") or contains(text(), "Table View") or contains(text(), "Map View")]'
                ]
                
                view_found = False
                for selector in view_selectors:
                    view_buttons = page.eles(selector)
                    if view_buttons:
                        logging.info(f"🔍 Found {len(view_buttons)} potential view toggle buttons with selector: {selector}")
                        for i, btn in enumerate(view_buttons):
                            btn_text = btn.text.strip()
                            btn_title = btn.attr('title') or ''
                            btn_class = btn.attr('class') or ''
                            logging.info(f"Button {i+1}: text='{btn_text}', title='{btn_title}', class='{btn_class[:50]}'")
                            
                            # Click any button that might switch views
                            if any(keyword in btn_text.lower() or keyword in btn_title.lower() 
                                   for keyword in ['list', 'table', 'view', 'toggle', 'switch']):
                                logging.info(f"🎯 Clicking view toggle button: {btn_text}")
                                btn.click()
                                time.sleep(3)  # Wait for view to change
                                view_found = True
                                break
                        if view_found:
                            break
                
                if not view_found:
                    logging.info("⚠️ No view toggle buttons found - trying to click on table elements directly")
                    # Try clicking on table headers or any clickable table elements
                    table_headers = page.eles('xpath://table//th[contains(@class, "clickable") or contains(@style, "cursor")]')
                    if table_headers:
                        logging.info(f"🔍 Found {len(table_headers)} clickable table headers")
                        for header in table_headers[:3]:  # Try first 3 headers
                            try:
                                header.click()
                                time.sleep(2)
                                logging.info("🎯 Clicked on table header")
                            except:
                                pass
            except Exception as e:
                logging.warning(f"Could not switch to list view: {e}")
            
            # Debug: Log page content to understand structure
            logging.info("=== DEBUGGING PAGE CONTENT ===")
            try:
                page_html = page.html
                logging.info(f"Page HTML length: {len(page_html)} characters")
                
                # Look for key indicators in the HTML
                if "Details" in page_html:
                    logging.info("✅ 'Details' text found in HTML")
                    details_count = page_html.count("Details")
                    logging.info(f"Found {details_count} occurrences of 'Details' in HTML")
                else:
                    logging.warning("❌ 'Details' text NOT found in HTML")
                    
                    # Debug: Check what content is actually on the page
                    if "No results found" in page_html or "no results" in page_html.lower():
                        logging.warning("🚫 No search results found for this property")
                    elif "Nearby" in page_html:
                        logging.info("✅ 'Nearby' section found - search results exist but no Details anchors")
                        # Log some sample table content to understand the structure
                        if "<table" in page_html:
                            logging.info("📋 Table structure exists - checking table content...")
                            # Extract a sample of the table content for debugging
                            table_start = page_html.find("<table")
                            if table_start > -1:
                                table_sample = page_html[table_start:table_start+2000]
                                logging.info(f"📋 Table sample (first 500 chars): {table_sample[:500]}...")
                    else:
                        logging.warning("❌ No 'Nearby' section found - search results may not have loaded")
                
                if apn in page_html:
                    logging.info(f"✅ APN '{apn}' found in HTML")
                else:
                    logging.warning(f"❌ APN '{apn}' NOT found in HTML")
                
                # Look for table structure
                if "<table" in page_html:
                    logging.info("✅ Table structure found in HTML")
                    table_count = page_html.count("<table")
                    logging.info(f"Found {table_count} tables in HTML")
                else:
                    logging.warning("❌ No table structure found in HTML")
                
            except Exception as e:
                logging.warning(f"Error analyzing page HTML: {e}")
            
            # NEW: Navigate to property details page by clicking the Details anchor
            logging.info("=== NAVIGATING TO PROPERTY DETAILS PAGE ===")
            
            # Wait for Details buttons to load dynamically - they load VERY slowly
            logging.info("⏳ Waiting for Details buttons to load dynamically...")
            
            # Retry loop to wait for Details buttons to appear
            details_found = False
            max_details_wait = 3  # Reduced to 3 attempts (15 seconds max) for maximum speed
            for details_wait in range(max_details_wait):
                # Take screenshot during loading/waiting phase
                if details_wait == 0:  # First attempt
                    take_screenshot(page, "02_loading_waiting.png", "Loading/waiting for Details buttons", property_info)
                time.sleep(2)  # Minimal wait for maximum speed
                
                # Scroll to ensure all elements are visible
                try:
                    page.scroll.to_bottom()
                    time.sleep(1)
                    page.scroll.to_top()
                    time.sleep(1)
                except Exception as e:
                    logging.warning(f"Could not scroll page: {e}")
                
                # Check if Details buttons have appeared
                try:
                    html_content = page.html
                    details_count = html_content.count("Details")
                    textbutton_count = html_content.count("textButton")
                    search_href_count = html_content.count('href="/search/')
                    table_rows = html_content.count("<tr>")
                    
                    logging.info(f"⏳ Wait attempt {details_wait + 1}/{max_details_wait}: Details={details_count}, textButton={textbutton_count}, search_href={search_href_count}, table_rows={table_rows}")
                    
                    # More flexible condition - if we have table rows and either Details or search_href, proceed
                    if (details_count > 0 and textbutton_count > 0 and search_href_count > 0) or (table_rows > 5 and search_href_count > 0):
                        logging.info("✅ Details buttons or search results have appeared!")
                        # Take screenshot when Details buttons are found
                        take_screenshot(page, "03_details_found.png", "Details buttons found", property_info)
                        details_found = True
                        break
                        
                except Exception as e:
                    logging.warning(f"Could not check for Details buttons: {e}")
            
            if not details_found:
                logging.warning("⚠️ Details buttons did not appear after extended wait - refreshing page")
                # Refresh the page and clear search input instead of forcing session refresh
                page.refresh()
                time.sleep(3)
                # Clear search input and try again
                search_input = page.ele('xpath://input[@placeholder="Enter County, City, Zip Code(s) or APN #"]', timeout=10)
                if search_input:
                    search_input.clear()
                    logging.info("🔄 Page refreshed and search input cleared - retrying search")
                else:
                    logging.warning("⚠️ Could not find search input after refresh")
                raise Exception(f"Details buttons did not appear after extended wait on attempt {attempt + 1} - page refreshed")
            
            # Final wait for any pending JavaScript to complete
            try:
                page.run_js("return document.readyState === 'complete'")
                time.sleep(2)
                logging.info("✅ Page ready state confirmed")
            except Exception as e:
                logging.warning(f"Could not check page ready state: {e}")
            
            # Look for the Details anchor in the search results
            # Based on the actual HTML structure: <a class="src-components-base-Button-style__MOJLh__border-blue src-app-Search-Property-style__fpSUR__textButton" href="/search/1848199534"><span class="src-components-base-Button-style__FBPrq__text">Details</span></a>
            details_selectors = [
                # Most specific selector matching the exact structure
                'xpath://a[contains(@class, "src-app-Search-Property-style__fpSUR__textButton")]//span[text()="Details"]/..',
                'xpath://a[contains(@class, "fpSUR__textButton")]//span[text()="Details"]/..',
                'xpath://a[contains(@class, "textButton")]//span[text()="Details"]/..',
                # Alternative selectors for the anchor itself
                'xpath://a[contains(@class, "src-app-Search-Property-style__fpSUR__textButton")]',
                'xpath://a[contains(@class, "fpSUR__textButton")]',
                'xpath://a[contains(@href, "/search/") and contains(@class, "textButton")]',
                # Fallback selectors
                'xpath://a[.//span[text()="Details"]]',
                'xpath://a[contains(text(), "Details")]',
                'xpath://span[text()="Details"]/..',
                'xpath://a[text()="Details"]',
                'xpath://table//a[text()="Details"]',
                'xpath://*[contains(@class, "table")]//a[contains(text(), "Details")]',
                'xpath://tr//a[text()="Details"]'
            ]
            
            # Debug: Check what's actually in the HTML before trying selectors
            try:
                html_content = page.html
                details_count = html_content.count("Details")
                logging.info(f"🔍 HTML contains {details_count} occurrences of 'Details' text")
                
                # Check for common button patterns
                button_patterns = [
                    "textButton",
                    "fpSUR",
                    "Details",
                    "href=\"/search/",
                    "class=\"src-app-Search-Property"
                ]
                for pattern in button_patterns:
                    count = html_content.count(pattern)
                    logging.info(f"🔍 HTML contains {count} occurrences of '{pattern}'")
                    
            except Exception as e:
                logging.warning(f"Could not analyze HTML content: {e}")
            
            details_anchor = None
            for i, selector in enumerate(details_selectors):
                try:
                    logging.info(f"🔍 Trying Details selector {i+1}: {selector}")
                    details_anchor = page.ele(selector, timeout=5)
                    if details_anchor:
                        logging.info(f"✅ Found Details anchor with selector {i+1}: {selector}")
                        break
                except Exception as e:
                    logging.info(f"❌ Details selector {i+1} failed: {e}")
                    continue
            
            if not details_anchor:
                # Debug: Log all anchors to understand the page structure
                logging.info("🔍 Debugging: Listing all anchors on the page...")
                all_anchors = page.eles('xpath://a')
                logging.info(f"Found {len(all_anchors)} total anchors on the page")
                
                for i, anchor in enumerate(all_anchors[:10]):  # Log first 10 anchors
                    try:
                        anchor_text = anchor.text.strip()
                        anchor_href = anchor.attr('href') or 'no-href'
                        anchor_class = anchor.attr('class') or 'no-class'
                        logging.info(f"Anchor {i+1}: text='{anchor_text}', href='{anchor_href}', class='{anchor_class[:100]}'")
                        
                        if "Details" in anchor_text:
                            details_anchor = anchor
                            logging.info(f"✅ Found Details anchor by text search: '{anchor_text}'")
                            break
                    except Exception as e:
                        logging.warning(f"Error examining anchor {i+1}: {e}")
                        continue
                
                # If still no Details anchor, try to find any clickable element in search results
                if not details_anchor:
                    logging.info("🔍 Trying to find any clickable element in search results...")
                    try:
                        # Look for any anchor with href containing /search/ (property detail links)
                        search_anchors = page.eles('xpath://a[contains(@href, "/search/")]')
                        if search_anchors:
                            logging.info(f"✅ Found {len(search_anchors)} search result anchors")
                            details_anchor = search_anchors[0]  # Use the first one
                            logging.info("✅ Using first search result anchor as Details anchor")
                        else:
                            # Look for any clickable element in table rows
                            table_links = page.eles('xpath://table//a')
                            if table_links:
                                logging.info(f"✅ Found {len(table_links)} table links")
                                details_anchor = table_links[0]  # Use the first one
                                logging.info("✅ Using first table link as Details anchor")
                    except Exception as e:
                        logging.warning(f"Could not find alternative clickable elements: {e}")
            
            # ALWAYS try sidebar extraction first - this is the fastest method
            logging.info("🔍 Attempting sidebar extraction (primary method)...")
            sidebar_data = extract_property_from_sidebar(page, apn)
            
            if sidebar_data and sidebar_data.get('acreage'):
                logging.info("✅ Successfully extracted property data from sidebar - skipping details page navigation")
                logging.info(f"📊 Sidebar data: {sidebar_data}")
                # Don't navigate to details page if we have the data we need
                return
            else:
                logging.info("⚠️ Sidebar extraction failed - trying details page as fallback...")
                if details_anchor:
                    logging.info("🎯 Clicking Details anchor to navigate to property details page...")
                    details_anchor.click()
                    
                    # Wait for the details page to load
                    page.wait.doc_loaded(timeout=5)
                    time.sleep(1)  # Minimal wait for maximum speed
                    
                    # Take screenshot of details page
                    logging.info(f"✅ Successfully navigated to details page. URL: {page.url}")
                else:
                    logging.error("❌ No Details anchor found - forcing session refresh and retry")
                    # Force session refresh when no Details buttons are found
                    browser_pool.invalidate_session()
                    raise Exception(f"No Details buttons found on attempt {attempt + 1} - forcing session refresh")
            
            # Property search and navigation completed - ready for data extraction
            logging.info("🎯 Property search and navigation completed - ready for data extraction")
            
            # Add this right after navigating to search page
            logging.info(f"Page title after navigation: {page.title}")
            logging.info(f"Page URL after navigation: {page.url}")
            logging.info(f"Page HTML length: {len(page.html)} characters")

            # Check if we're actually on the right page
            if "propstream" not in page.url.lower():
                logging.error(f"⚠️ Not on PropStream page! Current URL: {page.url}")
                take_screenshot(page, f"06_wrong_page_attempt_{attempt+1}.png", f"Wrong page detected (attempt {attempt+1})", property_info)
            
            return
        
        except Exception as e:
            logging.warning(f"Attempt {attempt + 1} failed: {str(e)}")
            
            # Track failures for session refresh logic
            if "No Details buttons found" in str(e) or "Details anchor not found" in str(e):
                browser_pool.recent_failures = getattr(browser_pool, 'recent_failures', 0) + 1
                logging.info(f"📊 Details button failure count: {browser_pool.recent_failures}")
            
            if attempt < max_retries - 1:
                logging.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                # Don't force fresh browser for page refresh issues - just retry with same session
                if "page refreshed" in str(e):
                    logging.info("🔄 Retrying with same session after page refresh")
                elif "No Details buttons found" in str(e) or "Details anchor not found" in str(e):
                    logging.info("🔄 Forcing fresh browser due to Details button issues")
                    browser_pool.invalidate_session()
            else:
                logging.error(f"Property search failed after {max_retries} attempts: {str(e)}")
                raise HTTPException(
                    status_code=404,
                    detail=f"No search results found for APN# {apn}, {county}, {state_abbr}"
                )

def extract_coordinates(html: str) -> Optional[Tuple[float, float]]:
    soup = BeautifulSoup(html, "html.parser")
    
    # Try multiple methods to find coordinates
    
    # Method 1: Look for Google Maps links
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if "maps.google.com/maps" in href and "ll=" in href:
            match = re.search(r"ll=([-+]?\d*\.\d+),([-+]?\d*\.\d+)", href)
            if match:
                try:
                    return float(match.group(1)), float(match.group(2))
                except ValueError:
                    logging.warning("Failed to convert coordinates to float.")
    
    # Method 2: Look for data attributes that might contain coordinates
    for element in soup.find_all(attrs={"data-lat": True, "data-lng": True}):
        try:
            lat = float(element.get("data-lat"))
            lng = float(element.get("data-lng"))
            return lat, lng
        except (ValueError, TypeError):
            continue
    
    # Method 3: Look for coordinates in script tags
    for script in soup.find_all("script"):
        if script.string:
            # Look for latitude/longitude patterns
            lat_match = re.search(r'"latitude":\s*([-+]?\d*\.\d+)', script.string)
            lng_match = re.search(r'"longitude":\s*([-+]?\d*\.\d+)', script.string)
            if lat_match and lng_match:
                try:
                    return float(lat_match.group(1)), float(lng_match.group(1))
                except ValueError:
                    continue
    
    # Method 4: Look for coordinates in the page URL or meta tags
    for meta in soup.find_all("meta", attrs={"name": ["geo.position", "latitude", "longitude"]}):
        content = meta.get("content", "")
        if "latitude" in meta.get("name", "").lower():
            try:
                lat = float(content)
                # Look for corresponding longitude
                for lng_meta in soup.find_all("meta", attrs={"name": "longitude"}):
                    try:
                        lng = float(lng_meta.get("content", ""))
                        return lat, lng
                    except ValueError:
                        continue
            except ValueError:
                continue
    
    logging.warning("Coordinates not found in HTML.")
    return None

# Fallback: geocode county/state using Nominatim if PropStream page has no coords
def geocode_location(query: str, timeout: int = 15) -> Optional[Tuple[float, float]]:
    try:
        headers = {"User-Agent": "sundial-realstate-scrape/1.0 (+contact@example.com)"}
        params = {"q": query, "format": "json", "limit": 1}
        resp = requests.get("https://nominatim.openstreetmap.org/search", params=params, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                lat = float(data[0]["lat"])  # type: ignore
                lon = float(data[0]["lon"])  # type: ignore
                return lat, lon
        logging.warning(f"Geocoding failed for '{query}' with status {resp.status_code}")
    except Exception as e:
        logging.warning(f"Geocoding error for '{query}': {e}")
    return None

def fetch_property_from_regrid(apn: str, county: str, state: str) -> Optional[Dict]:
    """
    Fetch property data from Regrid API using APN, county, and state.
    Returns property data in the same format as PropStream scraping.
    """
    try:
        settings = Settings()
        if not settings.regrid_api_token:
            logging.warning("Regrid API token not configured")
            return None
            
        # Build the path parameter for Regrid API
        state_abbr = get_cached_state_abbreviation(state).lower()
        county_clean = county.lower().replace(" county", "").replace(" ", "-")
        path = f"/us/{state_abbr}/{county_clean}"
        
        url = "https://app.regrid.com/api/v2/parcels/apn"
        headers = {
            "Authorization": f"Bearer {settings.regrid_api_token}",
            "Content-Type": "application/json"
        }
        params = {
            "parcelnumb": apn,
            "path": path
        }
        
        logging.info(f"🔍 Fetching property data from Regrid API for APN: {apn}, Path: {path}")
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            if data.get("parcels") and data["parcels"].get("features"):
                feature = data["parcels"]["features"][0]  
                properties = feature.get("properties", {})
                fields = properties.get("fields", {})
                
                logging.info(f"✅ Successfully fetched property data from Regrid for APN: {apn}")
                
                # Extract and map data to match PropStream format
                property_data = {
                    "address": fields.get("address", ""),
                    "lot_size_acres": None,
                    "lot_size_sqft": fields.get("ll_gissqft", 0),
                    "latitude": float(fields.get("lat", 0)) if fields.get("lat") else None,
                    "longitude": float(fields.get("lon", 0)) if fields.get("lon") else None,
                    "land_value": fields.get("landval", 0),
                    "improvement_value": fields.get("improvval", 0),
                    "total_assessed_value": fields.get("parval", 0),
                    "owner": fields.get("owner", ""),
                    "zoning": fields.get("zoning", ""),
                    "zoning_description": fields.get("zoning_description", ""),
                    "use_description": fields.get("usedesc", ""),
                    "county": fields.get("county", county),
                    "state": fields.get("state2", state),
                    "zip_code": fields.get("szip5", ""),
                    "data_source": "regrid"
                }
                
                # Convert square feet to acres if we have lot size
                if property_data["lot_size_sqft"]:
                    property_data["lot_size_acres"] = property_data["lot_size_sqft"] / 43560
                
                logging.info(f"📊 Regrid data extracted - Address: {property_data['address']}, "
                           f"Lot Size: {property_data['lot_size_acres']:.2f} acres, "
                           f"Coordinates: ({property_data['latitude']}, {property_data['longitude']})")
                
                return property_data
            else:
                logging.warning(f"⚠️ No property found in Regrid for APN: {apn}")
                return None
        else:
            logging.warning(f"⚠️ Regrid API request failed with status {response.status_code}: {response.text}")
            return None
            
    except Exception as e:
        logging.error(f"❌ Error fetching from Regrid API: {e}")
        return None

def extract_property_from_sidebar_regrid(property_data: Dict) -> Optional[Dict]:
    """
    Extract property information from Regrid data (similar to sidebar extraction from PropStream).
    This replaces the PropStream sidebar extraction with Regrid data.
    """
    try:
        if not property_data or property_data.get("data_source") != "regrid":
            return None
            
        logging.info("📋 Extracting property data from Regrid response...")
        
        # Extract lot size from Regrid data
        lot_size_acres = property_data.get("lot_size_acres")
        lot_size_sqft = property_data.get("lot_size_sqft")
        
        if lot_size_acres and lot_size_acres > 0:
            logging.info(f"✅ Found lot size from Regrid: {lot_size_acres:.2f} acres ({lot_size_sqft} sqft)")
            
            return {
                "lot_size_acres": lot_size_acres,
                "lot_size_sqft": lot_size_sqft,
                "address": property_data.get("address", ""),
                "latitude": property_data.get("latitude"),
                "longitude": property_data.get("longitude"),
                "land_value": property_data.get("land_value", 0),
                "improvement_value": property_data.get("improvement_value", 0),
                "total_assessed_value": property_data.get("total_assessed_value", 0),
                "owner": property_data.get("owner", ""),
                "zoning": property_data.get("zoning", ""),
                "zoning_description": property_data.get("zoning_description", ""),
                "use_description": property_data.get("use_description", ""),
                "county": property_data.get("county", ""),
                "state": property_data.get("state", ""),
                "zip_code": property_data.get("zip_code", ""),
                "data_source": "regrid"
            }
        else:
            logging.warning("⚠️ No valid lot size found in Regrid data")
            return None
            
    except Exception as e:
        logging.error(f"❌ Error extracting property from Regrid data: {e}")
        return None

def extract_property_info(page: WebPage) -> Optional[Dict]:
    try:
        try:
            html = page.html
        except AlertExistsError:
            try:
                # Try both common methods to clear alerts depending on DrissionPage version
                try:
                    page.handle_alert(accept=True)
                except Exception:
                    try:
                        page.alert.accept()
                    except Exception:
                        pass
            finally:
                time.sleep(1)
            html = page.html
        soup = BeautifulSoup(html, "html.parser")
        property_info = {}
        
        logging.info("Extracting property information from PropStream details page...")
        
        # Take screenshot of the page we're extracting from
        # take_screenshot(page, "10_extraction_page.png", "Page during property info extraction")
        
        # Log current page state for debugging
        logging.info(f"=== CURRENT PAGE STATE ===")
        logging.info(f"Page URL: {page.url}")
        logging.info(f"Page title: {page.title}")
        
        # Check if we're on the right page for extraction
        if "login.propstream.com" in page.url:
            logging.error("🚨 CRITICAL: Trying to extract from LOGIN PAGE instead of property page!")
            logging.error("This explains why no property data is found")
        
        # NEW EXTRACTION LOGIC: Extract from property details page
        logging.info("=== EXTRACTING FROM PROPERTY DETAILS PAGE ===")
        
        # Method 1: Extract Lot Size from the details page structure
        # Based on your HTML: <div class="src-components-GroupInfo-style__FpyDf__label">Lot Size</div>
        # followed by: <div class="src-components-GroupInfo-style__sbtoP__value"><div>12.29 acres</div><div>535,352 SqFt.</div></div>
        
        lot_size_labels = soup.find_all("div", class_=lambda x: x and "GroupInfo-style__FpyDf__label" in x)
        logging.info(f"Found {len(lot_size_labels)} GroupInfo label elements")
        
        for label_elem in lot_size_labels:
            label_text = label_elem.get_text().strip()
            logging.info(f"📋 Checking label: '{label_text}'")
            
            if label_text == "Lot Size":
                logging.info("🎯 Found Lot Size label - looking for corresponding value...")
                
                # Find the corresponding value element (should be a sibling or in same container)
                parent = label_elem.parent
                if parent:
                    # Look for the value div with the specific class
                    value_elem = parent.find("div", class_=lambda x: x and "GroupInfo-style__sbtoP__value" in x)
                    if value_elem:
                        value_text = value_elem.get_text().strip()
                        logging.info(f"🎯 Found Lot Size value container: '{value_text}'")
                        
                        # Extract acres value from the structure like "12.29 acres535,352 SqFt."
                        acres_match = re.search(r"(\d+(?:\.\d+)?)\s*acres?", value_text, re.IGNORECASE)
                        if acres_match:
                            try:
                                acres_value = float(acres_match.group(1))
                                property_info["acreage"] = acres_value
                                logging.info(f"🏡 Successfully extracted acreage from details page: {acres_value} acres")
                                break
                            except ValueError as e:
                                logging.warning(f"Could not convert acres value: {e}")
                        else:
                            logging.warning(f"No acres pattern found in value text: '{value_text}'")
                    else:
                        logging.warning("No value element found for Lot Size label")
                else:
                    logging.warning("No parent element found for Lot Size label")
        
        # Method 2: Extract Estimated Value from the details page
        # Look for "Estimated Value" label and corresponding value
        for label_elem in lot_size_labels:  # Reuse the same label elements
            label_text = label_elem.get_text().strip()
            
            if "Estimated Value" in label_text or "Est. Value" in label_text:
                logging.info(f"🎯 Found Estimated Value label: '{label_text}'")
                
                parent = label_elem.parent
                if parent:
                    value_elem = parent.find("div", class_=lambda x: x and "GroupInfo-style__sbtoP__value" in x)
                    if value_elem:
                        value_text = value_elem.get_text().strip()
                        logging.info(f"💰 Found Estimated Value container: '{value_text}'")
                        
                        # Extract dollar amount
                        value_match = re.search(r"\$?([\d,]+)", value_text)
                        if value_match:
                            try:
                                value_str = value_match.group(1).replace(",", "")
                                property_info["estimated_value"] = float(value_str)
                                logging.info(f"💰 Successfully extracted estimated value: ${property_info['estimated_value']:,.2f}")
                                break
                            except ValueError as e:
                                logging.warning(f"Could not convert estimated value: {e}")
        
        # Method 3: Fallback extraction using more generic patterns for details page
        if "acreage" not in property_info or "estimated_value" not in property_info:
            logging.info("=== FALLBACK: GENERIC PATTERN EXTRACTION FROM DETAILS PAGE ===")
            
            # Look for any text containing acres
            if "acreage" not in property_info:
                acre_patterns = [
                    r"(\d+(?:\.\d+)?)\s*acres?",  # "12.29 acres"
                    r"Lot\s*Size[:\s]*.*?(\d+(?:\.\d+)?)\s*acres?",  # "Lot Size: ... 12.29 acres"
                ]
                
                for i, pattern in enumerate(acre_patterns):
                    logging.info(f"🔍 Trying acres pattern {i+1}: {pattern}")
                    acre_match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
                    if acre_match:
                        try:
                            acres_value = float(acre_match.group(1))
                            property_info["acreage"] = acres_value
                            logging.info(f"🏡 Found acreage from fallback pattern {i+1}: {acres_value} acres")
                            break
                        except ValueError as e:
                            logging.warning(f"Could not convert acres from pattern {i+1}: {e}")
            
            # Look for estimated value patterns
            if "estimated_value" not in property_info:
                value_patterns = [
                    r"Estimated\s+Value[:\s]*\$?([\d,]+)",  # "Estimated Value: $409,000"
                    r"Est\.?\s*Value[:\s]*\$?([\d,]+)",     # "Est. Value: $409,000"
                    r"\$(\d{1,3}(?:,\d{3})*)\s*EST\.\s*VALUE",  # "$387,000 EST. VALUE" - based on your image
                    r"<h3[^>]*>\$(\d{1,3}(?:,\d{3})*)</h3>",  # Large price display like "$387,000"
                    r"Estimated\s+Value[^$]*\$(\d{1,3}(?:,\d{3})*)",  # "Estimated Value ... $387,000"
                    r"\$?([\d,]+).*?(?:estimated|est\.?)\s*value",  # "$409,000 ... estimated value" (original pattern)
                ]
                
                for i, pattern in enumerate(value_patterns):
                    logging.info(f"🔍 Trying value pattern {i+1}: {pattern}")
                    value_match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
                    if value_match:
                        try:
                            value_str = value_match.group(1).replace(",", "")
                            property_info["estimated_value"] = float(value_str)
                            logging.info(f"💰 Found estimated value from fallback pattern {i+1}: ${property_info['estimated_value']:,.2f}")
                            break
                        except ValueError as e:
                            logging.warning(f"Could not convert value from pattern {i+1}: {e}")
        
        # Method 4: Extract from any table structure on the details page
        if "acreage" not in property_info or "estimated_value" not in property_info:
            logging.info("=== METHOD 4: TABLE EXTRACTION FROM DETAILS PAGE ===")
            
            tables = soup.find_all("table")
            logging.info(f"Found {len(tables)} tables on details page")
            
            for table_idx, table in enumerate(tables):
                rows = table.find_all("tr")
                
                for row in rows:
                    cells = row.find_all(["td", "th"])
                    if len(cells) >= 2:
                        label = cells[0].get_text().strip()
                        value = cells[1].get_text().strip()
                        
                        # Look for lot size/acreage
                        if "acreage" not in property_info and any(keyword in label.lower() for keyword in ["lot size", "lot area", "land area", "acreage"]):
                            logging.info(f"🎯 Found lot size in table: '{label}' = '{value}'")
                            
                            acre_match = re.search(r"(\d+(?:\.\d+)?)\s*acres?", value, re.IGNORECASE)
                            if acre_match:
                                try:
                                    acres_value = float(acre_match.group(1))
                                    property_info["acreage"] = acres_value
                                    logging.info(f"🏡 Extracted acreage from table: {acres_value} acres")
                                except ValueError:
                                    continue
                        
                        # Look for estimated value
                        if "estimated_value" not in property_info and any(keyword in label.lower() for keyword in ["estimated value", "est. value", "estimated val"]):
                            logging.info(f"🎯 Found estimated value in table: '{label}' = '{value}'")
                            
                            value_match = re.search(r"\$?([\d,]+)", value)
                            if value_match:
                                try:
                                    value_str = value_match.group(1).replace(",", "")
                                    property_info["estimated_value"] = float(value_str)
                                    logging.info(f"💰 Extracted estimated value from table: ${property_info['estimated_value']:,.2f}")
                                except ValueError:
                                    continue
        
        # Method 5: Look for any div/span elements containing the target information
        if "acreage" not in property_info or "estimated_value" not in property_info:
            logging.info("=== METHOD 5: GENERAL ELEMENT SEARCH ON DETAILS PAGE ===")
            
            # Search all text elements for acres and value information
            for elem in soup.find_all(["div", "span", "p", "td", "th"]):
                text = elem.get_text().strip()
                
                # Look for acreage
                if "acreage" not in property_info and "acre" in text.lower() and any(char.isdigit() for char in text):
                    acre_match = re.search(r"(\d+(?:\.\d+)?)\s*acres?", text, re.IGNORECASE)
                    if acre_match:
                        try:
                            acres_value = float(acre_match.group(1))
                            property_info["acreage"] = acres_value
                            logging.info(f"🏡 Found acreage from element text: '{text}' -> {acres_value} acres")
                        except ValueError:
                            continue
                
                # Look for estimated value
                if "estimated_value" not in property_info and ("estimated" in text.lower() or "est." in text.lower()) and "$" in text:
                    value_match = re.search(r"\$?([\d,]+)", text)
                    if value_match:
                        try:
                            value_str = value_match.group(1).replace(",", "")
                            estimated_val = float(value_str)
                            # Only accept reasonable property values (> $10,000)
                            if estimated_val > 10000:
                                property_info["estimated_value"] = estimated_val
                                logging.info(f"💰 Found estimated value from element text: '{text}' -> ${estimated_val:,.2f}")
                        except ValueError:
                            continue
        
        # Final validation and defaults
        if "acreage" not in property_info:
            logging.warning("⚠️ No acreage found on details page - using default value of 1.0 acres")
            property_info["acreage"] = 1.0
        
        if "estimated_value" not in property_info:
            logging.warning("⚠️ No estimated value found on details page")
            property_info["estimated_value"] = None
        
        logging.info(f"✅ Final extracted property info: {property_info}")
        return property_info
        
    except Exception as e:
        logging.error(f"Failed to extract property info: {e}")
        logging.error(traceback.format_exc())
        return None

def calculate_bounding_box(latitude: float, longitude: float, miles: float) -> Tuple[float, float, float, float]:
    offset = miles * MILES_TO_DEGREES
    north = latitude + offset
    south = latitude - offset
    lng_offset = offset * math.cos(latitude * math.pi / 180.0)
    east = longitude + lng_offset
    west = longitude - lng_offset
    return north, south, east, west

def fetch_zillow_data(page: Optional[WebPage], north: float, south: float, east: float, west: float) -> Tuple[List[Dict], List[Dict], str]:
    def get_url_for_page(page_num: Optional[int] = None) -> str:
        search_query_state = {
            "isMapVisible": True,
            "mapBounds": {
                "west": west,
                "east": east,
                "south": south,
                "north": north
            },
            "mapZoom": 14,
            "filterState": {
                "sort": {"value": "globalrelevanceex"},
                "sf": {"value": False},
                "tow": {"value": False},
                "mf": {"value": False},
                "con": {"value": False},
                "apa": {"value": False},
                "manu": {"value": False},
                "apco": {"value": False},
                "rs": {"value": True},
                "fsba": {"value": False},
                "fsbo": {"value": False},
                "nc": {"value": False},
                "cmsn": {"value": False},
                "auc": {"value": False},
                "fore": {"value": False}
            },
            "isListVisible": True
        }
        
        if page_num is not None:
            search_query_state["pagination"] = {"currentPage": page_num}
        
        import json
        encoded_state = json.dumps(search_query_state).replace('"', '%22').replace(' ', '').replace(':', '%3A').replace(',', '%2C').replace('{', '%7B').replace('}', '%7D')
        
        return f"https://www.zillow.com/homes/recently_sold/?searchQueryState={encoded_state}&category=SEMANTIC"

    url = "https://zillow-com1.p.rapidapi.com/searchByUrl"
    headers = {
        "x-rapidapi-host": "zillow-com1.p.rapidapi.com",
        "x-rapidapi-key": "0175965f55msh818f681aee07526p177aaejsnc9b3caa29390"
    }

    all_homes = []
    potential_homes = []
    current_page = 1
    total_pages = None
    max_pages = 2  # Optimized to 2 pages for speed while maintaining good results
    
    base_search_url = get_url_for_page()
    
    while (total_pages is None or current_page <= total_pages) and current_page <= max_pages:
        zillow_url = get_url_for_page(current_page)
        querystring = {"url": zillow_url}

        try:
            response = requests.get(url, headers=headers, params=querystring)
            response.raise_for_status()
            data = response.json()

            if total_pages is None:
                total_pages = min(data.get('totalPages', 1), max_pages)

            for property_data in data.get('props', []):
                try:
                    price = property_data.get('price')
                    lot_acres = property_data.get('lotAreaValue')
                    lot_area_unit = property_data.get('lotAreaUnit', '').lower()
                    zpid = property_data.get('zpid')

                    if not lot_acres or not zpid:
                        logging.info("Skipping property: Missing lot area or ZPID")
                        continue

                    if lot_area_unit == 'sqft':
                        lot_acres = float(lot_acres) / 43560

                    property_dict = {
                        "address": f"{property_data.get('streetAddress', '')}, {property_data.get('city', '')}, {property_data.get('state', '')} {property_data.get('zipcode', '')}",
                        "price": float(price) if price else None,
                        "price_text": f"${price:,.2f}" if price else "N/A",
                        "beds": str(property_data.get('bedrooms', 'N/A')),
                        "baths": str(property_data.get('bathrooms', 'N/A')),
                        "sqft": str(property_data.get('livingArea', 'N/A')),
                        "lot_size": f"{lot_acres:.2f} acres",
                        "acreage": float(lot_acres),
                        "price_per_acre": float(price) / float(lot_acres) if price and lot_acres > 0 else None,
                        "date_sold": property_data.get('dateSold'),
                        "home_type": property_data.get('homeType'),
                        "latitude": property_data.get('latitude'),
                        "longitude": property_data.get('longitude'),
                        "zpid": zpid
                    }

                    if price:
                        logging.info(f"Added property with price: {property_dict['address']}")
                        all_homes.append(property_dict)
                    else:
                        logging.info(f"Stored potential property without price: {property_dict['address']}")
                        potential_homes.append(property_dict)

                except Exception as e:
                    logging.warning(f"Error processing property data: {e}")
                    continue

            current_page += 1
            if current_page <= total_pages and current_page <= max_pages:
                time.sleep(0.5)  # Minimal wait for maximum speed

        except Exception as e:
            logging.error(f"Error fetching data from Zillow API on page {current_page}: {e}")
            break

    return all_homes, potential_homes, base_search_url

API_KEYS = [
    "0175965f55msh818f681aee07526p177aaejsnc9b3caa29390",
]

def get_api_key():
    return random.choice(API_KEYS)

def fetch_price_history(zpid: str, max_retries: int = 1) -> Optional[float]:  # Reduced retries for speed
    url = "https://zillow-com1.p.rapidapi.com/property"
    params = {"zpid": zpid}
    
    for retry in range(max_retries):
        headers = {
            "x-rapidapi-host": "zillow-com1.p.rapidapi.com",
            "x-rapidapi-key": get_api_key()
        }
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=8)  # Optimized timeout
            if response.status_code == 429:
                # Skip retry for rate limit - return None immediately for speed
                logging.info(f"Rate limit hit for ZPID {zpid}. Skipping for speed...")
                return None
                
            response.raise_for_status()
            data = response.json()
            price_history = data.get("priceHistory", [])
            for event in price_history:
                price = event.get("price")
                if price and price > 0:
                    logging.info(f"Fetched price {price} from history for ZPID {zpid}")
                    return float(price)
            logging.info(f"No valid price found in history for ZPID {zpid}")
            return None
        except Exception as e:
            # Skip retries for speed - return None immediately
            logging.warning(f"Error fetching price history for ZPID {zpid}: {e}. Skipping for speed...")
            return None

def fetch_price_history_parallel(zpids: List[str], max_workers: int = 10) -> Dict[str, Optional[float]]:
    """Fetch price history for multiple ZPIDs in parallel"""
    results = {}
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_zpid = {executor.submit(fetch_price_history, zpid): zpid for zpid in zpids}
        
        # Collect results as they complete
        for future in as_completed(future_to_zpid):
            zpid = future_to_zpid[future]
            try:
                result = future.result(timeout=12)  # 12 second timeout per request for reliability
                results[zpid] = result
            except Exception as e:
                logging.warning(f"Error in parallel fetch for ZPID {zpid}: {e}")
                results[zpid] = None
    
    return results

def filter_properties_by_acreage(properties: List[Dict], target_acreage: float) -> List[Dict]:
    min_acreage = target_acreage * MIN_ACREAGE_RATIO
    max_acreage = target_acreage * MAX_ACREAGE_RATIO
    
    filtered_properties = []
    for prop in properties:
        if "acreage" in prop and prop["acreage"] is not None:
            if min_acreage <= prop["acreage"] <= max_acreage:
                filtered_properties.append(prop)
    
    return filtered_properties

def detect_outliers_iqr(properties: List[Dict], price_key: str = "price_per_acre") -> Tuple[List[Dict], List[Dict]]:
    if not properties:
        return [], []
    
    values = [prop[price_key] for prop in properties if price_key in prop and prop[price_key] is not None]
    
    if len(values) < 4:
        logging.warning("Not enough values for IQR analysis, skipping outlier detection.")
        return properties, []
    
    q1 = np.percentile(values, 25)
    q3 = np.percentile(values, 75)
    iqr = q3 - q1
    
    lower_bound = q1 - 1.5 * iqr
    upper_bound = q3 + 1.5 * iqr
    
    valid_properties = []
    outlier_properties = []
    
    for prop in properties:
        if price_key in prop and prop[price_key] is not None:
            if lower_bound <= prop[price_key] <= upper_bound:
                valid_properties.append(prop)
            else:
                outlier_properties.append(prop)
    
    return valid_properties, outlier_properties

def calculate_property_value(target_acreage: float, comparable_properties: List[Dict]) -> Dict:
    if not comparable_properties:
        logging.warning("No comparable properties found for valuation.")
        return {
            "estimated_value_avg": None,
            "estimated_value_median": None,
            "price_per_acre_stats": None,
            "comparable_count": 0
        }
    
    price_per_acre_values = [
        prop["price_per_acre"] for prop in comparable_properties 
        if "price_per_acre" in prop and prop["price_per_acre"] is not None
    ]
    
    if not price_per_acre_values:
        logging.warning("No valid price per acre values found.")
        return {
            "estimated_value_avg": None,
            "estimated_value_median": None,
            "price_per_acre_stats": None,
            "comparable_count": len(comparable_properties)
        }
    
    avg_price_per_acre = statistics.mean(price_per_acre_values)
    
    if len(price_per_acre_values) == 1:
        median_price_per_acre = price_per_acre_values[0]
    else:
        median_price_per_acre = statistics.median(price_per_acre_values)
    
    avg_estimated_value = avg_price_per_acre * target_acreage
    median_estimated_value = median_price_per_acre * target_acreage
    
    price_per_acre_stats = {
        "min": min(price_per_acre_values),
        "max": max(price_per_acre_values),
        "avg": avg_price_per_acre,
        "median": median_price_per_acre,
        "std_dev": statistics.stdev(price_per_acre_values) if len(price_per_acre_values) > 1 else 0
    }
    
    return {
        "estimated_value_avg": avg_estimated_value,
        "estimated_value_median": median_estimated_value,
        "price_per_acre_stats": price_per_acre_stats,
        "comparable_count": len(comparable_properties)
    }

def find_comparable_properties(
    page: Optional[WebPage], 
    latitude: float, 
    longitude: float, 
    target_acreage: float
) -> Tuple[List[Dict], List[Dict], float, int, str]:
    search_radius = INITIAL_SEARCH_RADIUS_MILES
    all_homes = []
    potential_homes = []
    final_radius = search_radius
    search_url = ""

    while search_radius <= MAX_SEARCH_RADIUS_MILES:
        north, south, east, west = calculate_bounding_box(latitude, longitude, search_radius)
        homes_with_price, homes_without_price, current_search_url = fetch_zillow_data(page, north, south, east, west)
        
        if not search_url and current_search_url:
            search_url = current_search_url
        
        all_homes.extend(homes_with_price)
        potential_homes.extend(homes_without_price)

        all_homes = list({prop['address']: prop for prop in all_homes}.values())
        potential_homes = list({prop['address']: prop for prop in potential_homes}.values())

        filtered_homes = filter_properties_by_acreage(all_homes, target_acreage)
        if len(filtered_homes) >= MIN_COMPARABLE_PROPERTIES:
            final_radius = search_radius
            break

        search_radius += SEARCH_RADIUS_INCREMENT

    final_radius = search_radius if search_radius <= MAX_SEARCH_RADIUS_MILES else MAX_SEARCH_RADIUS_MILES
    filtered_homes = filter_properties_by_acreage(all_homes, target_acreage)
    potential_filtered = filter_properties_by_acreage(potential_homes, target_acreage)

    total_comparables_found = len(filtered_homes) + len(potential_filtered)

    if len(filtered_homes) < MIN_COMPARABLE_PROPERTIES:
        # Collect ZPIDs for parallel processing
        zpids_to_fetch = [prop['zpid'] for prop in potential_filtered if prop.get('zpid')]
        
        if zpids_to_fetch:
            logging.info(f"🚀 Fetching price history for {len(zpids_to_fetch)} properties in parallel...")
            
            # Fetch all price histories in parallel with more workers
            price_results = fetch_price_history_parallel(zpids_to_fetch, max_workers=15)  # 15 parallel workers for speed
            
            # Apply results to properties
            for prop in potential_filtered:
                if len(filtered_homes) >= MIN_COMPARABLE_PROPERTIES:
                    break
                if prop.get('zpid') and prop['zpid'] in price_results:
                    price = price_results[prop['zpid']]
                    if price:
                        prop['price'] = price
                        prop['price_text'] = f"${price:,.2f}"
                        prop['price_per_acre'] = price / prop['acreage'] if prop['acreage'] > 0 else None
                        filtered_homes.append(prop)
                        logging.info(f"Added property with price from history: {prop['address']}")
        else:
            logging.warning("No ZPIDs found for price history fetching")

    valid_properties, outlier_properties = detect_outliers_iqr(filtered_homes)

    if len(filtered_homes) < 4:
        valid_properties = filtered_homes
        outlier_properties = []

    logging.info(f"Final result: {len(valid_properties)} valid properties and {len(outlier_properties)} outliers")
    
    return valid_properties, outlier_properties, final_radius, total_comparables_found, search_url

def clean_apn(apn: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "", apn)

@app.get("/")
async def read_root():
    return {"message": "Welcome to the Property Valuation API"}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": time.time()}

@app.post("/valuate-property", response_model=ValuationResponse)
async def valuate_property(property_request: PropertyRequest):
    # Sequential processing - wait for turn
    async with REQUEST_LOCK:
        return await _process_valuation(property_request)


async def _process_valuation(property_request: PropertyRequest):
    """
    Process a property valuation request with enhanced validation and session handling
    """
    global last_request_time
    
    # Validate property request before processing
    if not validate_property_request(property_request):
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid property request format. Please check APN, county, and state format."
        )
    
    # Rate limiting to prevent PropStream blocking
    current_time = time.time()
    time_since_last_request = current_time - last_request_time
    if time_since_last_request < MIN_REQUEST_INTERVAL:
        wait_time = MIN_REQUEST_INTERVAL - time_since_last_request
        # Minimal random variation for maximum speed
        random_delay = random.uniform(0.3, 0.6)  # Optimized delay for speed and reliability
        total_wait = wait_time + random_delay
        logging.info(f"⏱️  Rate limiting: waiting {total_wait:.1f} seconds before next request")
        await asyncio.sleep(total_wait)
    
    last_request_time = time.time()
    
    cleaned_apn = clean_apn(property_request.apn)
    cache_key = generate_cache_key(property_request)
    
    # Check Redis cache for existing valuation
    cached_result = redis_client.get(cache_key)
    if cached_result:
        logging.info(f"Cache hit for {cache_key}")
        try:
            cached_data = json.loads(cached_result)
            if 'search_url' not in cached_data:
                cached_data['search_url'] = None
            return ValuationResponse(**cached_data)
        except Exception as e:
            logging.warning(f"Error parsing cached data: {str(e)}. Proceeding with fresh valuation.")
    
    # Acquire rate limit slot
    rate_limit_key = f"valuation:{cleaned_apn}"
    if not await api_rate_limiter.acquire(rate_limit_key):
        logging.warning(f"Rate limit exceeded for {rate_limit_key}")
        raise HTTPException(status_code=429, detail="Rate limit exceeded, please try again later")

    # STEP 1: Try to get property data from Regrid API first (much faster)
    logging.info("🚀 Attempting to fetch property data from Regrid API...")
    regrid_property_data = await asyncio.get_event_loop().run_in_executor(
        None, fetch_property_from_regrid, cleaned_apn, 
        property_request.county, property_request.state
    )
    
    if regrid_property_data:
        # Successfully got data from Regrid - extract property info
        logging.info("✅ Successfully fetched property data from Regrid API")
        target_property_info = await asyncio.get_event_loop().run_in_executor(
            None, extract_property_from_sidebar_regrid, regrid_property_data
        )
        
        if target_property_info:
            # We have all the data we need from Regrid - skip PropStream entirely
            logging.info("🎯 Using Regrid data - skipping PropStream scraping")
            target_acreage = target_property_info.get("lot_size_acres", 1.0)
            latitude = target_property_info.get("latitude", 0.0)
            longitude = target_property_info.get("longitude", 0.0)
            
            # Continue with Zillow API calls and calculations
            logging.info("🔍 Finding comparable properties using Zillow API...")
            # For Regrid flow, we don't need the page parameter, so we'll use a dummy page
            # The function will use Zillow API directly
            comparable_properties, _, final_radius, total_comparables_found, search_url = await asyncio.get_event_loop().run_in_executor(
                None, find_comparable_properties, None, latitude, longitude, target_acreage
            )
            
            if not comparable_properties:
                logging.warning("No comparable properties found")
                raise HTTPException(
                    status_code=404, 
                    detail="No comparable properties found for valuation"
                )
            
            # Calculate valuation
            logging.info("📊 Calculating property valuation...")
            valuation_results = calculate_property_value(target_acreage, comparable_properties)
            
            # Prepare response
            valuation_response = ValuationResponse(
                target_property=f"APN# {cleaned_apn}, {property_request.county}, {property_request.state}",
                target_acreage=target_acreage,
                target_latitude=latitude,
                target_longitude=longitude,
                search_radius_miles=final_radius,
                total_comparables_found=total_comparables_found,
                comparable_count=valuation_results['comparable_count'],
                estimated_value_avg=valuation_results['estimated_value_avg'],
                estimated_value_median=valuation_results['estimated_value_median'],
                price_per_acre_stats=ValuationStats(**valuation_results['price_per_acre_stats']) if valuation_results['price_per_acre_stats'] else None,
                comparable_properties=[ComparableProperty(**prop) for prop in comparable_properties],
                outlier_properties=[],  # No outliers in Regrid flow
                search_url=search_url,
                data_source="Regrid API + Zillow API",
                processing_time_seconds=0.0  # Will be calculated by the endpoint
            )
            
            # Cache the response in Redis
            cache_key = hashlib.md5(f"{cleaned_apn}_{property_request.county}_{property_request.state}".encode()).hexdigest()
            try:
                redis_client.setex(
                    cache_key,
                    settings.CACHE_EXPIRATION,
                    json.dumps(valuation_response.model_dump())
                )
                logging.info(f"Cached valuation response for {cache_key}")
            except Exception as e:
                logging.warning(f"Failed to cache valuation response: {str(e)}")
            
            return valuation_response
        else:
            logging.warning("⚠️ Failed to extract property info from Regrid data - falling back to PropStream")
    else:
        logging.warning("⚠️ Regrid API failed - falling back to PropStream scraping")

    # STEP 2: Fallback to PropStream scraping if Regrid failed
    logging.info("🔄 Falling back to PropStream scraping...")
    async with browser_pool.get_browser() as page:
        try:
            # Ensure logged in for this browser instance (once)
            await ensure_logged_in(page)
            logging.info("Login successful")
            
            # STEP 2: PropStream scraping (fallback)
            # Search for the target property
            await asyncio.get_event_loop().run_in_executor(
                None,
                search_property,
                page, ADDRESS_FORMAT, cleaned_apn, 
                property_request.county, property_request.state
            )
            logging.info("Property search completed")

            # Try to extract property information from sidebar first (faster, no navigation needed)
            logging.info("🔍 Attempting to extract property data from sidebar...")
            target_property_info = await asyncio.get_event_loop().run_in_executor(
                None, extract_property_from_sidebar, page, cleaned_apn
            )
            
            # If sidebar extraction failed, fall back to details page extraction
            if not target_property_info:
                logging.info("⚠️ Sidebar extraction failed, falling back to details page extraction...")
                target_property_info = await asyncio.get_event_loop().run_in_executor(
                    None, extract_property_info, page
                )
                
                if not target_property_info:
                    logging.error("Failed to extract target property info from both sidebar and details page")
                    raise HTTPException(status_code=500, detail="Failed to extract target property information")
                
                # Browser cleanup will be handled by the browser pool automatically
                logging.info("✅ Property extraction completed - browser will be cleaned up automatically")

            # Extract target acreage from property info
            target_acreage = target_property_info.get("acreage", 1.0)

            # Extract coordinates (try from page first, then geocoding fallback)
            coordinates = await asyncio.get_event_loop().run_in_executor(
                None, extract_coordinates, page.html
            )
            if not coordinates:
                geo_query = f"{property_request.county}, {property_request.state}"
                logging.info(f"Attempting geocoding for location: {geo_query}")
                coordinates = await asyncio.get_event_loop().run_in_executor(
                    None, geocode_location, geo_query
                )
            if not coordinates:
                logging.error("Property coordinates not found via page or geocoding; falling back to safe default.")
                # Safe default
                coordinates = (37.0902, -95.7129)

            latitude, longitude = coordinates

            # Fetch comparable properties from Zillow
            valid_properties, outlier_properties, final_radius, total_comparables_found, search_url = await asyncio.get_event_loop().run_in_executor(
                None, find_comparable_properties, page, latitude, longitude, target_acreage
            )

            # Calculate valuation based on comparable properties
            valuation_results = calculate_property_value(target_acreage, valid_properties)
            
            # Determine data source for logging
            data_source = "Regrid API" if target_property_info and target_property_info.get("data_source") == "regrid" else "PropStream"
            logging.info(f"📊 Valuation completed using {data_source} for property data")
            
            # Prepare response
            valuation_response = ValuationResponse(
                target_property=f"APN# {cleaned_apn}, {property_request.county}, {property_request.state}",
                target_acreage=target_acreage,
                target_latitude=latitude,
                target_longitude=longitude,
                search_radius_miles=final_radius,
                total_comparables_found=total_comparables_found,
                comparable_count=valuation_results['comparable_count'],
                estimated_value_avg=valuation_results['estimated_value_avg'],
                estimated_value_median=valuation_results['estimated_value_median'],
                price_per_acre_stats=ValuationStats(**valuation_results['price_per_acre_stats']) if valuation_results['price_per_acre_stats'] else None,
                comparable_properties=[ComparableProperty(**prop) for prop in valid_properties],
                outlier_properties=[ComparableProperty(**prop) for prop in outlier_properties],
                search_url=search_url,
                data_source=data_source,
                processing_time_seconds=0.0  # Will be calculated by the endpoint
            )

            # Cache the response in Redis
            try:
                redis_client.setex(
                    cache_key,
                    settings.CACHE_EXPIRATION,
                    json.dumps(valuation_response.model_dump())
                )
                logging.info(f"Cached valuation response for {cache_key}")
            except Exception as e:
                logging.warning(f"Failed to cache valuation response: {str(e)}")

            return valuation_response
        except HTTPException:
            raise
        except Exception as e:
            logging.error(f"Error during valuation process: {str(e)}")
            logging.error(traceback.format_exc())
            raise HTTPException(status_code=500, detail="Internal server error during valuation process")
        finally:
            # Release rate limit slot
            await api_rate_limiter.release(rate_limit_key)

def extract_property_from_sidebar(page: WebPage, apn: str) -> Optional[Dict]:
    """Extract property data directly from the right sidebar on search results page"""
    try:
        logging.info("🔍 Attempting to extract property data from right sidebar...")
        
        # Get the page HTML for parsing
        html = page.html
        soup = BeautifulSoup(html, "html.parser")
        
        # Look for the lot size in the sidebar based on the HTML structure you provided
        # The structure is: <span class="src-app-Search-Property-style__Nh01s__iconCounts">5,750<span>LOT</span></span>
        
        # Method 1: Look for the specific class structure for lot size
        lot_size_elements = soup.find_all("span", class_=lambda x: x and "iconCounts" in x)
        logging.info(f"Found {len(lot_size_elements)} iconCounts elements")
        
        for element in lot_size_elements:
            element_text = element.get_text().strip()
            logging.info(f"📋 Checking iconCounts element: '{element_text}'")
            
            # Check if this element contains lot size information
            if "LOT" in element_text.upper():
                # Extract the numeric value (e.g., "5,750" from "5,750LOT")
                lot_match = re.search(r'([\d,]+)', element_text)
                if lot_match:
                    try:
                        lot_size_str = lot_match.group(1).replace(',', '')
                        lot_size_sqft = float(lot_size_str)
                        
                        # Convert square feet to acres (1 acre = 43,560 sq ft)
                        lot_acres = lot_size_sqft / 43560
                        
                        logging.info(f"🏡 Found lot size in sidebar: {lot_size_sqft} sq ft ({lot_acres:.2f} acres)")
                        
                        # Also look for estimated value in the sidebar
                        estimated_value = None
                        
                        # Look for AVG.COMPS value in the sidebar
                        # Based on your HTML: <div class="src-app-Search-Property-style__Er7lN__value">$3,354,512</div>
                        value_elements = soup.find_all("div", class_=lambda x: x and "Er7lN__value" in x)
                        for value_elem in value_elements:
                            value_text = value_elem.get_text().strip()
                            if value_text.startswith('$'):
                                # Check if this is the AVG.COMPS value by looking at the parent structure
                                parent = value_elem.parent
                                if parent:
                                    parent_text = parent.get_text().strip()
                                    if "AVG.COMPS" in parent_text.upper():
                                        value_match = re.search(r'\$([\d,]+)', value_text)
                                        if value_match:
                                            try:
                                                estimated_value = float(value_match.group(1).replace(',', ''))
                                                logging.info(f"💰 Found AVG.COMPS value in sidebar: ${estimated_value:,.2f}")
                                                break
                                            except ValueError:
                                                continue
                        
                        return {
                            'acreage': lot_acres,
                            'lot_size_sqft': lot_size_sqft,
                            'estimated_value': estimated_value,
                            'source': 'search_sidebar'
                        }
                        
                    except ValueError as e:
                        logging.warning(f"Could not convert lot size value: {e}")
                        continue
        
        # Method 2: Look for lot size using more generic patterns
        logging.info("🔍 Trying generic lot size patterns in sidebar...")
        
        # Look for any text containing lot size information
        all_text_elements = soup.find_all(["div", "span", "p"])
        for element in all_text_elements:
            text = element.get_text().strip()
            
            # Look for patterns like "5,750 LOT" or "217,800 LOT"
            lot_patterns = [
                r'([\d,]+)\s*LOT',
                r'LOT\s*([\d,]+)',
                r'([\d,]+)\s*SqFt',
                r'([\d,]+)\s*Sq\.?\s*Ft\.?'
            ]
            
            for pattern in lot_patterns:
                lot_match = re.search(pattern, text, re.IGNORECASE)
                if lot_match:
                    try:
                        lot_size_str = lot_match.group(1).replace(',', '')
                        lot_size_sqft = float(lot_size_str)
                        
                        # Only accept reasonable lot sizes (between 1,000 and 1,000,000 sq ft)
                        if 1000 <= lot_size_sqft <= 1000000:
                            lot_acres = lot_size_sqft / 43560
                            logging.info(f"🏡 Found lot size with generic pattern: {lot_size_sqft} sq ft ({lot_acres:.2f} acres)")
                            
                            return {
                                'acreage': lot_acres,
                                'lot_size_sqft': lot_size_sqft,
                                'estimated_value': None,
                                'source': 'search_sidebar_generic'
                            }
                    except ValueError:
                        continue
        
        logging.warning("❌ Could not extract property data from sidebar")
        return None
        
    except Exception as e:
        logging.error(f"Error extracting property from sidebar: {e}")
        return None

def extract_property_from_search_results(page: WebPage, apn: str) -> Optional[Dict]:
    """Extract property data directly from search results table when Details buttons don't exist"""
    try:
        logging.info("🔍 Attempting to extract property data from search results table...")
        
        # Look for the property in the search results table
        table_rows = page.eles('xpath://table//tr')
        logging.info(f"Found {len(table_rows)} table rows")
        
        for row in table_rows:
            try:
                row_text = row.text
                if apn in row_text:
                    logging.info(f"✅ Found property row containing APN {apn}")
                    
                    # Extract basic property information from the row
                    cells = row.eles('xpath://td')
                    if len(cells) >= 3:  # Ensure we have enough columns
                        # Try to extract acreage from the row text
                        acreage_match = re.search(r'(\d+\.?\d*)\s*acres?', row_text, re.IGNORECASE)
                        acreage = float(acreage_match.group(1)) if acreage_match else None
                        
                        # Try to extract estimated value from the row text
                        value_match = re.search(r'\$(\d{1,3}(?:,\d{3})*)', row_text)
                        estimated_value = float(value_match.group(1).replace(',', '')) if value_match else None
                        
                        if acreage or estimated_value:
                            logging.info(f"✅ Extracted from search results: acreage={acreage}, value={estimated_value}")
                            return {
                                'acreage': acreage,
                                'estimated_value': estimated_value,
                                'source': 'search_results_table'
                            }
                        
            except Exception as e:
                logging.warning(f"Error processing table row: {e}")
                continue
                
        logging.warning("❌ Could not extract property data from search results table")
        return None
        
    except Exception as e:
        logging.error(f"Error extracting property from search results: {e}")
        return None

def validate_county_format(county: str, state: str) -> bool:
    """
    Validate that the county is in proper format (either 'County Name' or 'County Name County')
    Returns True if valid, False if invalid
    """
    county = county.strip()
    state_abbr = get_cached_state_abbreviation(state)
    
    # Log the validation
    logging.info(f"County validation: Input '{county}' for state '{state}' ({state_abbr})")
    
    # For now, we'll accept any county format and let PropStream handle it
    # In the future, you could add a list of valid counties per state
    return True

def format_county_name(county: str) -> str:
    """
    Format county name to ensure it ends with 'County' but doesn't duplicate
    """
    county = county.strip()
    
    # If it already ends with 'County', just ensure proper capitalization
    if county.lower().endswith('county'):
        # Remove any trailing 'County' and add it back with proper capitalization
        base_name = county[:-6].strip()  # Remove 'county' (6 characters)
        return f"{base_name.title()} County"
    else:
        # If it doesn't end with 'County', add it
        return f"{county.title()} County"

def validate_property_request(property_request: PropertyRequest) -> bool:
    """
    Validate the property request before processing
    Returns True if valid, False if invalid
    """
    # Validate county format
    if not validate_county_format(property_request.county, property_request.state):
        logging.error(f"❌ Invalid county format: {property_request.county}")
        return False
    
    # Validate APN format (basic check)
    if not property_request.apn or len(property_request.apn.strip()) < 3:
        logging.error(f"❌ Invalid APN format: {property_request.apn}")
        return False
    
    # Validate state
    state_abbr = get_cached_state_abbreviation(property_request.state)
    if not state_abbr:
        logging.error(f"❌ Invalid state: {property_request.state}")
        return False
    
    logging.info(f"✅ Property request validation passed: APN={property_request.apn}, County={property_request.county}, State={property_request.state}")
    return True

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

