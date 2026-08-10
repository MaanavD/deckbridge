# Security Policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature for this
repository. Do not open a public issue for a suspected credential leak or a
vulnerability that could expose local application data.

## Local data

Deckbridge can read local agent state, application titles, Discord metadata,
and user-supplied configuration. It is designed to bind its control hub to
loopback. Do not expose the WebSocket or emulator ports to an untrusted network.

Keep tokens and private routes outside the repository. Before attaching logs to
an issue, review them for task names, local paths, hostnames, and channel IDs.
