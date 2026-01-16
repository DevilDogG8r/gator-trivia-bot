import hashlib
import json
import random
from typing import Dict, Any, Tuple

from openai import OpenAI
from config import OPENAI_API_KEY

client = OpenAI(api_key=OPENAI_API_KEY)

def _hash_payload(p: Dict[str, Any]) -> str:
    s = json.dumps(p, sort_keys=True).encode("utf-8")
    return hashlib.sha256(s).hexdigest()

def generate_trivia(sport: str, difficulty: str, mode: str) -> Tuple[Dict[str, Any], str]:
    qtype = "mcq"
    if mode == "Free":
        qtype = "free"
    elif mode == "Mixed":
        qtype = random.choice(["mcq", "free"])

    instructions = (
        "You are a Florida Gators sports trivia writer.\n"
        "Return ONLY valid JSON. No markdown, no extra text.\n"
        "Florida Gators (University of Florida) sports only.\n"
        "Avoid ambiguous questions. Avoid disputed stats.\n"
        "Prefer facts with a year/season when relevant.\n"
        "MCQ: exactly 4 distinct choices; only 1 correct.\n"
        "FREE: provide 1-5 acceptable answers (aliases allowed).\n"
        "Include explanation and confidence 0.0-1.0.\n"
    )

    input_text = {
        "sport": sport,
        "difficulty": difficulty,
        "requested_type": qtype,
        "output_schema": (
            "If type=mcq: {sport,difficulty,type:'mcq',question,choices[4],answer_index,explanation,tags[],confidence}\n"
            "If type=free: {sport,difficulty,type:'free',question,answers[],explanation,tags[],confidence}"
        ),
    }

    resp = client.responses.create(
        model="gpt-4o-mini",
        instructions=instructions,
        input=json.dumps(input_text),
    )

    raw = resp.output_text.strip()
    payload = json.loads(raw)
    h = _hash_payload(payload)
    return payload, h
