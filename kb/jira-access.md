---
kb_id: jira-access-request
title: How to Get JIRA Access
category: Apps
clearance: internal
app_name: jira
environment: production
audience: employees
department: all
tags: [jira, access, projects, permissions, onboarding]
updated: 2026-04-26
---

# How to Get JIRA Access

Use this article when a user cannot sign in to JIRA, needs access to a project,
or needs a different project permission level.

JIRA access is based on identity groups and project roles. Project
administrators approve access to their own boards and workflows.

## New User Access

1. Confirm the user has an active company account.
2. Ask the user to sign in through single sign-on.
3. If sign-in fails, verify that the user is in the default JIRA users group.
4. For project access, ask the user to provide the project key, team name, and
   business reason.
5. Route the request to the project administrator for approval.

## Permission Levels

Use viewer access for users who only need to read issues and dashboards. Use
contributor access for users who create or update issues. Use project admin
access only when the project owner confirms that the user manages workflows,
components, or releases.

Do not grant global administrator access through a standard helpdesk request.
Global administrator requests must be reviewed by the systems administration
team.

## Common Issues

If the user can sign in but cannot see a board, check whether the board is tied
to a restricted project or filter. If the user can see issues but cannot edit
them, verify the project role and issue security level.

## Escalation

Escalate to the JIRA administration queue when group membership looks correct
but permissions are still missing, a workflow change is requested, or the user
requests global administrator access.
