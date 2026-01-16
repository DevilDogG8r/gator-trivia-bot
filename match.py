import re
from rapidfuzz import fuzz

def normalize(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s

def free_is_correct(user_text: str, acceptable_answers: list[str], threshold: int = 88) -> bool:
    ut = normalize(user_text)
    best = 0
    for ans in acceptable_answers:
        score = fuzz.token_set_ratio(ut, normalize(ans))
        best = max(best, score)
    return best >= threshold
