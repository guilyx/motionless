# Security Policy

## Supported versions

The latest released version receives security fixes.

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/guilyx/motionless/security/advisories/new)
rather than opening a public issue. You can expect an initial response within
a week.

## Scope notes

A few things are worth knowing about motionless' threat model:

- **The control socket** lives in `$XDG_RUNTIME_DIR/motionless/` (mode 0700,
  owned by you) and is created mode 0600. Anything that can write to it can
  show, hide or stop the overlay — the same power as a process running as you.
- **The `udp` motion source** binds `127.0.0.1` by default. Changing
  `motion.udp.host` to a routable address lets anything on that network move
  your dots. The payload is parsed as JSON or CSV into three floats and
  nothing else, but the exposure is real: only widen the bind address on a
  network you trust.
- **The installer** runs your package manager through `sudo` to install GTK
  bindings. Read it before piping it to a shell — as you should with any
  installer. `--no-deps` skips that step entirely.
