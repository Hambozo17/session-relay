# Security and privacy

Do not post credentials, authentication files, account emails, full chat transcripts, or raw `%LOCALAPPDATA%\SessionRelay` contents in public issues.

For a suspected security problem, use GitHub's private vulnerability reporting feature if available on this repository. Otherwise open a minimal issue asking for a private reporting channel without including exploit details or personal data.

Useful non-secret diagnostics include the relay version, Windows version, affected application version, recovery phase, exception type, and a description of the behavior. Inspect screenshots and logs before sharing them.

The app reads saved credentials locally, and account probes can refresh existing logins. Its desktop integrations use local interfaces that can change between application versions. Recovery refuses uncertain ownership, settings changes, approvals, manual stops, and uncertain sends instead of replaying an action blindly.
