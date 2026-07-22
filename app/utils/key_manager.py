import os
import re
import time
import asyncio
import random
import logging

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

logger = logging.getLogger(__name__)

# Ensure only 1 LLM request hits Google's API at any given millisecond
llm_semaphore = asyncio.Semaphore(1)

def get_all_keys():
    keys = []
    raw_keys = os.environ.get("GEMINI_API_KEYS", "")
    if raw_keys:
        for k in raw_keys.split(","):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)

    for key, value in os.environ.items():
        if re.match(r"^GEMINI_API_KEY_\d+$", key):
            v = value.strip()
            if v and v not in keys:
                keys.append(v)
    return keys

class KeyManager:
    @classmethod
    async def execute_with_key_rotation(cls, system_prompt, prompt_text, response_schema):
        keys = get_all_keys()
        if not keys:
            raise Exception("No Gemini API keys found in environment.")
            
        max_attempts = 5
        
        for attempt in range(1, max_attempts + 1):
            key = keys[(attempt - 1) % len(keys)]
            suffix = f"...{key[-6:]}" if len(key) >= 6 else key
            
            try:
                async with llm_semaphore:
                    print(f"🔒 [PRAGMA KEY MANAGER] Lock acquired for key [{suffix}]. Executing LLM request...")
                    client = genai.Client(api_key=key)
                    response = await asyncio.to_thread(
                        client.models.generate_content,
                        model="gemini-3.5-flash",
                        contents=prompt_text,
                        config=types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            response_mime_type="application/json",
                            response_schema=response_schema,
                        ),
                    )
                    print(f"✅ [PRAGMA KEY MANAGER] Key [{suffix}] succeeded on attempt {attempt}.")
                    parsed_output = response_schema.model_validate_json(response.text)
                    return parsed_output, getattr(response, "usage_metadata", None)
                    
            except Exception as e:
                backoff = (2 ** attempt) + random.uniform(1.0, 2.0)
                print(f"⚠️ [PRAGMA KEY MANAGER] Key [{suffix}] error ({e}). Retrying with next key in {backoff:.2f}s (Attempt {attempt}/{max_attempts})...")
                await asyncio.sleep(backoff)
                
        raise Exception("All API keys and retries exhausted.")
