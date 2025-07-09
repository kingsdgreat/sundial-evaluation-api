import asyncio
import time
from typing import Dict, Optional

class RateLimiter:
    def __init__(self, calls_per_minute: int = 30):
        self.calls_per_minute = calls_per_minute
        self.calls: Dict[str, list] = {}
        
    async def acquire(self, key: str = "default") -> bool:
        now = time.time()
        if key not in self.calls:
            self.calls[key] = []
            
        # Remove old calls
        self.calls[key] = [call_time for call_time in self.calls[key] 
                          if now - call_time < 60]
        
        if len(self.calls[key]) >= self.calls_per_minute:
            return False
            
        self.calls[key].append(now)
        return True

# Global rate limiter for API calls
api_rate_limiter = RateLimiter(calls_per_minute=30)