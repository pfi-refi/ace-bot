"""Safe provider failure descriptions and bounded retry policy; never expose credentials."""

def classify(error):
    text = str(error).lower()
    if "credit balance is too low" in text or "insufficient_credit" in text:
        return "billing", "My AI provider is rejecting requests because Ace's API credit balance is too low. Your message was saved, but I couldn't finish this request."
    status = getattr(error, "status_code", None)
    if status in (401, 403):
        return "authentication", "My AI provider connection needs attention. Your message was saved, but I couldn't finish this request."
    if status == 429:
        return "rate_limit", "My AI provider is temporarily rate-limiting requests. Your message was saved; please try again shortly."
    return "unavailable", "My AI provider did not finish this request. Your message was saved; I can't confirm an answer or completed action."


def retry_delay(error, failures):
    kind, _ = classify(error)
    return 900 if kind in ("billing", "authentication") else min(900, 30 * 2 ** min(failures, 5))
