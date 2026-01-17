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


def _responses_api_call(payload: dict, timeout: int = 30) -> dict:
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
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenAI HTTPError {e.code}: {body}")
    except Exception as e:
        raise RuntimeError(f"OpenAI request failed: {e!r}")


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


def generate_trivia(
    sport: str,
    difficulty: str,
    mode: str = "MCQ",
    topic: str | None = None,
):
    """
    Returns (trivia_dict, trivia_hash)

    trivia_dict MCQ schema:
      {sport,difficulty,type:'mcq',question,choices:[4],answer_index,explanation,tags:[...],confidence:0-1}
    """

    requested_type = "mcq" if (mode or "").lower() != "free" else "free"

    scope = (
        "You write University of Florida (Florida Gators) athletics trivia.\n"
        "You MUST cover the full UF athletics universe across time: football, men's/women's basketball, baseball, softball, gymnastics, track & field, swimming & diving, soccer, lacrosse, volleyball, tennis, golf, cross country, rowing, and other UF varsity sports.\n"
        "ALSO include: (A) recruiting trivia (HS commits, signing classes, positions, star ratings, flips, coaching staff, eras), and (B) Florida/UF athletes in the Olympics (UF alums and Florida athletes associated with UF, medals, events, years).\n"
        "Keep questions specific and answerable. Avoid obscure unverifiable rumors. Prefer facts a fan could verify (names, years, awards, titles, venues, records, coaches, notable games/performances).\n"
    )

    formatting = (
        "Return ONLY valid JSON. No markdown, no extra text.\n"
        "Use this JSON schema exactly:\n"
        "MCQ: {sport,difficulty,type:'mcq',question,choices:[4],answer_index:0-3,explanation,tags:[...],confidence:0-1}\n"
        "FREE:{sport,difficulty,type:'free',question,answers:[...],explanation,tags:[...],confidence:0-1}\n"
        "Rules for MCQ:\n"
        "- choices must be exactly 4 strings\n"
        "- one and only one correct answer\n"
        "- no duplicate or near-duplicate choices\n"
        "- no 'All of the above' / 'None of the above'\n"
        "- answer_index must match the correct choice\n"
    )

    topic_hint = f"Focus topic: {topic}.\n" if topic else ""

    payload = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "input": json.dumps(
            {
                "sport": sport,
                "difficulty": difficulty,
                "requested_type": requested_type,
                "topic": topic or "",
            },
            ensure_ascii=False,
        ),
        "instructions": scope + topic_hint + formatting,
    }

    obj = _responses_api_call(payload)
    output_text = _extract_output_text(obj)
    trivia = json.loads(output_text)

    if "tags" not in trivia or not isinstance(trivia["tags"], list):
        trivia["tags"] = []

    h = _hash_payload(trivia)
    return trivia, h

