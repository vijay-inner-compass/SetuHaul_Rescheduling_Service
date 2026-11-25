"""Language handling: detection, localized deterministic wording, and claim detection.

Supported: English (en), Hindi in Devanagari (hi) and Hinglish, which is Hindi written in Latin
letters (hinglish). Anything else is unsupported. Original driver text is never altered; detection
only chooses which language the service answers in.

Deterministic wording lives here so that anything that states a fact about a booking (held,
confirmed, released, expired, hand-off) is rendered from verified state, not written by the model.
"""

# Lazy annotations.
from __future__ import annotations

# re: word and phrase matching for detection and claim checks.
import re

# unicodedata: tells whether a character belongs to the Latin alphabet.
import unicodedata

# dataclass for the detection result.
from dataclasses import dataclass

# Literal restricts values to a fixed list of strings.
from typing import Literal

# The three languages the service answers in.
Lang = Literal["en", "hi", "hinglish"]
# The same list as a tuple, for membership tests.
LANGS: tuple[Lang, ...] = ("en", "hi", "hinglish")

# Common Hindi words written in Latin letters. Finding two or more in a message marks it as Hinglish.
HINGLISH_WORDS = frozenset(["hai", "hain", "nahi", "nahin", "kal", "aaj", "gaadi", "gadi", "ghanta", "ghante", "der", "pahunch", "pahunchunga", "pahunchungi", "pahunchega", "mein", "kharab", "ho", "hoga", "gayi", "gaya", "raha", "rahi", "rahe", "karo", "kar", "chahiye", "abhi", "bhai", "theek", "thik", "haan", "mera", "meri", "mujhe", "aap", "tum", "kyun", "kitna", "lagenge", "lagega", "ruk", "bas", "jaldi", "pehle", "baad", "wala", "wali", "nikla", "nikli", "fasa", "fasi", "fansa", "jam", "tha", "thi", "toh", "na", "kya", "kaise", "kab"])
# Common English words. Used to tell English apart from Hinglish when a message has few clear markers.
ENGLISH_WORDS = frozenset(["the", "is", "am", "are", "my", "i", "truck", "late", "delay", "delayed", "slot", "please", "can", "will", "not", "stuck", "traffic", "minutes", "minute", "hour", "hours", "broke", "broken", "reach", "arrive", "arriving", "confirm", "cancel", "yes", "no", "thanks", "original", "keep", "option", "options", "would", "like", "need", "have", "has", "it", "to", "for", "and", "with", "warehouse", "dock", "driver", "shipment", "appointment", "hello", "hi", "hey", "ok", "okay", "sure", "thank"])


# The outcome of language detection.
@dataclass(frozen=True)
# language: a supported code, or 'unsupported' (another script) or 'unknown' (not enough text).
# confidence: a rough 0-1 score, not a probability.
# script: which writing system was seen.
class LanguageDetection:
    language: Lang | Literal["unsupported", "unknown"]
    confidence: float
    script: str  # latin | devanagari | other | none


# Count letters by script: (Latin, Devanagari, other). Punctuation and digits are ignored.
def _script_counts(text: str) -> tuple[int, int, int]:
    # Three counters starting at zero.
    latin = deva = other = 0
    # Look at every character.
    for ch in text:
        # The character's Unicode number.
        code = ord(ch)
        # The Devanagari block, excluding the danda and digit range, counts as Hindi script.
        if 0x0900 <= code <= 0x097F and not (0x0964 <= code <= 0x096F):
            deva += 1  # includes vowel signs, which are not "alphabetic" but are Devanagari text
        # Spaces, digits and punctuation carry no language signal: skip them.
        elif not ch.isalpha():
            continue
        # A letter whose Unicode name starts with LATIN is a Latin letter.
        elif unicodedata.name(ch, "").startswith("LATIN"):
            latin += 1
        # Any other alphabet (Tamil, Arabic, Chinese ...) counts as 'other'.
        else:
            other += 1
    # The three totals.
    return latin, deva, other


# Decide which language to answer in. 'previous' is the language already established in this conversation.
def detect_language(text: str, previous: str | None = None) -> LanguageDetection:
    # How much of each script the message has.
    latin, deva, other = _script_counts(text)
    # Total letters (not counting punctuation).
    total = latin + deva + other
    # No letters at all (for example just '👍' or '123'): keep the previous language.
    if total == 0:
        return _fallback(previous, "none")
    # 30% or more letters in a script we do not support: say so, do not guess.
    if other / total >= 0.3:
        return LanguageDetection("unsupported", 0.9, "other")
    # 30% or more Devanagari: Hindi.
    if deva / total >= 0.3:
        return LanguageDetection("hi", 0.9, "devanagari")
    # Split Latin text into lower-case words.
    tokens = re.findall(r"[a-z]+", text.lower())
    # How many words are common Hinglish words.
    hin = sum(t in HINGLISH_WORDS for t in tokens)
    # How many words are common English words.
    eng = sum(t in ENGLISH_WORDS for t in tokens)
    # Two or more Hinglish words, or one with at most one English word, means Hinglish.
    if hin >= 2 or (hin == 1 and eng <= 1):
        return LanguageDetection("hinglish", 0.9 if hin >= 2 else 0.6, "latin")
    # At least one English marker word means English (more markers give higher confidence).
    if eng >= 1:
        return LanguageDetection("en", 0.85 if eng >= 2 else 0.7, "latin")
    # Two or more Latin words with no Hindi markers: assume English.
    if len(tokens) >= 2:  # Latin text with no Hindi markers: English unless the driver says otherwise
        return LanguageDetection("en", 0.6, "latin")
    # A single unclear word: keep the previous language if there is one.
    return _fallback(previous, "latin")


# When the text itself is not decisive, reuse the earlier language, with low confidence.
def _fallback(previous: str | None, script: str) -> LanguageDetection:
    if previous in LANGS:
        return LanguageDetection(previous, 0.5, script)
    return LanguageDetection("unknown", 0.2, script)


# Understand a reply to 'English or Hindi?' such as 'english', 'हिंदी' or 'hinglish'.
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
# The two-language question asked when the driver's language cannot be told. It is shown in English and Hindi together.
LANGUAGE_PROMPT = (
    "Which language would you like to use? English or Hindi?\n"
    "आप किस भाषा में बात करना चाहेंगे? English या हिंदी?"
)

# The wording table: key -> {language -> text}. Every fact about a booking (held, confirmed, cancelled, hand-off, case updates, menus) is
# rendered from this table using verified data, never written by the model. {braces} are filled in by say().
T: dict[str, dict[str, str]] = {
    # Reply after the driver picks a language.
    "language_ack": {
        "en": "Thanks. Please tell me what is happening with your delivery.",
        "hi": "धन्यवाद। कृपया बताइए कि आपकी डिलीवरी में क्या हो रहा है।",
        "hinglish": "Thanks. Bataiye aapki delivery mein kya ho raha hai.",
    },
    # Told to the driver when their hold has lapsed. It says nothing was booked and the original stands.
    "hold_expired": {
        "en": "Your hold on that slot has lapsed. No booking has changed, and your original appointment "
              "still stands. I can search for fresh options if you would like.",
        "hi": "उस स्लॉट पर आपकी होल्ड की अवधि खत्म हो गई है। आपकी कोई बुकिंग नहीं बदली है और आपकी मूल "
              "अपॉइंटमेंट वैसी ही है। चाहें तो मैं नए विकल्प खोज सकता हूँ।",
        "hinglish": "Us slot par aapka hold khatam ho gaya hai. Aapki koi booking nahi badli hai aur original "
                    "appointment waisi hi hai. Chahein toh main naye options dhoondh sakta hoon.",
    },
    # Told when a hold was released (the driver changed their mind).
    "hold_released": {
        "en": "I have released the hold. Your original appointment is unchanged.",
        "hi": "मैंने होल्ड हटा दिया है। आपकी मूल अपॉइंटमेंट वैसी ही है।",
        "hinglish": "Maine hold hata diya hai. Aapki original appointment waisi hi hai.",
    },
    # Told when a hold was placed: which slot, until when, and what to do next.
    # A facility change is only ever a request until the destination warehouse approves it.
    "change_requested": {
        "en": "I have asked {to} to take this load. Your destination has not changed yet: it is still {origin} until they approve.",
        "hi": "मैंने {to} से यह लोड लेने का अनुरोध किया है। आपकी मंज़िल अभी नहीं बदली है: उनकी मंज़ूरी तक {origin} ही है।",
        "hinglish": "Maine {to} se yeh load lene ki request ki hai. Aapki destination abhi nahi badli hai: unki approval tak {origin} hi hai.",
    },
    "change_pending": {
        "en": "A request to move to {to} is already waiting for approval. Your destination is still {origin}.",
        "hi": "{to} में बदलने का अनुरोध पहले से मंज़ूरी का इंतज़ार कर रहा है। आपकी मंज़िल अभी {origin} ही है।",
        "hinglish": "{to} mein badalne ki request pehle se approval ka wait kar rahi hai. Aapki destination abhi {origin} hi hai.",
    },
    "change_refused": {
        "en": "That change cannot be requested for this load.",
        "hi": "इस लोड के लिए यह बदलाव नहीं माँगा जा सकता।",
        "hinglish": "Is load ke liye yeh change request nahi ho sakta.",
    },
    "hold_placed": {
        "en": "I am holding {start}-{end} at dock {dock} on {date}, {facility}, for you until {until}. "
              "It is not booked yet. Reply Yes to confirm, or No for the next slot.",
        "hi": "मैंने आपके लिए {facility} में {date} को डॉक {dock} पर {start}-{end} का स्लॉट {until} तक होल्ड किया है। "
              "अभी बुक नहीं हुआ है। कन्फर्म करने के लिए Yes लिखें, या अगले स्लॉट के लिए No लिखें।",
        "hinglish": "Maine aapke liye {facility} mein {date} ko dock {dock} par {start}-{end} ka slot {until} tak hold kiya hai. "
                    "Abhi book nahi hua hai. Confirm karne ke liye Yes likhein, ya agle slot ke liye No likhein.",
    },
    # First line of the numbered option menu (D65). It says plainly that nothing is held.
    "options_intro": {
        "en": "These slots are available. Nothing is held yet:",
        "hi": "ये स्लॉट उपलब्ध हैं। अभी कुछ भी होल्ड नहीं है:",
        "hinglish": "Ye slots available hain. Abhi kuch bhi hold nahi hai:",
    },
    # One numbered line of the menu: number, dock, times and date.
    "options_line": {
        "en": "{n}. Dock {dock}, {start}-{end} on {date}",
        "hi": "{n}. डॉक {dock}, {date} को {start}-{end}",
        "hinglish": "{n}. Dock {dock}, {date} ko {start}-{end}",
    },
    # Last line of the menu: which numbers can be chosen and until when the options are valid.
    "options_outro": {
        "en": "Reply with the number ({choices}) to hold one. These options are valid until {until}.",
        "hi": "होल्ड करने के लिए नंबर ({choices}) बताइए। ये विकल्प {until} तक मान्य हैं।",
        "hinglish": "Hold karne ke liye number ({choices}) bataiye. Ye options {until} tak valid hain.",
    },
    # The driver's request was refused because the truck's position changed and the slot can no longer be reached in time.
    "position_changed": {
        "en": "Your truck's position has changed, so that slot can no longer be reached in time. Nothing is held and your "
              "original appointment is unchanged. Tell me if you would like me to search again.",
        "hi": "आपके ट्रक की स्थिति बदल गई है, इसलिए उस स्लॉट तक समय पर नहीं पहुंचा जा सकता। कुछ भी होल्ड नहीं है और आपकी मूल "
              "अपॉइंटमेंट वैसी ही है। चाहें तो मैं दोबारा खोज सकता हूँ।",
        "hinglish": "Aapke truck ki position badal gayi hai, isliye us slot tak time par nahi pahunch sakte. Kuch bhi hold nahi hai "
                    "aur aapki original appointment waisi hi hai. Chahein toh main dobara dhoondh sakta hoon.",
    },
    # Told to the driver after every new position: it was received, how far the dock is and when they could arrive.
    "position_received": {
        "en": "Thanks, I have your new position: about {km} km from the dock, about {minutes} minutes away, so you could arrive around {eta}.",
        "hi": "धन्यवाद, मुझे आपकी नई स्थिति मिल गई है: डॉक से लगभग {km} किमी दूर, लगभग {minutes} मिनट की दूरी, यानी आप लगभग {eta} तक पहुंच सकते हैं।",
        "hinglish": "Thanks, mujhe aapki nayi position mil gayi hai: dock se lagbhag {km} km door, lagbhag {minutes} minute ki doori, yaani aap "
                    "lagbhag {eta} tak pahunch sakte hain.",
    },
    # First line of an offer of earlier slots when the truck has come closer; the numbered lines and closing line follow (options_line, options_outro).
    "earlier_intro": {
        "en": "For order {order}: your truck is closer now, so earlier slots than your current one are available. Nothing is held yet:",
        "hi": "ऑर्डर {order} के लिए: आपका ट्रक अब पास है, इसलिए आपके मौजूदा स्लॉट से पहले के स्लॉट उपलब्ध हैं। अभी कुछ भी होल्ड नहीं है:",
        "hinglish": "Order {order} ke liye: aapka truck ab paas hai, isliye aapke maujooda slot se pehle ke slots available hain. Abhi kuch bhi hold nahi hai:",
    },
    # Told to the driver when a new GPS ping showed the held slot is out of reach and the hold was released at once.
    "position_released": {
        "en": "Your truck's position has changed, so I have released the hold on {start}-{end} at dock {dock}: it can no longer be "
              "reached in time. Your original appointment is unchanged. Tell me if you would like me to search again.",
        "hi": "आपके ट्रक की स्थिति बदल गई है, इसलिए मैंने डॉक {dock} पर {start}-{end} का होल्ड हटा दिया है: वहां समय पर नहीं पहुंचा जा "
              "सकता। आपकी मूल अपॉइंटमेंट वैसी ही है। चाहें तो मैं दोबारा खोज सकता हूँ।",
        "hinglish": "Aapke truck ki position badal gayi hai, isliye maine dock {dock} par {start}-{end} ka hold hata diya hai: wahan "
                    "time par nahi pahunch sakte. Aapki original appointment waisi hi hai. Chahein toh main dobara dhoondh sakta hoon.",
    },
    # Added after a menu when the truck's GPS disagrees with the driver's report; the gap is absolute, so no direction is claimed.
    "gps_note": {
        "en": "Note: your truck's GPS suggests your arrival differs from your report by about {minutes} minutes.",
        "hi": "ध्यान दें: आपके ट्रक के GPS के अनुसार आपके पहुंचने का समय आपकी बताई जानकारी से लगभग {minutes} मिनट अलग है।",
        "hinglish": "Dhyan dein: aapke truck ke GPS ke hisaab se aapke pahunchne ka time aapki batayi jaankari se lagbhag "
                    "{minutes} minute alag hai.",
    },
    # Told when a reschedule is confirmed. Built from the committed appointment row.
    "confirmed": {
        "en": "Your appointment is confirmed: dock {dock}, {start}-{end} on {date}. Reference {ref}. Warehouse: {facility}.",
        "hi": "आपकी अपॉइंटमेंट कन्फर्म हो गई है: डॉक {dock}, {date} को {start}-{end}। रेफरेंस {ref}। वेयरहाउस: {facility}।",
        "hinglish": "Aapki appointment confirm ho gayi hai: dock {dock}, {date} ko {start}-{end}. Reference {ref}. Warehouse: {facility}.",
    },
    # Told when an appointment was cancelled.
    "cancelled": {
        "en": "Your appointment has been cancelled. Reference {ref}.",
        "hi": "आपकी अपॉइंटमेंट रद्द कर दी गई है। रेफरेंस {ref}।",
        "hinglish": "Aapki appointment cancel kar di gayi hai. Reference {ref}.",
    },
    # Told when a service failed mid-turn: promises nothing and says nothing was changed.
    "temp_unavailable": {
        "en": "I cannot check or change bookings right now, so I cannot promise anything. Nothing has been "
              "booked or held by this message. Operations has been told.",
        "hi": "अभी मैं बुकिंग देख या बदल नहीं सकता, इसलिए कोई वादा नहीं कर सकता। इस संदेश से कुछ भी बुक या "
              "होल्ड नहीं हुआ है। ऑपरेशंस टीम को बता दिया गया है।",
        "hinglish": "Abhi main booking dekh ya badal nahi sakta, isliye koi wada nahi kar sakta. Is message se "
                    "kuch bhi book ya hold nahi hua hai. Operations team ko bata diya gaya hai.",
    },
    # Told when the case was passed to operations: gives the case reference and says nothing changed.
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
    # Replaces model text that claimed a hold or confirmation the database does not back.
    "unverified_claim": {
        "en": "I have not confirmed or held anything yet. Let me check the system and come back to you.",
        "hi": "मैंने अभी तक कुछ भी कन्फर्म या होल्ड नहीं किया है। मैं सिस्टम देखकर आपको बताता हूँ।",
        "hinglish": "Maine abhi tak kuch bhi confirm ya hold nahi kiya hai. Main system dekhkar aapko batata hoon.",
    },
    # Posted to the chat when a coordinator picks up the case.
    "case_claimed": {
        "en": "A member of the operations team is now looking at your case {case}. You will see updates here in this chat.",
        "hi": "ऑपरेशंस टीम का एक सदस्य अब आपके केस {case} को देख रहा है। अपडेट आपको इसी चैट में मिलेंगे।",
        "hinglish": "Operations team ka ek member ab aapke case {case} ko dekh raha hai. Updates aapko isi chat mein milenge.",
    },
    # Posted to the chat when a coordinator resolves the case, with their note.
    "case_resolved": {
        "en": "Operations has resolved your case {case}: {note}",
        "hi": "ऑपरेशंस टीम ने आपका केस {case} हल कर दिया है: {note}",
        "hinglish": "Operations team ne aapka case {case} hal kar diya hai: {note}",
    },
    # Posted when the driver cancels their case.
    "case_cancelled": {
        "en": "Your case {case} has been cancelled at your request.",
        "hi": "आपके कहने पर केस {case} रद्द कर दिया गया है।",
        "hinglish": "Aapke kehne par case {case} cancel kar diya gaya hai.",
    },
    # What the driver is told while the case waits for someone to pick it up.
    "case_status_open": {
        "en": "Your case {case} has been received by operations and is waiting for someone to pick it up. Updates will appear here in this chat.",
        "hi": "आपका केस {case} ऑपरेशंस टीम को मिल गया है और किसी के उठाने का इंतज़ार कर रहा है। अपडेट इसी चैट में दिखेंगे।",
        "hinglish": "Aapka case {case} operations team ko mil gaya hai aur kisi ke uthane ka intezaar kar raha hai. Updates isi chat mein dikhenge.",
    },
    # What the driver is told while a coordinator is handling the case.
    "case_status_claimed": {
        "en": "Your case {case} is being handled by the operations team. Updates will appear here in this chat.",
        "hi": "आपका केस {case} ऑपरेशंस टीम देख रही है। अपडेट इसी चैट में दिखेंगे।",
        "hinglish": "Aapka case {case} operations team dekh rahi hai. Updates isi chat mein dikhenge.",
    },
    # Added only when operations has approved a response time.
    "sla_suffix": {
        "en": " Operations aims to respond within {minutes} minutes.",
        "hi": " ऑपरेशंस टीम {minutes} मिनट के भीतर जवाब देने का लक्ष्य रखती है।",
        "hinglish": " Operations team {minutes} minute ke andar jawab dene ka lakshya rakhti hai.",
    },
    # Partner-warehouse requests. Wording follows the stored request state; a pending request is never described as booked.
    "partner_pending": {
        "en": "I have asked {facility} for a dock slot at {time} on {date}. This is only a request until they reply, so I am waiting for their answer. Your current appointment is unchanged. I will tell you here as soon as they reply.",
        "hi": "मैंने {facility} से {date} को {time} का डॉक स्लॉट माँगा है। जब तक उनका जवाब नहीं आता यह सिर्फ़ एक अनुरोध है, इसलिए मैं उनके जवाब का इंतज़ार कर रहा हूँ। आपकी मौजूदा अपॉइंटमेंट जैसी थी वैसी ही है। जवाब आते ही मैं यहीं बताऊँगा।",
        "hinglish": "Maine {facility} se {date} ko {time} ka dock slot manga hai. Jab tak unka jawab nahi aata yeh sirf ek request hai, isliye main unke jawab ka intezaar kar raha hoon. Aapki maujooda appointment jaisi thi waisi hi hai. Jawab aate hi main yahin bataunga.",
    },
    "partner_approved": {
        "en": "{facility} has approved your slot at {time} on {date}. Your earlier appointment has been cancelled.",
        "hi": "{facility} ने {date} को {time} का आपका स्लॉट मंज़ूर कर दिया है। आपकी पिछली अपॉइंटमेंट रद्द कर दी गई है।",
        "hinglish": "{facility} ne {date} ko {time} ka aapka slot manzoor kar diya hai. Aapki pichhli appointment cancel kar di gayi hai.",
    },
    "partner_declined": {
        "en": "{facility} could not offer the {time} slot on {date}. Your original appointment is unchanged.",
        "hi": "{facility} {date} को {time} का स्लॉट नहीं दे सका। आपकी मूल अपॉइंटमेंट जैसी थी वैसी ही है।",
        "hinglish": "{facility} {date} ko {time} ka slot nahi de saka. Aapki original appointment jaisi thi waisi hi hai.",
    },
    "partner_expired": {
        "en": "{facility} did not reply in time about the {time} slot on {date}. Your original appointment is unchanged.",
        "hi": "{facility} ने {date} को {time} के स्लॉट पर समय पर जवाब नहीं दिया। आपकी मूल अपॉइंटमेंट जैसी थी वैसी ही है।",
        "hinglish": "{facility} ne {date} ko {time} ke slot par samay par jawab nahi diya. Aapki original appointment jaisi thi waisi hi hai.",
    },
    "partner_failed": {
        "en": "I could not complete the request to {facility} for {time} on {date}. A coordinator has been asked to help. Your original appointment is unchanged.",
        "hi": "मैं {date} को {time} के लिए {facility} से अनुरोध पूरा नहीं कर सका। एक कोऑर्डिनेटर से मदद माँगी गई है। आपकी मूल अपॉइंटमेंट जैसी थी वैसी ही है।",
        "hinglish": "Main {date} ko {time} ke liye {facility} se request poori nahi kar saka. Ek coordinator se madad maangi gayi hai. Aapki original appointment jaisi thi waisi hi hai.",
    },
    # Replaces model text that quoted the system prompt.
    "prompt_leak": {
        "en": "I cannot share that. I can help with your delivery slot: tell me what is happening.",
        "hi": "मैं यह साझा नहीं कर सकता। मैं आपके डिलीवरी स्लॉट में मदद कर सकता हूँ: बताइए क्या हो रहा है।",
        "hinglish": "Main yeh share nahi kar sakta. Main aapke delivery slot mein madad kar sakta hoon: bataiye kya ho raha hai.",
    },
}


# Look up a wording and fill in its fields. An unknown language falls back to English.
def say(key: str, lang: str | None, **fields: str) -> str:
    # All language variants of this wording.
    variants = T[key]
    return variants.get(lang or "en", variants["en"]).format(**fields)


# ---- claim detection (used by the output guard) -----------------------------------------------------
# Patterns that detect model text CLAIMING a booking is confirmed, in English, Hinglish and Hindi.
# The first pattern excludes 'booked by someone else', which describes another party's booking.
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
# Patterns that detect model text claiming a slot is being held.
_HOLD_CLAIMS = [re.compile(p, re.IGNORECASE) for p in (
    r"\bi('ve|\s+have)\s+(placed\s+a\s+)?held\b", r"\bi\s+am\s+holding\b", r"\bi'm\s+holding\b",
    r"\b(slot|it|that)\s+(is|has\s+been)\s+(being\s+)?held\b", r"\bheld\s+(it\s+)?for\s+you\b",
    r"\bhold\s+kar\s+(liya|diya)\b", r"\bhold\s+kiya\s+hai\b", r"होल्ड\s+(कर\s+लिया|कर\s+दिया|किया\s+है)",
    r"रोक\s+लिया",
)]


# "Nothing has been booked or held" and "I have not confirmed anything" deny a booking; they must not count as claiming one.
# A denial such as 'nothing has been booked' or 'I have not confirmed' must not count as a claim, so denials are removed before matching.
_NEGATED = re.compile(r"\b(nothing|not|never|no|isn't|hasn't|haven't|wasn't)\b[^.!?\n]{0,40}?\b(confirmed|booked|held|holding)\b", re.IGNORECASE)


# True if the text claims a confirmed or booked appointment (after removing denials).
def claims_confirmed(text: str) -> bool:
    # Replace each denial with a space.
    text = _NEGATED.sub(" ", text)
    return any(p.search(text) for p in _CONFIRM_CLAIMS)


# True if the text claims a hold on a slot (after removing denials).
def claims_held(text: str) -> bool:
    text = _NEGATED.sub(" ", text)
    return any(p.search(text) for p in _HOLD_CLAIMS)
