from app.spam import SpamDetector


def test_repeated_crypto_scam_scores_high():
    detector = SpamDetector()
    settings = {
        "emoji_limit": 8,
        "caps_limit": 0.72,
        "link_filter": True,
        "profanity_filter": True,
        "spam_sensitivity": 1.0,
        "blacklist": [],
        "whitelist": [],
        "keyword_alerts": [],
    }
    result = detector.score(
        "FREE CRYPTO GIVEAWAY send BTC now https://example.com !!!!!",
        channel_id="abc",
        username="scammer",
        settings=settings,
    )
    assert result.score >= 70
    assert "scam language" in result.reasons


def test_normal_message_scores_low():
    detector = SpamDetector()
    settings = {
        "emoji_limit": 8,
        "caps_limit": 0.72,
        "link_filter": True,
        "profanity_filter": True,
        "spam_sensitivity": 1.0,
        "blacklist": [],
        "whitelist": [],
        "keyword_alerts": [],
    }
    result = detector.score(
        "Great stream today, thanks for explaining that part.",
        channel_id="abc",
        username="viewer",
        settings=settings,
    )
    assert result.score < 20

