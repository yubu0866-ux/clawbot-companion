# Windows 部署

本项目同时支持 Linux 与 Windows。本文是 Windows 路径的完整部署说明；
Linux 路径见 README 快速开始与 `clawbot-companion.service`（systemd）。

## 前置要求

- **Python 3.10 或更高（推荐 3.12）**：从 python.org 安装时勾选
  「Add python.exe to PATH」，装好后 `py --version` 能输出版本号即可。
- **Git**（可选）：克隆仓库用；没有 Git 直接下载 zip 解压也一样。
- **ffmpeg**（可选）：视频抽帧与音频筛查/响度测量用。未装时相关功能
  自动降级跳过，不影响聊天主链路。安装：`winget install Gyan.FFmpeg`
  或 `choco install ffmpeg`。

## 部署步骤

```powershell
# 1. 取得代码
git clone <本仓库地址，见仓库页 Code 按钮>
cd clawbot-companion

# 2. 复制三件套示例并按 docs/configuration.md 填写
copy config.example.json  config.json
copy persona.example.json persona.json
copy facts.example.json   facts.json

# 3. 安装依赖
py -m pip install -r requirements.txt

# 4. 设置 API 密钥环境变量（变量名由 config.json 的 llm.api_key_env 指定）
$env:COMPANION_LLM_API_KEY = "你的 key"        # 当前窗口
setx COMPANION_LLM_API_KEY "你的 key"          # 持久化（新开窗口生效）

# 5. 扫码登录（按微信官方 ClawBot 当期流程开通机器人后）
py bridge.py login
py bridge.py wait-login

# 6. 启动（start.ps1 / start.bat 与 Linux 的 start.sh 等价：
#    自动加载同目录 .env、设 TZ 与 PYTHONUTF8=1、拉起 serve）
.\start.ps1
```

启动后另开一个窗口验证：`py bridge.py status`；用你自己的微信给
机器人发一条消息，确认能收到符合人设的回复。

### 依赖说明（为什么 Windows 多一个 tzdata）

- `tzdata`（仅 Windows 安装）：Windows 没有系统时区数据库，
  Python 的 `zoneinfo` 在 Windows 上靠这个包查时区；Linux/macOS
  用系统 tz 数据库，不需要它。`requirements.txt` 已用环境标记
  （`sys_platform == "win32"`）限定只在 Windows 安装。
- `Pillow`：媒体图片处理；`numpy`：音频筛查脚本。
- `faster-whisper`（可选）：本地语音转写，体积较大（含 torch），
  按需 `py -m pip install faster-whisper`；未装时音频筛查的
  whisper 项自动跳过。已装在独立 venv 时，用环境变量
  `AUDIO_SCREEN_STT_PYTHON` 指向该 venv 的 `Scripts\python.exe`。

### 编码注意事项

本项目的人设、日志与聊天内容含大量中文。Windows 控制台与 Python
默认编码随系统区域设置变化（可能是 GBK/CP1252），直接读写会乱码
甚至抛错。`start.bat` / `start.ps1` 已自动设置 `PYTHONUTF8=1`；
手动跑任何子命令（如 `py worker.py ...`）前请先执行：

```powershell
$env:PYTHONUTF8 = "1"     # PowerShell
set PYTHONUTF8=1          # cmd
```

项目内所有 JSON/Markdown 读写均显式 `encoding="utf-8"`，配合
`PYTHONUTF8=1` 后全链路安全。

## 常驻运行

### 方案一：任务计划程序（Task Scheduler，零额外工具）

1. 打开「任务计划程序」→「创建基本任务」，名称如 `clawbot-companion`。
2. 触发器选「当用户登录时」（或「计算机启动时」，见下方注意）。
3. 操作选「启动程序」：程序填仓库里的 `start.bat` 全路径，
   「起始于」填仓库目录全路径。
4. 完成后在任务属性里按需勾选「不管用户是否登录都要运行」
   （需要保存 Windows 密码）、「如果任务失败，按以下频率重新启动」。

注意：serve 有单实例保护，重复拉起会以退出码 75 直接退出，不会双开。

### 方案二：注册为 Windows 服务（NSSM / WinSW）

想要真正的服务语义（开机自启、崩溃拉起、服务管理器可见），二选一：

- **NSSM**（`choco install nssm`）：
  ```bat
  nssm install clawbot-companion "C:\Path\To\Python\python.exe" "-u bridge.py serve"
  nssm set clawbot-companion AppDirectory "C:\Path\To\clawbot-companion"
  nssm set clawbot-companion AppStdout "C:\Path\To\clawbot-companion\state\serve-out.log"
  nssm set clawbot-companion AppStderr "C:\Path\To\clawbot-companion\state\serve-err.log"
  nssm set clawbot-companion Start SERVICE_AUTO_START
  nssm start clawbot-companion
  ```
  API 密钥用系统环境变量（setx /M）或 NSSM 的 `AppEnvironmentExtra` 注入。
- **WinSW**：下载 WinSW 可执行文件改名为 `clawbot-companion.exe`，
  同目录放一份 `clawbot-companion.xml`（executable 指向 python，
  arguments 为 `-u bridge.py serve`，workingdirectory 为仓库目录，
  含 `<env name="PYTHONUTF8" value="1"/>`），再
  `clawbot-companion.exe install` / `start`。

## 与 Linux 的平台差异（已在代码内适配，部署者需知）

- **文件锁**：跨进程锁在 POSIX 走 `fcntl.flock`，Windows 走
  `msvcrt.locking`（代码内置 shim，行为一致，无需配置）。
- **进程内 AES**：媒体加解密用的 libcrypto，Windows 下从 CPython
  自带的 `DLLs\libcrypto-3.dll` 等候选路径加载；若你用的是极简版
  Python 发行版且加载失败，安装官方 Python 或 OpenSSL 即可。
- **文件权限位**：Windows 没有 POSIX 权限位，`session.json` 的
  600 权限在 Windows 上不生效——请把仓库放在只有你自己账号能访问
  的目录（如用户目录下），靠 NTFS ACL 保护凭据文件。
- **systemd 相关**：sd_notify 心跳、目录 fsync 在 Windows 自动跳过；
  看门狗/拉起语义由上面两种常驻方案承担。
- **停止登录**：`py bridge.py logout` 在 Windows 走 pid 路径停
  serve（Linux 优先 systemctl）。
