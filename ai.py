import json
import hashlib
import os
import ssl
import urllib.request
import urllib.error

from config import OPENAI_API_KEY

API_URL = "https://api.openai.com/v1/responses"


def _hash_payload(payload) -> str:
    s = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(s).hexdigest()


def _extract_output_text(obj: dict) -> str:
    output_text = (obj.get("output_text") or "").strip()
    if output_text:
        return output_text
    chunks = []
    for item in obj.get("output", []) or []:
        for c in item.get("content", []) or []:
            if c.get("type") == "output_text":
                chunks.append(c.get("text", ""))
    return "\n".join(chunks).strip()


def _responses_api_call(payload: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenAI HTTPError {e.code}: {body}")
    except Exception as e:
        raise RuntimeError(f"OpenAI request failed: {e!r}")


def generate_trivia(sport: str, difficulty: str, mode: str = "MCQ", topic: str = ""):
    requested_type = "mcq" if (mode or "").upper() == "MCQ" else "free"

    instructions = (
        "Return ONLY valid JSON. No markdown, no extra text.\n"
        "You write Florida Gators / University of Florida athletics trivia across time.\n"
        "You MUST include all UF varsity sports plus recruiting trivia and UF/Florida athletes in the Olympics.\n"
        "Make questions CHALLENGING by default (avoid 101-level facts like conference/maskot/colors).\n"
        "Prefer verifiable deep-cut facts: specific years, opponents, awards, records, postseason results, recruiting classes, Olympic medals/events.\n"
        "JSON schema:\n"
        "MCQ: {sport,difficulty,type:'mcq',question,choices:[4],answer_index:0-3,explanation,tags:[...],confidence:0-1}\n"
        "Rules for MCQ:\n"
        "- choices must be exactly 4\n"
        "- one correct answer\n"
        "- no 'All of the above'/'None of the above'\n"
        "- avoid unverifiable rumors\n"
    )

    payload = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "input": json.dumps(
            {"sport": sport, "difficulty": difficulty, "requested_type": requested_type, "topic": topic},
            ensure_ascii=False,
        ),
        "instructions": instructions,
    }

    obj = _responses_api_call(payload)
    output_text = _extract_output_text(obj)
    trivia = json.loads(output_text)

    if "tags" not in trivia or not isinstance(trivia["tags"], list):
        trivia["tags"] = []

    h = _hash_payload(trivia)
    return trivia, h


def verify_trivia_mcq(trivia: dict):
    """
    LLM-only verification / correction pass.

    Returns:
      (verified_trivia_dict, verdict_str)

    verdict_str: "PASS" | "FIXED" | "FAIL"
    """
    verifier_instructions = (
        "You are a strict fact-checking editor for Florida Gators / UF athletics trivia.\n"
        "Goal: reduce hallucinations and unverifiable claims.\n"
        "You must either:\n"
        "  - PASS: confirm question is specific, plausible, and internally consistent, OR\n"
        "  - FIX: output a corrected question/choices/answer_index that is more verifiable, OR\n"
        "  - FAIL: reject if you are not confident it can be made verifiable.\n"
        "\n"
        "Return ONLY JSON with this schema:\n"
        "{verdict:'PASS'|'FIX'|'FAIL', trivia:{sport,difficulty,type:'mcq',question,choices:[4],answer_index:0-3,explanation,tags:[...],confidence:0-1}, notes:''}\n"
        "\n"
        "Rules:\n"
        "- choices must remain exactly 4\n"
        "- answer_index must match the correct choice\n"
        "- remove or rewrite any uncertain / rumor-like claims\n"
        "- if unsure about a key fact (year, opponent, medal), FAIL rather than guessing\n"
        "- keep it challenging, not basic\n"
    )

    payload = {
        "model": os.getenv("OPENAI_VERIFY_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini")),
        "input": json.dumps(trivia, ensure_ascii=False),
        "instructions": verifier_instructions,
    }

    obj = _responses_api_call(payload)
    output_text = _extract_output_text(obj)
    out = json.loads(output_text)

    verdict = (out.get("verdict") or "").upper()
    fixed = out.get("trivia")

    if verdict not in {"PASS", "FIX", "FAIL"}:
        return None, "FAIL"
    if verdict == "FAIL":
        return None, "FAIL"
    if not isinstance(fixed, dict):
        return None, "FAIL"

    # normalize
    fixed["type"] = "mcq"
    if "tags" not in fixed or not isinstance(fixed["tags"], list):
        fixed["tags"] = []
    return fixed, ("FIXED" if verdict == "FIX" else "PASS")
