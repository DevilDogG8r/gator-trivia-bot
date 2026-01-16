import hashlib
import json
import random
from typing import Dict, Any, Tuple, List

from openai import OpenAI
from config import OPENAI_API_KEY

# Create OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# If one model is unavailable on your account, it will try the next
MODEL_FALLBACKS: List[str] = [
    "gpt-4o-mini",
    "gpt-4.1-mini",
    "gpt-4o",
]

def _hash_payload(payload: Dict[str, Any]) -> str:
    s = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(s).hexdigest()

def _validate_payload(p: Dict[str, Any]) -> None:
    # Required common fields
    for k in ["sport", "difficulty", "type", "question", "explanation", "confidence"]:
        if k not in p:
            raise ValueError(f"AI JSON missing required field: {k}")

    if p["type"] not in ["mcq", "free"]:
        raise ValueError("AI JSON type must be 'mcq' or 'free'")

    conf = float(p.get("confidence", 0))
    if conf < 0 or conf > 1:
        raise ValueError("AI JSON confidence must be between 0.0 and 1.0")

    if p["type"] == "mcq":
        choices = p.get("choices", [])
        if not isinstance(choices, list) or len(choices) != 4:
            raise ValueError("MCQ must include choices[4]")
        ai = p.get("answer_index", None)
        if ai is None:
            raise ValueError("MCQ must include answer_index")
        ai = int(ai)
        if ai < 0 or ai > 3:
            raise ValueError("answer_index must be 0..3")
        # Ensure unique choices
        norm = [str(c).strip().lower() for c in choices]
        if len(set(norm)) != 4:
            raise ValueError("MCQ choices must be 4 distinct options")

    if p["type"] == "free":
        answers = p.get("answers", [])
        if not isinstance(answers, list) or len(answers) < 1:
            raise ValueError("FREE must include answers[] with at least 1 acceptable answer")

def generate_trivia(sport: str, difficulty: str, mode: str) -> Tuple[Dict[str, Any], str]:
    """
    Returns: (payload_dict, sha256_hash)
    payload format:
      MCQ:  {sport,difficulty,type:'mcq',question,choices[4],answer_index,explanation,tags[],confidence}
      FREE: {sport,difficulty,type:'free',question,answers[],explanation,tags[],confidence}
    """
    # Decide requested type based on mode
    if mode == "MCQ":
        requested_type = "mcq"
    elif mode == "Free":
        requested_type = "free"
    else:
        requested_type = random.choice(["mcq", "free"])  # Mixed

    instructions = (
        "You are a Florida Gators (University of Florida) sports trivia writer.\n"
        "Return ONLY valid JSON. No markdown. No extra text.\n"
        "Only ask about Florida Gators sports history, players, coaches, titles, iconic games.\n"
        "Avoid disputed/ambiguous stats. If a stat is used, include the year/season.\n"
        "Keep questions clear and answerable.\n\n"
        "JSON schema:\n"
        "If type='mcq': {\n"
        "  sport, difficulty, type:'mcq', question,\n"
        "  choices:[4 strings], answer_index:0-3,\n"
        "  explanation:string, tags:[strings], confidence:0.0-1.0\n"
        "}\n"
        "If type='free': {\n"
        "  sport, difficulty, type:'free', question,\n"
        "  answers:[1-5 strings], explanation:string,\n"
        "  tags:[strings], confidence:0.0-1.0\n"
        "}\n"
    )

    user_input = {
        "sport": sport,
        "difficulty": difficulty,
        "requested_type": requested_type,
        "notes": "If sport='All', choose any major UF sport (football, basketball, baseball, gymnastics, etc.)."
    }

    last_err = None

    for model in MODEL_FALLBACKS:
        try:
            resp = client.responses.create(
                model=model,
                instructions=instructions,
                input=json.dumps(user_input),
            )

            raw = (resp.output_text or "").strip()
            payload = json.loads(raw)

            # Normalize fields
            if "tags" not in payload or not isinstance(payload["tags"], list):
                payload["tags"] = []

            _validate_payload(payload)

            h = _hash_payload(payload)
            return payload, h

        except Exception as e:
            # This prints into Railway logs so we can see the REAL error
            print("OPENAI_CALL_FAILED:", model, repr(e))
            last_err = e
            continue

    # If we tried all models and none worked:
    raise RuntimeError(f"OpenAI trivia generation failed for all models. Last error: {last_err!r}")

