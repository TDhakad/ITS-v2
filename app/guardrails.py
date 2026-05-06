from __future__ import annotations

import re

from app.schemas import AnswerValidation, GuardrailDecision, Priority

PROMPT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(ignore|override|forget)\b.{0,80}\b(system|developer|previous)\b", re.I
    ),
    re.compile(
        r"\b(reveal|print|dump|show)\b.{0,80}\b(system prompt|hidden prompt|developer message)\b",
        re.I,
    ),
    re.compile(r"\bexfiltrate\b|\bjailbreak\b|\bprompt injection\b", re.I),
)

UNAUTHORIZED_ACCESS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(bypass|disable)\b.{0,80}\b(mfa|2fa|approval|access control|security)\b",
        re.I,
    ),
    re.compile(
        r"\b(give|grant|escalate)\b.{0,80}\b(admin|root|production|privileged)\b", re.I
    ),
    re.compile(
        r"\b(api key|secret|token|password|private key)\b.{0,80}\b(show|send|share|print|dump)\b",
        re.I,
    ),
)

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{8,}"
    ),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        re.S,
    ),
)

PII_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[redacted-ssn]"),
    (re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"), "[redacted-email]"),
    (
        re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"),
        "[redacted-phone]",
    ),
)


def evaluate_input_safety(text: str) -> GuardrailDecision:
    detected: list[str] = []
    for pattern in PROMPT_INJECTION_PATTERNS:
        if pattern.search(text):
            detected.append("prompt_injection")
            break
    for pattern in UNAUTHORIZED_ACCESS_PATTERNS:
        if pattern.search(text):
            detected.append("unauthorized_access")
            break

    if "prompt_injection" in detected:
        return GuardrailDecision(
            is_safe=False,
            risk_level=Priority.CRITICAL,
            authorization_required=True,
            can_self_resolve=False,
            reason="The request appears to contain prompt injection or hidden-instruction extraction.",
            detected_patterns=detected,
        )
    if "unauthorized_access" in detected:
        return GuardrailDecision(
            is_safe=False,
            risk_level=Priority.HIGH,
            authorization_required=True,
            can_self_resolve=False,
            reason="The request appears to require privileged access or security approval.",
            detected_patterns=detected,
        )
    return GuardrailDecision(
        is_safe=True,
        risk_level=Priority.LOW,
        authorization_required=False,
        can_self_resolve=True,
        reason="No local guardrail pattern matched.",
        detected_patterns=[],
    )


def redact_sensitive_text(text: str) -> tuple[str, list[str]]:
    findings: list[str] = []
    redacted = text
    for pattern in SECRET_PATTERNS:
        if pattern.search(redacted):
            findings.append("secret")
            redacted = pattern.sub("[redacted-secret]", redacted)
    for pattern, replacement in PII_PATTERNS:
        if pattern.search(redacted):
            findings.append(replacement.strip("[]"))
            redacted = pattern.sub(replacement, redacted)
    return redacted, findings


def local_output_validation(text: str) -> AnswerValidation:
    redacted, findings = redact_sensitive_text(text)
    contains_secrets = "secret" in findings
    contains_pii = any(item != "secret" for item in findings)
    return AnswerValidation(
        is_safe=not contains_secrets and not contains_pii,
        contains_pii=contains_pii,
        contains_secrets=contains_secrets,
        sanitized_answer=redacted,
        reason="Local regex output validation completed.",
    )
