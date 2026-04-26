---
title: VPN Connection Troubleshooting
category: network
audience: all
department: all
clearance_level: public
tags: [vpn, remote-access, network, connectivity]
updated: 2026-04-25
---

# VPN Connection Troubleshooting

Use this guide when a user cannot connect to VPN, disconnects repeatedly, or can
connect but cannot reach internal resources.

## Initial Checks

Confirm the user has working internet access outside the VPN. Ask them to browse
to a public website and check whether other household or office network devices
are online.

Confirm the VPN client is current. If the client is out of date, have the user
install the approved version from the software portal before deeper
troubleshooting.

## Common Fixes

Have the user fully quit and reopen the VPN client. If the client reports cached
credential errors, ask the user to sign out of the client and authenticate
again.

If VPN connects but internal sites fail, ask for one example hostname and verify
whether the user is on a split-tunnel profile. DNS failures usually show as a
browser error that says the site cannot be found.

## Escalation

Escalate to network support if multiple users report failures at the same time,
the VPN gateway status page shows degraded service, or the user can connect but
cannot reach any internal network after a client reinstall.
