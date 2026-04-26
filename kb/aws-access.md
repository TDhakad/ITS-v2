---
kb_id: aws-access-request
title: How to Get AWS Access
category: Infra
clearance: internal
app_name: aws
environment: production
audience: employees
department: engineering
tags: [aws, cloud, access, sso, permissions]
updated: 2026-04-26
---

# How to Get AWS Access

Use this article when a user needs access to AWS accounts, AWS SSO, or a cloud
role used by an engineering or operations team.

AWS access is granted through the identity portal and requires manager approval.
Users should not ask another employee to share credentials, access keys, or a
temporary session.

## Request Process

1. Open the IT service portal.
2. Choose **Cloud Access Request**.
3. Select the AWS account, business justification, requested role, and expected
   duration.
4. Add the manager or project owner as the approver.
5. Submit the request and wait for approval before attempting to sign in.

## Access Requirements

The user must have an active company account, enrolled MFA, and a valid business
reason for the requested AWS account. Production roles require a separate
approval from the service owner.

Access is assigned using least privilege. If a user only needs to view logs,
grant a read-only or observability role instead of an administrator role.

## Common Issues

If AWS SSO does not show the expected account, confirm that the access request
was approved and that the user has signed out and back in to the SSO portal.

If the user receives an MFA error, follow the MFA recovery article before
escalating the AWS request.

## Escalation

Escalate to cloud operations when an approved production role is missing after
one business day, the wrong AWS account was assigned, or the request involves
break-glass access.
