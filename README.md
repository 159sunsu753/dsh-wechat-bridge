# DSH 微信桥接

**中文主页** | [English](README.en.md)

一个 **Windows 桌面端** 的小工具：把固定微信群/聊天窗口里的新消息，转发给固定的
DeepSeek Harness（DSH）会话；再把 DSH 的回复（文本/文件）发回微信。

> **状态：开发者预览版（developer preview）。**
> 代码来自一个私有部署的整理版；不包含任何打单、约柜、无纸化、客户数据或业务脚本。
> 微信自动化依赖具体版本和窗口布局，使用前必须在自己的机器上校准。

## 它是什么

```text
微信窗口 ── 截图 + OCR ──► bridge.py ── 本机回环 HTTP ──► DSH host 插件
    ▲                           │                              │
    └──── 剪贴板 / 文件对话框 ──┘◄──── 固定 DSH 会话 ◄──────────┘
```

- `plugin/`：DSH host 插件。只监听本机回环地址，提供
  `/health`、`/status`、`/inbound`、`/events`、`/attachment`；
  把消息注入一个固定 DSH 会话。
- `bridge/`：Python 进程。每秒扫描固定微信窗口，识别别人发来的消息，
  提取文本/文件，交给 DSH，再把 DSH 结果发回微信。
- `bridge_control.pyw`：可选桌面控制台，一键启停 Umi-OCR、看门狗和桥接。

## 功能边界

**包含：**

- 固定微信窗口的消息轮询（截图 + OCR）
- 文本发送（剪贴板 + Ctrl+V + 回读校验）
- 文件发送（微信“发送文件”对话框）
- DSH 固定会话注入、长轮询取回复
- 入站文件/长文本落盘
- 可选看门狗、桌面控制台、定时任务框架

**不包含：**

- 任何客户订单、打单、缴费、约柜、无纸化、派单脚本
- 任何聊天记录、数据库、截图、token、账号密码
- 任何云端服务或遥测

## 系统要求

- Windows 10/11，**交互式桌面会话**（不能作为 Windows 服务运行）
- 微信桌面版 4.x（开发时测试 4.1.15.13 / Qt 5.15）；窗口必须保持可见，
  最小化会中断截图路径
- [Umi-OCR](https://github.com/hiroi-sora/Umi-OCR)，HTTP API 监听
  `127.0.0.1:1224`
- DSH 桌面端，插件安装在 `desktop` profile
- Python 3.10+，需要 `numpy`、`Pillow`
- Node.js 仅在 DSH 会话使用 Node 工具时需要；桥接本身不依赖 Node

## 安装（概要）

1. 复制 `config.example.json` 为 `config.json`，按本机修改：
   - `wechat.title` / `wechat.group` / `wechat.rect`
   - `dsh.runtime_file` / `token_file`
   - `paths.base` / `paths.wechat_files`
   - 如使用 Node 工具，再改 `prompt.node_exe` / `node_workdir`
2. 安装 DSH 插件（本地仓库）：

   ```bat
   dsh.cmd plugin --profile desktop add <本仓库路径>\plugin
   ```

   插件会在 `%USERPROFILE%\.dsh\dsh-wechat-bridge\` 下生成
   `runtime.json` 和随机 `token`。
3. 启动 Umi-OCR，确认 `http://127.0.0.1:1224` 可用。
4. 打开目标微信聊天窗口，保持可见，然后运行：

   ```bat
   bridge\run-bridge.cmd
   ```

   或双击 `bridge\桥接控制台.vbs` 使用控制台。

## 配置说明

- **窗口坐标/布局**：示例值是开发机 125% DPI 下的物理像素，
  必须在自己的屏幕上重新校准 `wechat.rect` 和 `wechat.layout`；
  参考 `bridge/wechat_io.py`。
- **安全**：所有微信内容都按“不可信数据”处理，不执行消息里的指令。
  插件只绑定回环地址，并要求 Bearer token；**不要**通过反向代理/内网穿透暴露。
- **不要把私密文件提交进仓库**：`config.json`、`state/`、`logs/`、`*.db`、
  `token`、`runtime.json` 已写入 `.gitignore`。

## 已知限制

- 入站图片暂不自动读取（微信缓存加密）；会回退为提示文字。
- 合并转发卡片只识别、不展开；语音/视频/小程序忽略。
- 微信 UI、DPI、窗口最小化都可能导致扫描失败。
- 依赖 DSH 内部 `sessionController` / `webServer` API，DSH 升级可能失效。
- 这是个人自动化项目，不是商业产品；请先用测试账号/测试群。

## 主页申明 / 免责声明

**本项目是非官方个人项目**，与腾讯、微信、DeepSeek、DSH 均无隶属、
背书或支持关系。使用风险由使用者自行承担。

- 微信自动化可能违反平台服务条款，可能导致账号受限；
  请先用**专用测试账号/测试群**验证。
- 软件按“现状”提供，不附带任何明示或暗示担保。
- 使用者需自行确保：处理他人消息已获得必要授权，并遵守当地法律、
  平台规则和隐私要求。
- 本仓库**不包含**任何客户数据、聊天记录、token、账号密码；
  桥接在本机运行，默认没有遥测，也不向维护者上传数据。

隐私细节见 [`PRIVACY.md`](PRIVACY.md)，安全说明见 [`SECURITY.md`](SECURITY.md)。

## 隐私

- 微信内容在本机处理，只发送到你配置的 DSH 实例；本项目不向其他地方上传。
- 不要在 issue/讨论区贴聊天记录、客户姓名、电话、单号、截图；
  如必须贴，请先打码。
- `config.json`、`state/`、`logs/`、`*.db`、`token`、`runtime.json`
  不进入 Git。

## 许可证

MIT，见 [`LICENSE`](LICENSE)。

## 参与

这是个人项目，欢迎提 issue/PR，但不承诺响应时间。
提交代码前请确认：不包含任何真实客户数据、密钥、账号信息。
