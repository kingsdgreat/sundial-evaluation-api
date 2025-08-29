import asyncio
from contextlib import asynccontextmanager
from typing import List, Optional
from DrissionPage import ChromiumOptions, WebPage
import logging
import os
import random

class BrowserPool:
    def __init__(self, pool_size: int = 3):  # Increased to 3 per instance
        self.pool_size = pool_size
        self.browsers: List[WebPage] = []
        self.available_browsers: asyncio.Queue = asyncio.Queue()
        self.lock = asyncio.Lock()
        self.persistent_browser: Optional[WebPage] = None
        self.session_valid = False
        
    async def initialize(self):
        """Initialize browser pool"""
        for i in range(self.pool_size):
            try:
                browser = self._create_browser()
                self.browsers.append(browser)
                await self.available_browsers.put(browser)
                logging.info(f"Browser {i+1}/{self.pool_size} initialized successfully")
            except Exception as e:
                logging.error(f"Failed to initialize browser {i+1}: {e}")
                
    def _create_browser(self) -> WebPage:
        co = ChromiumOptions()
        co.headless(True)
        
        # Anti-detection measures
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-dev-shm-usage')
        co.set_argument('--disable-gpu')
        co.set_argument('--single-process')
        co.set_argument('--disable-extensions')
        co.set_argument('--disable-plugins')
        co.set_argument('--disable-images')  # Faster loading
        co.set_argument('--remote-debugging-port=0')  # Auto-assign port
        
        # Stealth mode - make browser look more human
        co.set_argument('--disable-blink-features=AutomationControlled')
        co.set_argument('--disable-web-security')
        co.set_argument('--disable-features=VizDisplayCompositor')
        co.set_argument('--no-first-run')
        co.set_argument('--no-default-browser-check')
        
        # Randomized user agent from common browsers
        user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ]
        selected_ua = random.choice(user_agents)
        co.set_user_agent(selected_ua)
        
        # Randomized viewport size
        widths = [1366, 1440, 1920, 1280, 1536]
        heights = [768, 900, 1080, 720, 864]
        width = random.choice(widths)
        height = random.choice(heights)
        co.set_argument(f'--window-size={width},{height}')
        
        # Memory optimization
        co.set_argument('--memory-pressure-off')
        co.set_argument('--max_old_space_size=512')
        
        browser = WebPage(chromium_options=co)
        
        # Additional stealth JavaScript execution
        try:
            browser.run_js_loaded('''
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                });
                
                // Remove automation indicators
                delete window.chrome.runtime.onConnect;
                
                // Override plugins
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5],
                });
                
                // Override languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                });
            ''')
        except Exception as e:
            logging.warning(f"Could not execute stealth JS: {e}")
        
        return browser
    
    @asynccontextmanager
    async def get_browser(self):
        """Get persistent browser instance to maintain session"""
        async with self.lock:
            # Check if we have a valid persistent browser
            if self.persistent_browser is None or not self.session_valid:
                # Create new persistent browser
                if self.persistent_browser:
                    try:
                        self.persistent_browser.close()
                        self.persistent_browser.quit()
                        logging.info("🧹 Cleaned up old persistent browser")
                    except:
                        pass
                
                self.persistent_browser = self._create_browser()
                self.session_valid = False  # Will be set to True after successful login
                logging.info("🆕 Created new persistent browser instance")
            else:
                logging.info("♻️  Reusing existing persistent browser session")
        
        try:
            yield self.persistent_browser
        except Exception as e:
            # If there's an error, mark session as invalid
            logging.warning(f"Error with persistent browser, will recreate: {e}")
            self.session_valid = False
            raise
    
    def mark_session_valid(self):
        """Mark the current session as valid (called after successful login)"""
        self.session_valid = True
        logging.info("✅ Browser session marked as valid")
    
    def invalidate_session(self):
        """Invalidate the current session (called when login fails)"""
        self.session_valid = False
        logging.info("❌ Browser session marked as invalid")
    
    async def cleanup(self):
        """Cleanup all browsers"""
        for browser in self.browsers:
            try:
                browser.close()
                browser.quit()
            except Exception as e:
                logging.warning(f"Error closing browser: {e}")

# Global browser pool - smaller per instance since we have multiple instances
browser_pool = BrowserPool(pool_size=3)
