---
title: Helpdesk Account Lockout Procedure
category: security
audience: helpdesk
department: it
clearance_level: restricted
tags: [lockout, identity, security, helpdesk]
updated: 2026-04-25
---

# Helpdesk Account Lockout Procedure

Use this restricted procedure when a verified employee account is locked after
repeated failed sign-in attempts.

## Required Verification

Before unlocking the account, verify the user's identity with the approved
callback number or manager confirmation path. Do not rely on caller ID, email
display name, or chat profile alone.

Check recent sign-in events for impossible travel, repeated failures from a new
country, or successful MFA prompts that the user did not initiate. Treat any of
those signals as possible compromise.

## Unlock Steps

If verification passes and no compromise indicators are present, clear the
lockout in the identity admin console. Require a password reset when failures
came from an unknown device or when the user cannot explain the failed attempts.

Document the verification method, admin action, and whether a password reset was
required in the ticket.

## Escalation

Escalate to security operations if there are suspicious sign-in events, the user
reports approving an unexpected MFA prompt, or the account locks again within one
hour of unlock.
