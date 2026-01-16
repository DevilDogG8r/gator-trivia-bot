import os

# Load .env locally if available (Railway ignores this)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

DISCORD_TOKEN = (os.getenv("DISCORD_TOKEN") or "").strip()
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()

if not DISCORD_TOKEN:
    raise RuntimeError(
        "Missing DISCORD_TOKEN (set it in Railway Variables or local .env)"
    )

if not OPENAI_API_KEY:
    raise RuntimeError(
        "Missing OPENAI_API_KEY (set it in Railway Variables or local .env)"
    )
