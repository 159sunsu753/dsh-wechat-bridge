# Security policy

## Scope

This project runs a local-only bridge between WeChat and a DSH session. It is
designed for a single-user Windows desktop and is **not** hardened for
multi-user or internet-facing deployment.

## Default protections

- The DSH plugin binds only to loopback.
- All bridge routes require `Authorization: Bearer <token>`.
- The token is generated with 32 random bytes and stored next to `runtime.json`;
  it is not committed to the repository.
- The bridge does not execute instructions found in WeChat messages.

## Do not

- Do not expose the bridge port through a reverse proxy, tunnel, or firewall
  rule.
- Do not put `config.json`, `runtime.json`, tokens, chat databases, or customer
  data into Git issues/screenshots.
- Do not run the bridge from a Windows service/session 0; UI automation requires
  the interactive desktop.

## Reporting

Open a private security advisory or contact the maintainer listed in the
repository. Include the version/commit and a minimal reproduction; redact
tokens, phone numbers, order numbers, and chat content.
