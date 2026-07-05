import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings


LOGGER = logging.getLogger(__name__)

ALLOWED_ACTIONS = {"allow", "warn", "delete", "timeout", "ban"}
ALLOWED_CATEGORIES = {
    "Spam",
    "Toxicity",
    "Harassment",
    "Hate Speech",
    "Adult Content",
    "Violence",
    "Threats",
    "Scam",
    "Phishing",
    "Self Promotion",
}


@dataclass
class GeminiDecision:
    action: str = "allow"
    reason: str = "AI moderation unavailable"
    severity: int = 1
    categories: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0
    status: str = "skipped"


class GeminiModerator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.gemini_api_key)

    async def classify(
        self,
        *,
        username: str,
        message: str,
        spam_score: int,
        spam_reasons: list[str],
    ) -> GeminiDecision:
        if not self.configured:
            return GeminiDecision(reason="GEMINI_API_KEY is not configured")

        prompt = self._build_prompt(
            username=username,
            message=message,
            spam_score=spam_score,
            spam_reasons=spam_reasons,
        )
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.settings.gemini_model}:generateContent"
        )
        params = {"key": self.settings.gemini_api_key}
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }
        start = time.perf_counter()
        last_error = "Unknown Gemini error"
        for attempt in range(self.settings.gemini_max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.settings.gemini_timeout_seconds) as client:
                    response = await client.post(url, params=params, json=payload)
                latency = (time.perf_counter() - start) * 1000
                response.raise_for_status()
                text = self._extract_text(response.json())
                decision = self._parse_decision(text)
                decision.latency_ms = latency
                decision.status = "ok"
                return decision
            except Exception as exc:
                last_error = str(exc)
                LOGGER.warning("Gemini moderation attempt %s failed: %s", attempt + 1, exc)
        return GeminiDecision(
            reason=f"Gemini moderation failed: {last_error}",
            latency_ms=(time.perf_counter() - start) * 1000,
            status="error",
        )

    @staticmethod
    def _build_prompt(username: str, message: str, spam_score: int, spam_reasons: list[str]) -> str:
        return f"""
You are a strict but fair YouTube live-chat moderation engine.
Return ONLY valid minified JSON. Never return markdown. Never explain outside JSON.

Schema:
{{"action":"allow|warn|delete|timeout|ban","reason":"short reason","severity":1-5,"categories":["Spam"]}}

Allowed categories:
Spam, Toxicity, Harassment, Hate Speech, Adult Content, Violence, Threats, Scam, Phishing, Self Promotion

Rules:
- Use allow for normal conversation, jokes, disagreement, and harmless slang.
- Use warn for mild spam, mild toxicity, or borderline self-promotion.
- Use delete for obvious spam, abusive comments, phishing attempts, adult content, or repeated disruption.
- Use timeout for severe abuse, scams, threats, hate speech, coordinated flooding, or repeated offender behavior.
- Use ban only for extreme threats, explicit hate speech, doxxing, malware/phishing, or dangerous scams.

Context:
username={username!r}
local_spam_score={spam_score}
local_spam_reasons={spam_reasons}
message={message!r}
""".strip()

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        candidates = payload.get("candidates") or []
        if not candidates:
            raise ValueError("Gemini returned no candidates")
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        if not parts:
            raise ValueError("Gemini returned no content parts")
        text = "".join(str(part.get("text", "")) for part in parts)
        if not text:
            raise ValueError("Gemini returned empty text")
        return text

    @staticmethod
    def _parse_decision(text: str) -> GeminiDecision:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.replace("json", "", 1).strip()
        data = json.loads(cleaned)
        action = str(data.get("action", "allow")).lower().strip()
        if action not in ALLOWED_ACTIONS:
            action = "allow"
        reason = str(data.get("reason", "No reason provided"))[:500]
        try:
            severity = int(data.get("severity", 1))
        except (TypeError, ValueError):
            severity = 1
        severity = max(1, min(5, severity))
        categories = [
            str(category)
            for category in data.get("categories", [])
            if str(category) in ALLOWED_CATEGORIES
        ]
        return GeminiDecision(
            action=action,
            reason=reason,
            severity=severity,
            categories=categories,
            raw=data,
        )

