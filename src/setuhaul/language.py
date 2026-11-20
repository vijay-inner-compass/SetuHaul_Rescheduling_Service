"""Language handling: detection, localized deterministic wording, and claim detection.

Supported: English (en), Hindi in Devanagari (hi) and Hinglish, which is Hindi written in Latin
letters (hinglish). Anything else is unsupported. Original driver text is never altered; detection
only chooses which language the service answers in.

Deterministic wording lives here so that anything that states a fact about a booking (held,
confirmed, released, expired, hand-off) is rendered from verified state, not written by the model.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

Lang = Literal["en", "hi", "hinglish"]
LANGS: tuple[Lang, ...] = ("en", "hi", "hinglish")

HINGLISH_WORDS = frozenset(["hai", "hain", "nahi", "nahin", "kal", "aaj", "gaadi", "gadi", "ghanta", "ghante", "der", "pahunch", "pahunchunga", "pahunchungi", "pahunchega", "mein", "kharab", "ho", "hoga", "gayi", "gaya", "raha", "rahi", "rahe", "karo", "kar", "chahiye", "abhi", "bhai", "theek", "thik", "haan", "mera", "meri", "mujhe", "aap", "tum", "kyun", "kitna", "lagenge", "lagega", "ruk", "bas", "jaldi", "pehle", "baad", "wala", "wali", "nikla", "nikli", "fasa", "fasi", "fansa", "jam", "tha", "thi", "toh", "na", "kya", "kaise", "kab"])
ENGLISH_WORDS = frozenset(["the", "is", "am", "are", "my", "i", "truck", "late", "delay", "delayed", "slot", "please", "can", "will", "not", "stuck", "traffic", "minutes", "minute", "hour", "hours", "broke", "broken", "reach", "arrive", "arriving", "confirm", "cancel", "yes", "no", "thanks", "original", "keep", "option", "options", "would", "like", "need", "have", "has", "it", "to", "for", "and", "with", "warehouse", "dock", "driver", "shipment", "appointment", "hello", "hi", "hey", "ok", "okay", "sure", "thank"])


@dataclass(frozen=True)
class LanguageDetection:
    language: Lang | Literal["unsupported", "unknown"]
    confidence: float
    script: str  # latin | devanagari | other | none


def _script_counts(text: str) -> tuple[int, int, int]:
    latin = deva = other = 0
    for ch in text:
        code = ord(ch)
        if 0x0900 <= code <= 0x097F and not (0x0964 <= code <= 0x096F):
            deva += 1  # includes vowel signs, which are not "alphabetic" but are Devanagari text
        elif not ch.isalpha():
            continue
        elif unicodedata.name(ch, "").startswith("LATIN"):
            latin += 1
        else:
            other += 1
    return latin, deva, other


def detect_language(text: str, previous: str | None = None) -> LanguageDetection:
    latin, deva, other = _script_counts(text)
    total = latin + deva + other
    if total == 0:
        return _fallback(previous, "none")
    if other / total >= 0.3:
        return LanguageDetection("unsupported", 0.9, "other")
    if deva / total >= 0.3:
        return LanguageDetection("hi", 0.9, "devanagari")
    tokens = re.findall(r"[a-z]+", text.lower())
    hin = sum(t in HINGLISH_WORDS for t in tokens)
    eng = sum(t in ENGLISH_WORDS for t in tokens)
    if hin >= 2 or (hin == 1 and eng <= 1):
        return LanguageDetection("hinglish", 0.9 if hin >= 2 else 0.6, "latin")
    if eng >= 1:
        return LanguageDetection("en", 0.85 if eng >= 2 else 0.7, "latin")
    if len(tokens) >= 2:  # Latin text with no Hindi markers: English unless the driver says otherwise
        return LanguageDetection("en", 0.6, "latin")
    return _fallback(previous, "latin")


def _fallback(previous: str | None, script: str) -> LanguageDetection:
    if previous in LANGS:
        return LanguageDetection(previous, 0.5, script)
    return LanguageDetection("unknown", 0.2, script)


def parse_language_choice(text: str) -> Lang | None:
    t = text.strip().lower()
    if t in {"english", "en", "angrezi", "अंग्रेज़ी", "अंग्रेजी"}:
        return "en"
    if t in {"hindi", "हिंदी", "हिन्दी", "hi"}:
        return "hi"
    if t in {"hinglish"}:
        return "hinglish"
    return None


# ---- deterministic wording -------------------------------------------------------------------------
LANGUAGE_PROMPT = (
    "Which language would you like to use? English or Hindi?\n"
    "आप किस भाषा में बात करना चाहेंगे? English या हिंदी?"
)

T: dict[str, dict[str, str]] = {
    "language_ack": {
        "en": "Thanks. Please tell me what is happening with your delivery.",
        "hi": "धन्यवाद। कृपया बताइए कि आपकी डिलीवरी में क्या हो रहा है।",
        "hinglish": "Thanks. Bataiye aapki delivery mein kya ho raha hai.",
    },
    "hold_expired": {
        "en": "Your hold on that slot has lapsed. No booking has changed, and your original appointment "
              "still stands. I can search for fresh options if you would like.",
        "hi": "उस स्लॉट पर आपकी होल्ड की अवधि खत्म हो गई है। आपकी कोई बुकिंग नहीं बदली है और आपकी मूल "
              "अपॉइंटमेंट वैसी ही है। चाहें तो मैं नए विकल्प खोज सकता हूँ।",
        "hinglish": "Us slot par aapka hold khatam ho gaya hai. Aapki koi booking nahi badli hai aur original "
                    "appointment waisi hi hai. Chahein toh main naye options dhoondh sakta hoon.",
    },
    "hold_released": {
        "en": "I have released the hold. Your original appointment is unchanged.",
        "hi": "मैंने होल्ड हटा दिया है। आपकी मूल अपॉइंटमेंट वैसी ही है।",
        "hinglish": "Maine hold hata diya hai. Aapki original appointment waisi hi hai.",
    },
    "hold_placed": {
        "en": "I am holding {start}-{end} at dock {dock} on {date} for you until {until}. "
              "Reply to confirm this slot, or tell me if you would like another option.",
        "hi": "मैंने आपके लिए {date} को डॉक {dock} पर {start}-{end} का स्लॉट {until} तक होल्ड किया है। "
              "इसे कन्फर्म करने के लिए जवाब दें, या कोई और विकल्प बताइए।",
        "hinglish": "Maine aapke liye {date} ko dock {dock} par {start}-{end} ka slot {until} tak hold kiya hai. "
                    "Confirm karne ke liye reply karein, ya koi aur option bataiye.",
    },
    "confirmed": {
        "en": "Your appointment is confirmed: dock {dock}, {start}-{end} on {date}. Reference {ref}.",
        "hi": "आपकी अपॉइंटमेंट कन्फर्म हो गई है: डॉक {dock}, {date} को {start}-{end}। रेफरेंस {ref}।",
        "hinglish": "Aapki appointment confirm ho gayi hai: dock {dock}, {date} ko {start}-{end}. Reference {ref}.",
    },
    "cancelled": {
        "en": "Your appointment has been cancelled. Reference {ref}.",
        "hi": "आपकी अपॉइंटमेंट रद्द कर दी गई है। रेफरेंस {ref}।",
        "hinglish": "Aapki appointment cancel kar di gayi hai. Reference {ref}.",
    },
    "temp_unavailable": {
        "en": "I cannot check or change bookings right now, so I cannot promise anything. Nothing has been "
              "booked or held by this message. Operations has been told.",
        "hi": "अभी मैं बुकिंग देख या बदल नहीं सकता, इसलिए कोई वादा नहीं कर सकता। इस संदेश से कुछ भी बुक या "
              "होल्ड नहीं हुआ है। ऑपरेशंस टीम को बता दिया गया है।",
        "hinglish": "Abhi main booking dekh ya badal nahi sakta, isliye koi wada nahi kar sakta. Is message se "
                    "kuch bhi book ya hold nahi hua hai. Operations team ko bata diya gaya hai.",
    },
    "handoff": {
        "en": "I cannot sort this out automatically, so I have passed it to the operations team with the "
              "details. Your case reference is {case}. Nothing on your booking has been changed by this. "
              "You will get updates here in this chat.",
        "hi": "मैं इसे अपने आप हल नहीं कर सकता, इसलिए मैंने पूरी जानकारी के साथ इसे ऑपरेशंस टीम को दे दिया है। "
              "आपका केस रेफरेंस {case} है। इससे आपकी बुकिंग में कुछ नहीं बदला है। अपडेट आपको इसी चैट में मिलेंगे।",
        "hinglish": "Main ise apne aap hal nahi kar sakta, isliye maine poori jaankari ke saath ise operations "
                    "team ko de diya hai. Aapka case reference {case} hai. Isse aapki booking mein kuch nahi "
                    "badla hai. Updates aapko isi chat mein milenge.",
    },
    "unverified_claim": {
        "en": "I have not confirmed or held anything yet. Let me check the system and come back to you.",
        "hi": "मैंने अभी तक कुछ भी कन्फर्म या होल्ड नहीं किया है। मैं सिस्टम देखकर आपको बताता हूँ।",
        "hinglish": "Maine abhi tak kuch bhi confirm ya hold nahi kiya hai. Main system dekhkar aapko batata hoon.",
    },
    "case_claimed": {
        "en": "A member of the operations team is now looking at your case {case}. You will see updates here in this chat.",
        "hi": "ऑपरेशंस टीम का एक सदस्य अब आपके केस {case} को देख रहा है। अपडेट आपको इसी चैट में मिलेंगे।",
        "hinglish": "Operations team ka ek member ab aapke case {case} ko dekh raha hai. Updates aapko isi chat mein milenge.",
    },
    "case_resolved": {
        "en": "Operations has resolved your case {case}: {note}",
        "hi": "ऑपरेशंस टीम ने आपका केस {case} हल कर दिया है: {note}",
        "hinglish": "Operations team ne aapka case {case} hal kar diya hai: {note}",
    },
    "case_cancelled": {
        "en": "Your case {case} has been cancelled at your request.",
        "hi": "आपके कहने पर केस {case} रद्द कर दिया गया है।",
        "hinglish": "Aapke kehne par case {case} cancel kar diya gaya hai.",
    },
    "case_status_open": {
        "en": "Your case {case} has been received by operations and is waiting for someone to pick it up. Updates will appear here in this chat.",
        "hi": "आपका केस {case} ऑपरेशंस टीम को मिल गया है और किसी के उठाने का इंतज़ार कर रहा है। अपडेट इसी चैट में दिखेंगे।",
        "hinglish": "Aapka case {case} operations team ko mil gaya hai aur kisi ke uthane ka intezaar kar raha hai. Updates isi chat mein dikhenge.",
    },
    "case_status_claimed": {
        "en": "Your case {case} is being handled by the operations team. Updates will appear here in this chat.",
        "hi": "आपका केस {case} ऑपरेशंस टीम देख रही है। अपडेट इसी चैट में दिखेंगे।",
        "hinglish": "Aapka case {case} operations team dekh rahi hai. Updates isi chat mein dikhenge.",
    },
    "sla_suffix": {
        "en": " Operations aims to respond within {minutes} minutes.",
        "hi": " ऑपरेशंस टीम {minutes} मिनट के भीतर जवाब देने का लक्ष्य रखती है।",
        "hinglish": " Operations team {minutes} minute ke andar jawab dene ka lakshya rakhti hai.",
    },
    "prompt_leak": {
        "en": "I cannot share that. I can help with your delivery slot: tell me what is happening.",
        "hi": "मैं यह साझा नहीं कर सकता। मैं आपके डिलीवरी स्लॉट में मदद कर सकता हूँ: बताइए क्या हो रहा है।",
        "hinglish": "Main yeh share nahi kar sakta. Main aapke delivery slot mein madad kar sakta hoon: bataiye kya ho raha hai.",
    },
}


def say(key: str, lang: str | None, **fields: str) -> str:
    variants = T[key]
    return variants.get(lang or "en", variants["en"]).format(**fields)


# ---- claim detection (used by the output guard) -----------------------------------------------------
_CONFIRM_CLAIMS = [re.compile(p, re.IGNORECASE) for p in (
    # "booked by someone else" describes another party's booking, not a claim about this driver's
    r"\b(is|are|was|been|now|got|am)\s+(now\s+)?(confirmed|booked)\b(?!\s+(by|for)\s+(someone|somebody|another|other|a\s+different|the\s+planner|a\s+planner))",
    r"\bi('ve|\s+have)\s+(now\s+)?(confirmed|booked)\b",
    r"\b(booking|appointment|slot|reschedule)\s+(is\s+)?confirmed\b",
    r"\bi\s+(just\s+|now\s+)?(confirmed|booked)\b",
    r"\bconfirm(ed)?\s+ho\s+gay[ai]\b", r"\bbook\s+ho\s+gay[ai]\b", r"\bbook\s+kar\s+diya\b",
    r"\bconfirm\s+kar\s+diya\b", r"कन्फर्म\s+हो\s+गई?\b", r"कन्फर्म\s+कर\s+दिया", r"बुक\s+हो\s+गया",
    r"बुक\s+कर\s+दिया",
)]
_HOLD_CLAIMS = [re.compile(p, re.IGNORECASE) for p in (
    r"\bi('ve|\s+have)\s+(placed\s+a\s+)?held\b", r"\bi\s+am\s+holding\b", r"\bi'm\s+holding\b",
    r"\b(slot|it|that)\s+(is|has\s+been)\s+(being\s+)?held\b", r"\bheld\s+(it\s+)?for\s+you\b",
    r"\bhold\s+kar\s+(liya|diya)\b", r"\bhold\s+kiya\s+hai\b", r"होल्ड\s+(कर\s+लिया|कर\s+दिया|किया\s+है)",
    r"रोक\s+लिया",
)]


# "Nothing has been booked or held" and "I have not confirmed anything" deny a booking; they must not count as claiming one.
_NEGATED = re.compile(r"\b(nothing|not|never|no|isn't|hasn't|haven't|wasn't)\b[^.!?\n]{0,40}?\b(confirmed|booked|held|holding)\b", re.IGNORECASE)


def claims_confirmed(text: str) -> bool:
    text = _NEGATED.sub(" ", text)
    return any(p.search(text) for p in _CONFIRM_CLAIMS)


def claims_held(text: str) -> bool:
    text = _NEGATED.sub(" ", text)
    return any(p.search(text) for p in _HOLD_CLAIMS)
