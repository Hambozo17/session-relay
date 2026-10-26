# Changelog

## 1.0.19

- Refresh saved-account allowances and failed sign-ins every minute, with the actual interval shown in the panel.
- Schedule checks independently so a slow sign-in does not hold an entire refresh batch, and keep scheduling during long recovery operations.
- Recover selected chats after all accounts are exhausted, including reusing the original account after a verified reset.
- Persist verified screen-only quota failures across monitor restarts so a reset does not erase recovery eligibility.
- Record allowance windows and accepted continuations to prevent repeated sends against stale usage readings.
- Preserve supported Orca sandbox restrictions explicitly and verify resumed turn settings.
- Resume Goals without a token budget while preserving their unlimited budget and accounting.
- List saved Orca chats and keep selected detached conversations visible.
- Add a standalone Windows distribution, installer, automated checks, and MIT license.
