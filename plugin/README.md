# DSH WeChat Bridge plugin

Host-only DSH plugin. It exposes loopback-only routes and injects messages into a
fixed DSH session.

## Routes

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/dsh-wechat-bridge/health` | none | liveness |
| GET | `/dsh-wechat-bridge/status` | Bearer | session/lastSeq |
| POST | `/dsh-wechat-bridge/inbound` | Bearer | submit a message |
| GET | `/dsh-wechat-bridge/events?since=&timeout=` | Bearer | long-poll events |
| GET | `/dsh-wechat-bridge/attachment/:id` | Bearer | fetch attachment |

The plugin writes `runtime.json` and a random `token` under
`%USERPROFILE%\.dsh\dsh-wechat-bridge\`. Never commit these files.

## Install

From the repo root:

```bat
dsh.cmd plugin --profile desktop add <path-to-repo>\plugin
```

`cwd`/`sessionTitle`/`sessionId` can be overridden in `cordis.patch.yml` or the
DSH plugin config. If `sessionId` is empty, the plugin reuses/creates the
configured title.

## Security

Loopback only; Bearer token required for all data routes. Do not expose this
plugin through a reverse proxy.
