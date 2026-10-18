# DSH WeChat Bridge

[中文主页](README.md) | **English**

A Windows-only bridge that forwards messages from a fixed WeChat chat window to a
fixed DeepSeek Harness (DSH) session, then sends DSH replies (text / attachments)
back to that WeChat chat.

> **Status: developer preview.** Extracted from a working private deployment.
> It is not a turnkey business product and does not include any order/customer
> scripts. The WeChat automation depends on a specific WeChat version/window
> layout and must be calibrated on your machine.

## What it is

```
WeChat window ── screenshot + OCR ──► bridge.py ── loopback HTTP ──► DSH host plugin
      ▲                                  │                              │
      └──────── clipboard / file dialog ─┘◄──── fixed DSH session ◄──────┘
```

- `plugin/`: DSH host plugin (`dsh-wechat-bridge`). It exposes loopback-only
  routes (`/health`, `/status`, `/inbound`, `/events`, `/attachment`) and
  injects messages into one fixed DSH session.
- `bridge/`: Python process that scans the fixed WeChat window every second,
  detects messages from other people, extracts text/images/files (OCR where
  applicable), submits to DSH, and sends DSH results back.
- `bridge_control.pyw`: optional desktop control panel (start/stop Umi-OCR,
  watchdog, and bridge together).

## Requirements

- Windows 10/11, interactive desktop session (not a Windows service).
- WeChat desktop 4.x (tested on 4.1.15.13, Qt 5.15). Window must stay visible;
  minimize breaks the screenshot path.
- [Umi-OCR](https://github.com/hiroi-sora/Umi-OCR) with HTTP API on
  `127.0.0.1:1224`.
- DSH desktop with the host plugin installed in the `desktop` profile.
- Python 3.10+ with `numpy` and `Pillow` for the bridge.
- Node.js only if your DSH session uses Node-based tools (the bridge itself
  does not require Node).

## Install (outline)

1. Copy `config.example.json` to `config.json` and edit:
   - `wechat.title`, `wechat.group`, `wechat.rect`
   - `dsh.runtime_file` / `token_file` (match your DSH profile)
   - `paths.base` / `paths.wechat_files`
   - `prompt.node_exe` / `node_workdir` if you use Node tools
2. Install the plugin. For a local checkout:

   ```bat
   dsh.cmd plugin --profile desktop add <path-to-this-repo>\plugin
   ```

   The plugin writes `runtime.json` and a random token under
   `%USERPROFILE%\.dsh\dsh-wechat-bridge\`.
3. Start Umi-OCR and verify `http://127.0.0.1:1224` responds.
4. Open the target WeChat chat, keep the window visible, then run:

   ```bat
   bridge\run-bridge.cmd
   ```

   For the control panel: `bridge\桥接控制台.vbs` (or `pythonw bridge_control.pyw`).

## Configuration notes

- **Window rect/layout**: values are physical pixels for the tested 125% DPI
  setup. Recalibrate `wechat.layout` and `rect` for your display; see
  `bridge/wechat_io.py`.
- **Safety**: the bridge treats all WeChat content as untrusted data. It never
  follows instructions found in messages. The plugin routes are loopback-only
  with a Bearer token; never expose them through a reverse proxy.
- **No secrets in repo**: `config.json`, `state/`, `logs/`, `*.db`, `token`,
  and `runtime.json` are intentionally ignored.

## Known limitations

- Inbound images are not read automatically in this version (WeChat cache is
  encrypted); the bridge can forward a fallback notice.
- Merged-forward cards are detected but not expanded; voice/video are ignored.
- WeChat UI changes, DPI changes, or window minimization can break scanning.
- The plugin depends on DSH's internal `sessionController`/`webServer` APIs;
  DSH upgrades may require changes.
- This is a personal automation project. Automating WeChat may violate platform
  terms or trigger risk controls; use a test account/group first.

## Disclaimer

This is an **unofficial personal project**. It is not affiliated with, endorsed
by, or supported by Tencent, WeChat, DeepSeek, or DSH. Use it at your own risk.

- Automating WeChat may violate its terms of service and can lead to account
  restrictions. Test with a dedicated test account/group first.
- The software is provided **as is**, without warranty of any kind. You are
  responsible for compliance with applicable laws, platform rules, and the
  privacy of the people whose messages you process.
- No customer data, chat history, tokens, or credentials are included in this
  repository. The bridge runs locally and has no telemetry.

See `SECURITY.md` and `PRIVACY.md` for details.

## Privacy

- The bridge processes WeChat content locally. Messages are sent only to the
  DSH instance you configure; this project does not upload them anywhere else.
- Do not commit `config.json`, `state/`, `logs/`, `*.db`, `token`, or
  `runtime.json`. They are in `.gitignore`.
- If you publish logs/screenshots in an issue, redact names, phone numbers,
  order numbers, and chat content first.

## License

MIT. See `LICENSE`.
