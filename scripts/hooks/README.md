# Git hooks — secret guard

Two layers keep credentials out of this repo:

1. **`pre-commit`** (local) — refuses a commit whose staged additions look like
   a private key, a provider token, a Tailscale auth URL, a hardcoded
   `secret = value`, or a file that should never be tracked (AI logs, `*.key`,
   `*.env`, …). It is the first line and runs before anything is committed.

2. **`.github/workflows/secret-scan.yml`** (CI) — runs `gitleaks` over each
   push/PR as a server-side backstop.

## Enable the local hook (once per clone)

```
git config core.hooksPath scripts/hooks
```

That points Git at this directory for hooks. Verify with
`git config core.hooksPath` (should print `scripts/hooks`).

## Notes

- A genuine false positive can be committed with `git commit --no-verify` — but
  fix the rule if it keeps mis-firing rather than making bypass a habit.
- Neither layer un-exposes a secret that already reached git or a CI runner.
  If a real credential is caught, **rotate it** — removal from history is
  cleanup, not a fix.
