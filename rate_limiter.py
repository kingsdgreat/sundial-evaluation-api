import asyncio
import time
import redis
from typing import Dict, Optional
import logging

class RateLimiter:
    def __init__(self, calls_per_minute: int = 30, redis_client=None):
        self.calls_per_minute = calls_per_minute
        self.redis_client = redis_client
        self.local_calls: Dict[str, list] = {}
        
    async def acquire(self, key: str = "default") -> bool:
        """Acquire rate limit slot - uses Redis for distributed limiting if available"""
        now = time.time()
        
        if self.redis_client:
            try:
                # Use Redis for distributed rate limiting across instances
                pipe = self.redis_client.pipeline()
                rate_key = f"rate_limit:{key}"
                
                # Remove old entries
                pipe.zremrangebyscore(rate_key, 0, now - 60)
                # Count current entries
                pipe.zcard(rate_key)
                # Add current request
                pipe.zadd(rate_key, {str(now): now})
                # Set expiry
                pipe.expire(rate_key, 60)
                
                results = pipe.execute()
                current_count = results[1]
                
                if current_count >= self.calls_per_minute:
                    # Remove the request we just added since we're over limit
                    self.redis_client.zrem(rate_key, str(now))
                    return False
                    
                return True
                
            except Exception as e:
                logging.warning(f"Redis rate limiting failed, falling back to local: {e}")
                # Fall back to local rate limiting
        
        # Local rate limiting fallback
        if key not in self.local_calls:
            self.local_calls[key] = []
            
        # Remove old calls
        self.local_calls[key] = [call_time for call_time in self.local_calls[key] 
                                if now - call_time < 60]
        
        if len(self.local_calls[key]) >= self.calls_per_minute:
            return False
            
        self.local_calls[key].append(now)
        return True

    async def release(self, key: str = "default") -> None:
        """No-op release for interface compatibility."""
        return None

# Global rate limiter - will be initialized with Redis client in main.py
api_rate_limiter = RateLimiter(calls_per_minute=30)
