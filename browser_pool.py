import asyncio
from contextlib import asynccontextmanager
from typing import List, Optional
from DrissionPage import ChromiumOptions, WebPage
import logging
import os

class BrowserPool:
    def __init__(self, pool_size: int = 3):  # Increased to 3 per instance
        self.pool_size = pool_size
        self.browsers: List[WebPage] = []
        self.available_browsers: asyncio.Queue = asyncio.Queue()
        self.lock = asyncio.Lock()
        
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
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-dev-shm-usage')
        co.set_argument('--disable-gpu')
        co.set_argument('--single-process')
        co.set_argument('--disable-extensions')
        co.set_argument('--disable-plugins')
        co.set_argument('--disable-images')  # Faster loading
        # Removed: co.set_argument('--disable-javascript')  # JS required for PropStream
        co.set_argument('--remote-debugging-port=0')  # Auto-assign port
        
        # Memory optimization
        co.set_argument('--memory-pressure-off')
        co.set_argument('--max_old_space_size=512')
        
        return WebPage(chromium_options=co)
    
    @asynccontextmanager
    async def get_browser(self):
        """Get fresh browser instance for each request to prevent session issues"""
        # Create a fresh browser instance instead of reusing from pool
        browser = self._create_browser()
        logging.info("🆕 Created fresh browser instance for request")
        try:
            yield browser
        finally:
            # Clean up the browser after use
            try:
                browser.close()
                browser.quit()
                logging.info("🧹 Cleaned up browser instance after request")
            except Exception as e:
                logging.warning(f"Error cleaning up browser: {e}")
    
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
