import json
import hashlib
import os
import ssl
import urllib.request
import urllib.error

from config import OPENAI_API_KEY

API_URL = "https://api.openai.com/v1/responses"

def _hash_payload(payload):
    s = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(s).hexdigest()

def generate_trivia(sport: str, difficulty: str, mode: str):
    requested_type = "mcq" if mode == "MCQ" else "free" if mode == "Free" else "mcq"

    instructions = (
        "Return ONLY valid JSON. No markdown, no extra text.\n"
        "You write Florida Gators (University of Florida) sports triviaugglerQn"
        "Only ask about Florida Gators sports history, players, coaches, titles, iconic games.\n"
        "JSON schema:\n"
        "MCQ: {sport,difficulty,type:'mcq',question,choices:[4],answer_index:0-3,explanation,tags:[...],confidence:0-1}\n"
        "FREE:{sport,difficulty,type:'free',question,answers:[...],explanation,tags:[...],confidence:0-1}\n"
    )

    payload = {
        "model": "gpt-4o-mini",
        "input": json.dumps({
            "sport": sport,
            "difficulty": difficulty,
            "requested_type": requested_type
        }),
        "instructions": instructions
    }

    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    ctx = ssl.create_default_context()

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print("OPENAI_HTTP_ERROR:", e.code, body)
        raise RuntimeError(f"OpenAI HTTPError {e.code}: {body}")
    except Exception as e:
        print("OPENAI_RAW_CALL_FAILED:", repr(e))
        raise RuntimeError(f"OpenAI raw connection failed: {e!r}")

    # Parse Responses API output -> output_text
    obj = json.loads(raw)
    output_text = obj.get("output_text", "")
    if not output_text:
        # fallback: sometimes output is structured
        try:
            chunks = []
            for item in obj.get("output", []):
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        chunks.append(c.get("text", ""))
            output_text = "\n".join(chunks).strip()
        except Exception:
            output_text = ""

    trivia = json.loads(output_text)

    if "tags" not in trivia or not isinstance(trivia["tags"], list):
        trivia["tags"] = []

    h = _hash_payload(trivia)
    return trivia, h


