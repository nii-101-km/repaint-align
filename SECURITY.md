# Security and privacy

Repaint Align runs on a trusted, single-user Linux desktop. It controls Audacity with the privileges of the current user. It is not designed for a public server, shared hosting, remote access, or an untrusted user account. Do not proxy or expose its HTTP port to other machines.

## Local interface

The HTTP server binds only to `127.0.0.1`. API requests require a random per-process token, an exact Host header, and a same-origin Origin when present. Duplicate security headers are rejected. The initial token is passed in the URL fragment, removed from the address bar, and retained in per-tab session storage. It is not sent in HTTP URLs. Treat the printed launch link as a credential; do not share terminal screenshots containing it. Browser session restoration or extensions may retain/access browser state.

The frontend has no external scripts, fonts, analytics, or remote requests. Clip names are rendered as text. A strict Content Security Policy blocks inline scripts, framing, and external content. HTTP request logging is disabled. Request bodies, scripting responses, and optional diagnostic trace size are bounded. The trace is disabled by default.

These controls do not defend against malicious software running as the same OS user or a compromised browser. The scripting module itself remains a powerful local control interface independent of this application.

## Files and memory

Audacity's fixed FIFO paths are checked for owner, type, permissions, hard links, and symlinks. A checked owner-only advisory lock prevents cooperating Repaint Align clients from sharing the pipe. Other scripting clients may ignore that lock; do not run them concurrently.

Audio reads use owner-private temporary directories, removed on normal completion and handled exceptions. Abrupt termination, OS crashes, swap, backups, and filesystem recovery can retain data. Cleanup is not secure erasure. Selected samples and fingerprints remain in process memory until cleared/replaced or the process exits. Whole clips are loaded for validation, so memory consumption depends on recording length. Full-clip hashing avoids making an extra full-size bytes copy.

The installed helper macro and ownership marker are owner-only text files. Apply replaces the helper atomically and resets it in a `finally` block. A forced kill can interrupt the reset; reinstall the helper before continuing. Never invoke it manually. It is not a downloaded binary module.

## Applying changes

Analysis does not move clips. A current one-use result identifier is required for Apply; a failed or uncertain Apply cannot be retried with the same identifier. Stale captures, ambiguous clips, possible neighboring overlaps, and unexpected project changes are refused. The app verifies the target's full audio fingerprint and the other clips' positions after moving it. These checks cannot make Audacity scripting transactional against concurrent edits. Keep one project open and leave the editor idle during operations.

The application never reads or writes the internal Audacity project database and never automatically closes Audacity. Timeouts poison the current scripting session rather than replaying commands. Inspect the actual project before restarting.

## Publication hygiene and reporting

The repository excludes recordings, Audacity projects, private research, logs, environment files, credentials, and local configuration. The test suite uses synthetic signals. Dependency vulnerability checks and static analysis are useful checks, not proof that software has no vulnerabilities or leaks.

Report suspected issues using GitHub private vulnerability reporting if enabled on this repository. Avoid public reports containing audio, project names, local paths, launch tokens, crash archives, or full scripting transcripts. Provide a minimal synthetic reproduction and version details where possible.
