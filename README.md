# Session Relay

A local Windows app that monitors selected Orca and Codex Desktop conversations, switches between your saved accounts when usage runs low, and continues the original conversation or its existing Goal.

Account checks run **every 1 minute**. Chat monitoring runs continuously, with a five-second pause between completed cycles. Quota checks and application recovery can take longer than that pause. A detected usage/login failure starts recovery without waiting for the next background refresh.

## Install on a Windows PC

Download the Windows x64 ZIP from [Releases](https://github.com/Hambozo17/session-relay/releases), extract the whole folder, and run the included installer. See [installation instructions](docs/INSTALL.md) for the executable command and requirements.

The standalone distribution bundles the relay runtime. Orca, Codex Desktop, Codex CLI, and your saved account logins are separate prerequisites. Install and sign in to the applications you intend to automate. You do not have to use both applications.

Open **Session Relay** from your Desktop or Start menu, select conversations using their Monitor checkboxes, then enable Orca or Codex. Each control manages its own application. Search supports selecting multiple conversations without losing hidden selections. Saved conversations can be selected before they are opened; open them in the relevant application to attach their live owner.

Fresh installations start with both automation controls disabled. Closing the panel leaves monitoring running. Disable the relevant control to stop automation. Windows sign-out, shutdown, loss of connectivity, or a required interactive sign-in can prevent recovery.

## Recovery behavior

- At the default **2%** threshold, or after a verified usage/login failure, check saved accounts and select a usable destination. Prefer the greatest five-hour allowance on a separate account; share the other application's account only when needed and ownership is verified.
- Preserve the original conversation ID, model, reasoning effort, permissions, and existing Goal identity, budget, and accounting.
- Continue the existing active Goal, or send one continuation when there is no Goal. Recognize automatic Goal resumption to avoid a duplicate prompt.
- If all accounts are depleted, keep the selected chats queued, wait for allowance to return, and verify it before recovery. The original account can be reused after its reset, including when it is the only saved account.
- Remember verified quota failures across relay restarts. Record allowance windows to avoid repeated continuations against stale quota readings. An uncertain submission stays blocked for review.
- Recheck failed saved sign-ins every minute. A failed login is not usable until a fresh check succeeds. The relay does not perform a new browser sign-in.

By default, manual stops, approval requests, paused/completed Goals, independent Goal budget limits, and unrelated conversations remain stopped. Proxy-provider limits do not trigger an OpenAI account switch. Automatic wait-and-retry for proxy quotas is not implemented.

In Automation preferences, **Send Continue when selected Codex chats stop** enables continued work without requiring a Goal. It can continue idle or blocked work, with three minutes between retries. Running turns, manual stops, paused Goals, budget limits and approval requests remain protected. Automatic continuation messages do not grant human approval. Disable this option or Codex monitoring to stop the loop.

Orca recovery uses its local authenticated runtime and original terminal pane. Codex Desktop recovery may need a normal application restart; it checks other running conversations and approvals first. Both integrations depend on the installed applications' local protocols. An incompatible or unverifiable protocol stops recovery with a status message.

Unsupported source permission policies stop recovery before the original chat is interrupted. Supported Orca sandbox restrictions are set explicitly during resume and checked against the new turn's recorded settings.

Optional included usage resets are **disabled by default**. They require fresh checks confirming zero weekly allowance on every saved account. Five-hour exhaustion alone never authorizes a reset. No paid credits are purchased.

## Privacy

Credentials remain on your PC. The relay reads existing local account stores and uses the applications' local IPC interfaces. It does not upload chats or credentials to a relay service. Native account checks still contact the account provider through Codex.

Local settings, account caches, recovery receipts, diagnostics, and logs live under `%LOCALAPPDATA%\SessionRelay`. Authentication backups use Windows DPAPI. Do not publish that directory or attach raw runtime files to a public issue. This repository and release archives exclude personal account stores and conversation history.

## Run from source

Use Windows x64 with Python 3.11, Node.js on PATH, and the relevant signed-in applications:

```powershell
python -m pip install -r requirements.txt
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

The installer creates per-user startup tasks and shortcuts. It preserves an existing configuration. Runtime changes require a graceful relay-worker upgrade; editing source files alone does not reload an already-running worker.

Run the checks:

```powershell
python -m unittest discover -q
node --test test_bridge.cjs
```

Build the standalone Windows distribution:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

The build produces a Windows x64 ZIP and SHA-256 checksums in `dist`. Builds are unsigned unless a distributor adds code signing; Windows may show an unknown-publisher prompt.

## Support and license

Windows PCs are the supported target for this release. macOS, Linux, mobile devices, and unattended work across sign-out are not supported. This project is independent of Orca and OpenAI, and is not endorsed by either.

The relay's code is licensed under [MIT](LICENSE), copyright 2026 Hambozo17. Third-party runtimes retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.md). MIT includes a warranty and liability disclaimer; it does not grant rights to third-party products or services.
