import asyncio
from contextlib import asynccontextmanager
from typing import Optional
from DrissionPage import ChromiumOptions, WebPage
import logging
import random
import time

class BrowserPool:
    def __init__(self):
        self.persistent_browser: Optional[WebPage] = None
        self.session_valid = False
        self.session_created_at = None
        self.max_session_age = 3600  # 1 hour max session age
        self.lock = asyncio.Lock()
        
    async def initialize(self):
        """Initialize single persistent browser"""
        try:
            self.persistent_browser = self._create_browser()
            self.session_valid = False  # Will be set to True after successful login
            self.session_created_at = None
            logging.info("🆕 Initialized single persistent browser instance")
        except Exception as e:
            logging.error(f"Failed to initialize browser: {e}")
            raise
                
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
        co.set_user_agent(random.choice(user_agents))
        
        # Additional randomization
        co.set_argument(f'--window-size={random.randint(1200, 1920)},{random.randint(800, 1080)}')
        
        # Create browser with timeout handling
        try:
            browser = WebPage(chromium_options=co, timeout=30)
            
            # Set timeouts
            browser.set.timeouts(base=20, page_load=30, script=20)
            
            # Apply stealth measures
            try:
                stealth_js = """
                function(){
                    // Only modify if not already modified
                    if (navigator.webdriver !== undefined) {
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined,
                        });
                    }
                    
                    // Remove automation indicators safely
                    if (window.chrome && window.chrome.runtime) {
                        try {
                            delete window.chrome.runtime.onConnect;
                        } catch(e) {}
                    }
                    
                    // Override plugins safely
                    if (navigator.plugins.length === 0) {
                        Object.defineProperty(navigator, 'plugins', {
                            get: () => [1, 2, 3, 4, 5],
                        });
                    }
                    
                    // Override languages
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['en-US', 'en'],
                    });
                }
                """
                browser.run_js(stealth_js)
            except Exception as e:
                logging.warning(f"Could not execute stealth JS: {e}")
        except Exception as e:
            logging.error(f"Failed to create browser: {e}")
            raise
        
        return browser
    
    @asynccontextmanager
    async def get_browser(self):
        """Get the single persistent browser instance"""
        async with self.lock:
            # Check if session is too old
            if self.session_created_at and time.time() - self.session_created_at > self.max_session_age:
                logging.info("🕐 Session is too old, invalidating...")
                self.invalidate_session()
            
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
                self.session_created_at = None
                logging.info("🆕 Created new persistent browser instance")
            else:
                logging.info("♻️  Using existing persistent browser session")
        
        try:
            yield self.persistent_browser
        except Exception as e:
            # If there's an error, mark session as invalid
            logging.warning(f"Error with persistent browser, will recreate: {e}")
            self.session_valid = False
            self.session_created_at = None
            raise
    
    def mark_session_valid(self):
        """Mark the current session as valid (called after successful login)"""
        self.session_valid = True
        self.session_created_at = time.time()
        logging.info("✅ Browser session marked as valid")
    
    def invalidate_session(self):
        """Invalidate the current session (called when login fails)"""
        self.session_valid = False
        self.session_created_at = None
        logging.info("❌ Browser session marked as invalid")
    
    async def cleanup(self):
        """Cleanup browser"""
        if self.persistent_browser:
            try:
                self.persistent_browser.close()
                self.persistent_browser.quit()
            except Exception as e:
                logging.warning(f"Error closing persistent browser: {e}")

# Global browser pool - single instance for sequential processing
browser_pool = BrowserPool()
