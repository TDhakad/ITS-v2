---
kb_id: infrastructure-architectures
title: Infrastructure Architectures
category: Infra
clearance: internal
app_name: general
environment: production
audience: employees
department: engineering
tags: [infrastructure, architecture, network, cloud, diagrams]
updated: 2026-04-26
---

# Infrastructure Architectures

Use this article when a user asks where to find approved infrastructure
architecture diagrams or needs a high-level explanation of service layout.

Architecture documents are internal references. They may describe environments,
service boundaries, dependencies, and support ownership, but they must not
include secrets, private keys, passwords, or unredacted customer data.

## Standard Architecture Layers

Most internal services follow a common structure:

1. User entry through VPN, SSO, or an approved public endpoint.
2. Application traffic routed through a load balancer or API gateway.
3. Service workloads running in managed containers or virtual machines.
4. Data stored in managed databases, queues, object storage, or caches.
5. Logs and metrics forwarded to the observability platform.

## Where to Find Diagrams

Approved diagrams are stored in the engineering documentation workspace under
the owning service or platform area. Draft diagrams should be clearly marked as
draft and reviewed by the service owner before use in incident response or
change planning.

## Support Guidance

For basic questions, direct users to the architecture document for the service
they support. For production troubleshooting, use the current runbook instead of
an older architecture diagram because deployed topology may have changed.

## Escalation

Escalate to platform engineering when a diagram is missing, outdated, or
conflicts with the current deployment runbook.
