# Changelog

## 1.0.22

- Resolve a removed Codex Store installation again after an update instead of retaining its obsolete path.
- Retire an old confirmed pause when newer native work has already resumed or failed, and recover confirmed relay pauses without requiring a Goal.
- Retry transient owner-discovery reads at lower concurrency so large saved histories can complete verification before account recovery.
- Refresh delayed native snapshots and retain authorization for chats preserved during a handoff.
- Reopen enabled selected chats after Codex or Windows restarts, with persistent navigation retries and no automatic prompt for a missing owner.
- Retry another account after an exact-message continuation is natively confirmed to have failed on usage or login; retain unconfirmed-send protection.

## 1.0.21

- Recognize verified Git main/worktree root aliases under full access after Codex reopens, while preserving the raw settings and restricted permission checks.
- Retry original-thread reopening after a pre-submission startup failure, and reconcile later verified native turns without replaying an old continuation.
- Prioritize selected and recently active conversations for monitoring; app-wide recovery still verifies the full inventory before a restart.

## 1.0.20

- Preserve a running turn when its saved Goal still says usage limited and fresh allowance is healthy. Continue monitoring the quota threshold and recover when the turn stops for usage or login failure.

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
