import asyncio
from contextlib import asynccontextmanager
from typing import List, Optional
from DrissionPage import ChromiumOptions, WebPage
import logging

class BrowserPool:
    def __init__(self, pool_size: int = 3):
        self.pool_size = pool_size
        self.browsers: List[WebPage] = []
        self.available_browsers: asyncio.Queue = asyncio.Queue()
        self.lock = asyncio.Lock()
        
    async def initialize(self):
        """Initialize browser pool"""
        for i in range(self.pool_size):
            browser = self._create_browser()
            self.browsers.append(browser)
            await self.available_browsers.put(browser)
            
    def _create_browser(self) -> WebPage:
        co = ChromiumOptions()
        co.headless(True)
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-dev-shm-usage')
        co.set_argument('--disable-gpu')
        co.set_argument('--single-process')
        # Add unique port for each browser
        co.set_argument(f'--remote-debugging-port=0')  # Auto-assign port
        return WebPage(chromium_options=co)
    
    @asynccontextmanager
    async def get_browser(self):
        """Get browser from pool"""
        browser = await self.available_browsers.get()
        try:
            yield browser
        finally:
            await self.available_browsers.put(browser)
    
    async def cleanup(self):
        """Cleanup all browsers"""
        for browser in self.browsers:
            try:
                browser.close()
                browser.quit()
            except:
                pass

# Global browser pool
browser_pool = BrowserPool(pool_size=5)  # Increase from 3