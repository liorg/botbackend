# services/regex_ai/compiler.py
# JSON תנאים → regex. אותו קוד שעבר את ה-POC, בלי שינוי לוגי.
# הפלט עובד גם ב-Python re וגם ב-.NET Regex (ה-Worker).
import re
import unicodedata

ANY = r"[\s\S]*"        # '.' לא חוצה שורות — הודעות WhatsApp יכולות להיות רב-שורתיות
DIGIT = "[0-9]"         # \d תופס גם ספרות ערביות-הודיות
LETTER = "[A-Za-zא-תء-يА-яЁё]"
WORD = r"\S+"           # מילה = רצף בלי רווחים

FIXED   = ("starts_with", "contains", "contains_after", "literal")   # ערך קבוע
COUNTED = ("digits", "letters")                                     # רכיב עם כמות

END_TRAIL = r"[\s.!?…]*"   # סוף הודעה סובל רווח/פיסוק אחרון: "ביי!" "ביי." "ביי "


def _end(value, scope):
    """סוף ההודעה ($) או סוף משפט — אחרי הערך בא סימן פיסוק סוגר, שורה חדשה או סוף."""
    v = escape_regex(value)
    if scope == "sentence":
        return v + r"(?=\s*(?:[.!?…]|\n|$))"
    return v + END_TRAIL + "$"

TYPES = ["starts_with", "ends_with", "contains", "contains_after",
         "exact", "digits", "letters", "one_of", "not_contains",
         "space", "literal", "words"]

SCHEMA = {
    "type": "object",
    "properties": {
        "conditions": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": TYPES},
                    "value": {"anyOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                        {"type": "null"},
                    ]},
                    "count": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                },
                "required": ["type", "value", "count"],
                "additionalProperties": False,
            },
        },
        "whole_input": {"type": "boolean"},
    },
    "required": ["conditions", "whole_input"],
    "additionalProperties": False,
}

SEPARATORS = {"hyphen": "-", "colon": ":", "slash": "/"}


def escape_regex(value):
    if value is None:
        return ""
    return re.sub(r'([.*+?^${}()|[\]\\])', r'\\\1', value)


def _quant(count, kind):
    if count is None:
        return "+"
    if not isinstance(count, int) or count < 1:
        raise ValueError(f"Invalid {kind} count: {count}")
    return f"{{{count}}}"


def _component(c):
    t, value, count = c.get("type"), c.get("value"), c.get("count")
    if t in ("starts_with", "literal", "exact", "contains", "contains_after", "ends_with"):
        return escape_regex(value)
    if t == "space":
        return " "
    if t == "digits":
        return DIGIT + _quant(count, "digits")
    if t == "letters":
        return LETTER + _quant(count, "letters")
    if t == "words":
        if count is None:
            return WORD + r"(?:\s+" + WORD + ")*"
        if not isinstance(count, int) or count < 1:
            raise ValueError(f"Invalid words count: {count}")
        return WORD + (r"(?:\s+" + WORD + f"){{{count - 1}}}" if count > 1 else "")
    if t == "one_of":
        if not isinstance(value, list) or not value:
            raise ValueError("one_of requires values")
        return "(?:" + "|".join(escape_regex(v) for v in value) + ")"
    if t == "separator":
        symbol = SEPARATORS.get(value)
        if symbol is None:
            raise ValueError(f"Unsupported separator: {value}")
        return escape_regex(symbol)
    raise ValueError(f"Unsupported condition type: {t}")


def compile_condition(data: dict) -> str:
    conditions = data.get("conditions", [])
    whole_input = data.get("whole_input", False)

    looks = ["(?!" + ANY + escape_regex(c.get("value")) + ")"
             for c in conditions if c.get("type") == "not_contains"]
    rest = [c for c in conditions if c.get("type") != "not_contains"]

    if whole_input:
        body = ""
        for c in rest:
            t = c.get("type")
            if t in ("contains", "contains_after"):
                body += ANY + _component(c) + ANY
            elif t == "ends_with":
                body += ANY + _end(c.get("value"), c.get("scope"))
                return "^" + "".join(looks) + body
            else:
                body += _component(c)
        return "^" + "".join(looks) + body + "$"

    chain, anchored, tail = "", False, ""
    prev = None
    for i, c in enumerate(rest):
        t = c.get("type")
        nxt = rest[i + 1].get("type") if i + 1 < len(rest) else None
        if t == "space":
            chain += r"\s+"                       # כל סוג רווח (NBSP, טאב) — הודעות WhatsApp
        elif t in ("digits", "letters", "one_of") and prev in FIXED:
            chain += r"\s*" + _component(c)       # "קוד1234" וגם "קוד 1234"
        elif t == "literal" and prev in COUNTED:
            chain += r"\s*" + _component(c)       # "אבגדה ביי" וגם "אבגדהביי"
        elif t == "starts_with" and i == 0:
            chain, anchored = escape_regex(c.get("value")), True
        elif t == "contains":
            if nxt in ("contains_after", "space", "words", "literal", "digits", "letters", "one_of", "ends_with"):
                chain += (ANY if chain else "") + escape_regex(c.get("value"))
            else:
                looks.append("(?=" + ANY + escape_regex(c.get("value")) + ")")
        elif t == "contains_after":
            chain += ANY + escape_regex(c.get("value"))
        elif t == "ends_with":
            tail = _end(c.get("value"), c.get("scope"))
        elif t == "exact":
            return "^" + "".join(looks) + escape_regex(c.get("value")) + "$"
        else:
            chain += _component(c)
        prev = t

    # רצף שנגמר בכמות קבועה ופתוח בסוף — בדיוק N, לא N+1 ("קוד 12345" לא עובר על 4 ספרות)
    if not tail and rest:
        last = rest[-1]
        if last.get("type") == "digits" and last.get("count") is not None:
            chain += "(?![0-9])"
        elif last.get("type") == "letters" and last.get("count") is not None:
            chain += "(?!" + LETTER + ")"

    if tail and (chain or anchored):
        tail = ANY + tail
    if anchored:
        return "^" + "".join(looks) + chain + tail
    if looks:
        return "^" + "".join(looks) + (ANY if (chain or tail) else "") + chain + tail
    return chain + tail


# ── normalize: תיקונים דטרמיניסטיים לטעויות המודל ──────────────────────────

SYMBOL_WORDS = sorted({
    "נקודתיים": ":", "מקף": "-", "לוכסן": "/", "נקודה": ".", "קו תחתון": "_", "שטרודל": "@",
    "colon": ":", "hyphen": "-", "dash": "-", "slash": "/", "dot": ".", "period": ".",
    "underscore": "_", "at sign": "@",
    "двоеточие": ":", "дефис": "-", "тире": "-", "слэш": "/", "точка": ".",
    "подчёркивание": "_", "собака": "@",
    "نقطتان": ":", "شرطة مائلة": "/", "شرطة سفلية": "_", "شرطة": "-", "نقطة": ".",
}.items(), key=lambda kv: -len(kv[0]))

# "а затем" (#67 ב-POC) נתפס ע"י "затем"; "ואז" ע"י עצמו.
ORDER_WORDS = [
    "בהמשך", "אחר כך", "לאחר מכן", "מאוחר יותר", "ואז", "אחרי זה",
    "later", "afterwards", "after that", "then",
    "позже", "затем", "после этого", "потом",
    "لاحقًا", "لاحقا", "بعد ذلك", "ثم",
]


def _is_symbol(v):
    return isinstance(v, str) and len(v) == 1 and unicodedata.category(v)[0] in ("P", "S")


def _fix_order(conditions, request):
    text = request.lower()
    prev_end = None
    for c in conditions:
        v = c.get("value")
        if not isinstance(v, str) or not v:
            continue
        pos = text.find(v.lower(), prev_end or 0)
        if pos == -1:
            continue
        if c.get("type") == "contains" and prev_end is not None:
            if any(w in text[prev_end:pos] for w in ORDER_WORDS):
                c["type"] = "contains_after"
        prev_end = pos + len(v)


def _requested_symbols(request):
    text = re.sub(r'"[^"]*"', lambda m: " " * len(m.group()), request.lower())
    found, taken = [], [False] * len(text)
    for word, symbol in SYMBOL_WORDS:
        for m in re.finditer(re.escape(word.lower()), text):
            if not any(taken[m.start():m.end()]):
                found.append((m.start(), symbol))
                for k in range(m.start(), m.end()):
                    taken[k] = True
    return [s for _, s in sorted(found)]


# מילים שסוגרות את הפורמט: בלעדיהן "מתחיל ב..." הוא קידומת, ומה שאחריה חופשי
CLOSING_WORDS = [
    "בדיוק", "רק", "בלבד", "שום דבר נוסף", "כלום אחרי",
    "exactly", "only", "nothing else",
    "только", "точно", "ровно",
    "فقط", "بالضبط",
]


START_WORDS = [
    "מתחיל", "מתחילה", "להתחיל", "תחילת",
    "start", "begin",
    "начина",
    "يبدأ", "تبدأ", "بداية",
]


def _fix_start(data, request):
    """starts_with בלי שהבקשה אמרה 'מתחיל' — המודל ניחש. הערך יכול להופיע בכל מקום: starts_with → contains."""
    conds = data.get("conditions") or []
    if not conds or conds[0].get("type") != "starts_with":
        return
    text = re.sub(r'"[^"]*"', " ", request.lower())
    if not any(w in text for w in START_WORDS):
        conds[0]["type"] = "contains"
        data["whole_input"] = False


def _open_prefix(data, request):
    conds = data.get("conditions") or []
    if not data.get("whole_input") or not conds or conds[0].get("type") != "starts_with":
        return
    text = re.sub(r'"[^"]*"', " ", request.lower())   # מילים בתוך מרכאות הן ערכים, לא הוראות
    if not any(w in text for w in CLOSING_WORDS):
        data["whole_input"] = False


# מונחים לזיהוי "מילים" מול "אותיות" בבקשה — 4 שפות.
# עברית: "אות" רק כמילה שלמה (עם תחילית ו/ב/ה/ל/מ/ש/כ), אחרת "אותו"/"אותה" היו נתפסים כאותיות.
WORD_RE = re.compile(
    r"(?<![א-ת])[ובהלמשכ]?(?:מילה|מילים)"                 # he
    r"|\bwords?\b"                                       # en
    r"|слов"                                              # ru: слово / слова / слов / словами
    r"|كلم"                                               # ar: كلمة / كلمات
)
LETTER_RE = re.compile(
    r"(?<![א-ת])[ובהלמשכ]?(?:אותיות|אות)(?![א-ת])"      # he
    r"|\bletters?\b|\bcharacters?\b|\bchars?\b"       # en
    r"|букв|символ"                                       # ru: буква / буквы / букв / символов
    r"|حرف|حروف|أحرف"                                     # ar
)


# "מספר מילים" / "כמה" / several — בלי מספר בבקשה, הכמות פתוחה (אחד או יותר)
NUMBER_RE = re.compile(
    r"\d"
    r"|(?<![א-ת])[ובהלמשכ]?(?:אחת|אחד|שתיים|שתי|שניים|שני|שלוש|שלושה|ארבע|ארבעה|חמש|חמישה|שש|שישה|שבע|שבעה|שמונה|תשע|תשעה|עשר|עשרה)(?![א-ת])"
    r"|\b(?:one|two|three|four|five|six|seven|eight|nine|ten)\b"
    r"|\b(?:один|одно|одна|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять)\b"
    r"|(?:واحد|اثنان|اثنين|ثلاث|أربع|خمس|ست|سبع|ثمان|تسع|عشر)"
)

# "בסוף" — הערך הוא סוף ההודעה; "סוף המשפט" — סוף משפט, גם באמצע הודעה עם כמה משפטים
END_RE      = re.compile(r"בסוף|ובסוף|at the end|ends? with|в конце|заканчива|في نهاية|ينتهي|تنتهي")
SENTENCE_RE = re.compile(r"(?:סוף|בסוף|ובסוף)\s+(?:ה)?משפט|end of (?:the |a )?sentence|конце предложения|نهاية الجملة")


def _fix_counts(data, request):
    text = re.sub(r'"[^"]*"', " ", request.lower())
    if NUMBER_RE.search(text):
        return
    for c in data.get("conditions") or []:
        if c.get("type") in ("words", "letters", "digits") and c.get("count") is not None:
            c["count"] = None   # המודל המציא מספר


def _fix_end(data, request):
    """ערך שבתיאור בא אחרי 'בסוף' — ends_with, גם אם המודל החזיר contains/literal."""
    text = request.lower()
    conds = data.get("conditions") or []
    sentence = bool(SENTENCE_RE.search(re.sub(r'"[^"]*"', " ", text)))
    for c in conds:
        v = c.get("value")
        if not isinstance(v, str) or not v:
            continue
        pos = text.find('"' + v.lower() + '"')
        if pos == -1:
            continue
        before = text[max(0, pos - 25):pos]
        if c.get("type") in ("contains", "contains_after", "literal", "ends_with") and (END_RE.search(before) or c.get("type") == "ends_with"):
            c["type"] = "ends_with"
            if sentence:
                c["scope"] = "sentence"
    # ends_with הוא תמיד האחרון ברצף
    ends = [c for c in conds if c.get("type") == "ends_with"]
    if ends:
        data["conditions"] = [c for c in conds if c.get("type") != "ends_with"] + ends[-1:]
        if not sentence:
            data["whole_input"] = False


def _fix_words(data, request):
    """המודל מבלבל 'מילים' עם 'אותיות'. אם הבקשה מדברת על מילים ולא על אותיות — letters → words.
    ואחרי words, ערך שבא אחריו הוא צמוד (literal), ומסביב ל-words תמיד יש רווח."""
    text = re.sub(r'"[^"]*"', " ", request.lower())
    conds = data.get("conditions") or []
    has_word, has_letter = bool(WORD_RE.search(text)), bool(LETTER_RE.search(text))
    if has_word and not has_letter:
        for c in conds:
            if c.get("type") == "letters":
                c["type"] = "words"
    elif has_letter and not has_word:                    # ובכיוון ההפוך: "אותיות" שהמודל קרא כמילים
        for c in conds:
            if c.get("type") == "words":
                c["type"] = "letters"

    # "ואחרי 5 מילים/אותיות/ספרות X" — X בא מיד אחרי הרכיב הספור, לא איפשהו בהמשך
    for i, c in enumerate(conds):
        if c.get("type") not in ("contains", "contains_after") or i == 0:
            continue
        j = i - 1
        while j >= 0 and conds[j].get("type") == "space":
            j -= 1
        if j >= 0 and conds[j].get("type") in ("words", "letters", "digits"):
            c["type"] = "literal"

    if not any(c.get("type") == "words" for c in conds):
        return

    out = []
    for i, c in enumerate(conds):
        t = c.get("type")
        if t == "words" and out and out[-1].get("type") != "space":
            out.append({"type": "space", "value": None, "count": None})
        if out and out[-1].get("type") == "words" and t not in ("space", "not_contains"):
            out.append({"type": "space", "value": None, "count": None})
        out.append(c)
    data["conditions"] = out


def _collapse_spaces(data):
    """רווחים רצופים הם רווח אחד: space,space,space → \\s+ יחיד, לא \\s+\\s+\\s+ (שדורש 3 רווחים לפחות)."""
    out = []
    for c in data.get("conditions") or []:
        if c.get("type") == "space" and out and out[-1].get("type") == "space":
            continue
        out.append(c)
    data["conditions"] = out


def normalize_dsl(data, request: str):
    if not isinstance(data, dict) or not isinstance(data.get("conditions"), list):
        return data
    conditions = data["conditions"]

    for c in conditions:
        if c.get("type") == "one_of" and isinstance(c.get("value"), list) and len(c["value"]) == 1:
            c["value"] = c["value"][0]
            if len(conditions) == 1:
                c["type"] = "exact"
                data["whole_input"] = True
            else:
                c["type"] = "literal"

    for c in conditions:
        if c.get("type") == "separator":
            c["type"] = "literal"
            c["value"] = SEPARATORS.get(c.get("value"), c.get("value"))

    requested = _requested_symbols(request)
    slots = [c for c in conditions if c.get("type") == "literal" and _is_symbol(c.get("value"))]
    if requested and len(requested) == len(slots):
        for slot, symbol in zip(slots, requested):
            slot["value"] = symbol

    _fix_order(conditions, request)
    _fix_start(data, request)
    _fix_words(data, request)
    _fix_counts(data, request)
    _fix_end(data, request)
    _collapse_spaces(data)
    _open_prefix(data, request)
    return data
