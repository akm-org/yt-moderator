import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from app.utils import clamp


URL_RE = re.compile(r"https?://|www\.|(?:[a-z0-9-]+\.)+[a-z]{2,}", re.IGNORECASE)
REPEATED_CHAR_RE = re.compile(r"(.)\1{5,}", re.IGNORECASE)
PUNCT_RE = re.compile(r"[!?.,;:]{5,}")
SYMBOL_RE = re.compile(r"[$#@%^&*_+=|~<>]{5,}")
EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001f5ff"
    "\U0001f600-\U0001f64f"
    "\U0001f680-\U0001f6ff"
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001faff"
    "\u2600-\u27bf"
    "]",
    re.UNICODE,
)

SCAM_TERMS = {
    "giveaway",
    "airdrop",
    "double your",
    "free crypto",
    "whatsapp me",
    "telegram me",
    "investment",
    "elon musk",
    "claim prize",
    "limited offer",
    "send btc",
    "send eth",
    "recover wallet",
    "seed phrase",
    "private key",
}

CRYPTO_TERMS = {
    "bitcoin",
    "btc",
    "ethereum",
    "eth",
    "usdt",
    "wallet",
    "binance",
    "coinbase",
    "solana",
    "nft",
}

INVITE_PATTERNS = {
    "discord.gg",
    "t.me/",
    "telegram.me",
    "chat.whatsapp.com",
    "wa.me/",
}

PROMOTION_TERMS = {
    "subscribe to my",
    "check my channel",
    "follow me",
    "my channel",
    "my stream",
    "watch my video",
}

PROFANITY = {
    "fuck",
    "shit",
    "bitch",
    "asshole",
    "bastard",
    "slut",
    "whore",
}


@dataclass
class SpamResult:
    score: int
    reasons: list[str] = field(default_factory=list)
    features: dict[str, Any] = field(default_factory=dict)
    keyword_alerts: list[str] = field(default_factory=list)


class SpamDetector:
    """Fast, local scoring for messages before AI moderation."""

    def __init__(self) -> None:
        self.recent_messages: dict[str, deque[tuple[float, str]]] = defaultdict(lambda: deque(maxlen=30))

    def score(
        self,
        message: str,
        *,
        channel_id: str,
        username: str,
        settings: dict[str, Any],
    ) -> SpamResult:
        text = message.strip()
        lowered = text.lower()
        score = 0
        reasons: list[str] = []
        features: dict[str, Any] = {}
        alerts: list[str] = []
        now = time.monotonic()
        history = self.recent_messages[channel_id or username]

        if not text:
            return SpamResult(score=0)

        if len(text) > 350:
            score += 10
            reasons.append("very long message")

        if self._has_repeated_message(history, lowered):
            score += 28
            reasons.append("repeated message")

        repeated_char_count = len(REPEATED_CHAR_RE.findall(text))
        if repeated_char_count:
            score += 18
            reasons.append("repeated characters")
            features["repeated_char_count"] = repeated_char_count

        emoji_count = len(EMOJI_RE.findall(text))
        emoji_limit = int(settings.get("emoji_limit", 8))
        if emoji_count > emoji_limit:
            score += min(25, (emoji_count - emoji_limit) * 3)
            reasons.append("emoji spam")
        features["emoji_count"] = emoji_count

        symbol_ratio = self._ratio(SYMBOL_RE.findall(text), text)
        if SYMBOL_RE.search(text) or symbol_ratio > 0.12:
            score += 12
            reasons.append("excessive symbols")

        caps_ratio = self._caps_ratio(text)
        caps_limit = float(settings.get("caps_limit", 0.72))
        if len(text) >= 12 and caps_ratio > caps_limit:
            score += 18
            reasons.append("all caps")
        features["caps_ratio"] = round(caps_ratio, 3)

        if PUNCT_RE.search(text):
            score += 12
            reasons.append("excessive punctuation")

        burst_score = self._flood_score(history, now)
        if burst_score:
            score += burst_score
            reasons.append("flooding")
        features["messages_last_10s"] = sum(1 for ts, _ in history if now - ts <= 10)

        if bool(settings.get("link_filter", True)) and URL_RE.search(lowered):
            score += 12
            reasons.append("contains link")

        if any(pattern in lowered for pattern in INVITE_PATTERNS):
            score += 22
            reasons.append("invite link")

        scam_hits = [term for term in SCAM_TERMS if term in lowered]
        if scam_hits:
            score += 24 + min(16, len(scam_hits) * 4)
            reasons.append("scam language")
            features["scam_terms"] = scam_hits

        crypto_hits = [term for term in CRYPTO_TERMS if term in lowered]
        if crypto_hits and (URL_RE.search(lowered) or scam_hits):
            score += 18
            reasons.append("crypto scam pattern")
            features["crypto_terms"] = crypto_hits

        if any(term in lowered for term in PROMOTION_TERMS):
            score += 18
            reasons.append("self promotion")

        if bool(settings.get("profanity_filter", True)):
            profanity_hits = self._word_hits(lowered, PROFANITY)
            if profanity_hits:
                score += 15
                reasons.append("profanity")
                features["profanity"] = profanity_hits

        blacklist = set(settings.get("blacklist", []) or [])
        whitelist = set(settings.get("whitelist", []) or [])
        keyword_alerts = set(settings.get("keyword_alerts", []) or [])

        if any(item and item in lowered for item in whitelist):
            score = max(0, score - 40)
            reasons.append("whitelist match")

        black_hits = [item for item in blacklist if item and item in lowered]
        if black_hits:
            score += 50
            reasons.append("blacklist match")
            features["blacklist_hits"] = black_hits

        alert_hits = [item for item in keyword_alerts if item and item in lowered]
        if alert_hits:
            alerts.extend(alert_hits)
            reasons.append("keyword alert")
            features["keyword_alerts"] = alert_hits

        sensitivity = float(settings.get("spam_sensitivity", 1.0))
        final_score = int(clamp(round(score * sensitivity), 0, 100))

        history.append((now, lowered))
        return SpamResult(
            score=final_score,
            reasons=list(dict.fromkeys(reasons)),
            features=features,
            keyword_alerts=alerts,
        )

    @staticmethod
    def _has_repeated_message(history: deque[tuple[float, str]], lowered: str) -> bool:
        recent = [msg for ts, msg in history if time.monotonic() - ts <= 120]
        return lowered in recent[-5:]

    @staticmethod
    def _flood_score(history: deque[tuple[float, str]], now: float) -> int:
        last_10 = sum(1 for ts, _ in history if now - ts <= 10)
        last_30 = sum(1 for ts, _ in history if now - ts <= 30)
        if last_10 >= 6:
            return 35
        if last_30 >= 10:
            return 25
        if last_10 >= 4:
            return 18
        return 0

    @staticmethod
    def _caps_ratio(text: str) -> float:
        letters = [ch for ch in text if ch.isalpha()]
        if not letters:
            return 0
        upper = [ch for ch in letters if ch.isupper()]
        return len(upper) / len(letters)

    @staticmethod
    def _ratio(matches: list[str], text: str) -> float:
        if not text:
            return 0
        matched = sum(len(item) for item in matches)
        return matched / len(text)

    @staticmethod
    def _word_hits(lowered: str, words: set[str]) -> list[str]:
        tokens = set(re.findall(r"[a-z0-9']+", lowered))
        return sorted(tokens.intersection(words))

