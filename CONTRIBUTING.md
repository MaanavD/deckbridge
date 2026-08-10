# Contributing

Thanks for improving Deckbridge.

1. Fork the repository and create a focused branch.
2. Keep machine-specific values in `deckbridge.conf` or
   `~/.deckbridge/apps.json`; never commit credentials, private channel IDs,
   local paths, logs, or screenshots containing task names.
3. Add or update regression tests for behavior changes.
4. Run `./run_tests.sh` before opening a pull request.
5. Explain the user-visible change and any macOS permissions needed to test it.

Small, self-contained pull requests are easiest to review. Integration changes
should degrade cleanly when the relevant application or service is absent.
