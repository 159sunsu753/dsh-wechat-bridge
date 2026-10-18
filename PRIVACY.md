# Privacy

## Summary

This bridge is local-first. It reads messages from a fixed WeChat window and
forwards them to the DSH instance you configure; it does not include analytics,
telemetry, or a cloud service.

## What is processed

- Text, file names/paths, and screenshots of the configured WeChat chat window.
- OCR output from that window.
- DSH replies and attachments you choose to send back.

## Where it goes

- The DSH host plugin listens on `127.0.0.1` and requires a local Bearer token.
- Message content is sent only to the DSH session configured in `config.json`.
- No data is sent to the maintainer or any third party by this project.

## What is stored

- `state/bridge.db` may contain message metadata/content; `state/logs/` may
  contain event logs; `state/shots/` may contain screenshots.
- `config.json`, `runtime.json`, and `token` are local configuration/secrets.
- These paths are ignored by Git and must not be uploaded.

## Your responsibilities

- Obtain any consent required to process other people's messages.
- Do not publish chat logs, customer names, phone numbers, order numbers,
  addresses, or screenshots without redaction.
- Secure the machine and the DSH instance; anyone with local access to the
  token can submit messages to the configured session.

## Reporting a privacy issue

Open a private security advisory or contact the maintainer. Do not attach
production data; redact first.
