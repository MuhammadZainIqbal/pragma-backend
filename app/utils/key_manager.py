import os
import time
import asyncio
import logging

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

# Parse globally ONCE at import
_raw_keys = os.environ.get("GEMINI_API_KEYS", "")
_GLOBAL_KEYS = []
if _raw_keys:
    for key in [k.strip() for k in _raw_keys.split(",") if k.strip()]:
        _GLOBAL_KEYS.append({
            "key": key,
            "last_used_at": 0.0,
            "is_active": True,
            "cooldown_until": 0.0
        })

class KeyManager:
    _lock = asyncio.Lock()

    @classmethod
    async def get_next_key(cls) -> str:
        # Wrap the entire selection, sort, and update block in the lock
        async with cls._lock:
            if not _GLOBAL_KEYS:
                raise ValueError("No API keys configured in GEMINI_API_KEYS.")

            now = time.time()
            active_keys = [k for k in _GLOBAL_KEYS if k["is_active"]]
            
            if not active_keys:
                raise ValueError("All configured API keys have been deactivated.")

            available_keys = [k for k in active_keys if now >= k["cooldown_until"]]
            
            if not available_keys:
                min_cooldown = min(k["cooldown_until"] for k in active_keys)
                wait_time = max(0.1, min_cooldown - time.time())
                logger.warning(f"All keys on cooldown. Waiting {wait_time:.2f}s...")
                need_wait = True
            else:
                available_keys.sort(key=lambda k: k["last_used_at"])
                selected = available_keys[0]
                
                # Agent 1 fully updates last_used_at before lock is released
                selected["last_used_at"] = time.time()
                
                api_key = selected["key"]
                print(f"[KEY MANAGER] Handing out key: ...{api_key[-6:]}")
                return api_key
                
        # If we didn't return, it means we need to wait (sleep outside lock)
        await asyncio.sleep(wait_time)
        return await cls.get_next_key()

    @classmethod
    async def report_rate_limit(cls, key: str):
        async with cls._lock:
            for k in _GLOBAL_KEYS:
                if k["key"] == key:
                    logger.warning(f"Gemini 429 Rate Limit encountered for key ...{key[-6:]}. Cooling down for 60s.")
                    k["cooldown_until"] = time.time() + 60.0
                    break
