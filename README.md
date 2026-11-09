# codexCLI_provider_switch

Linux 上的实验性 Codex CLI provider 切换与历史修复工具。当前仅管理 `openai` 与 `krillcode`，不是通用 provider 管理器，也不保证跨接口或任意 Codex 版本兼容。

**发布验证仅为离线合成测试；未在本仓库执行真实 Codex 原生回放、真实凭证调用或网络推理。** 原环境的 0.167.0 使用背景不等于本项目已验证该版本；其他版本同样未经验证。不要把测试通过理解为真实历史迁移成功。

## 安装与依赖

需要 Python **3.11+**（标准库 `tomllib`，没有 Python 第三方依赖）、Linux `/proc`、`fcntl` 锁、SQLite、Node.js，以及实际修复时需要的 Codex 原生二进制和 **bubblewrap (`bwrap`)**。不支持 Windows/macOS。原生回放通过 bwrap 禁用网络并只开放隔离 home 的文件写入；系统必须允许相应 Linux namespace。沙箱并非用于运行不可信二进制，也不是秘密文件读取隔离：根文件系统只读可见，请仅使用可信 Codex。

把根目录的 **`codex-switch` 与 `codex_session_repair.py` 两个文件放在同一目录，必须一起安装/升级**：

```sh
mkdir -p "$HOME/.local/bin"
install -m 755 codex-switch codex_session_repair.py "$HOME/.local/bin/"
python3 "$HOME/.local/bin/codex-switch" --help
```

将该目录加入 PATH，确保 `python3` 是 3.11+。`--help` 和离线测试不需要 Node/Codex/bwrap，也不读取真实凭证。

### 路径与凭证

| 环境变量 | 默认/用途 |
| --- | --- |
| `CODEX_HOME` | `~/.codex`；现有 config、rollout 和数据库所在目录 |
| `CODEX_SWITCH_NODE` | 否则从 PATH 查找 `node`；建议指定可信绝对路径 |
| `CODEX_SWITCH_OPENCODE_AUTH` | 否则 `$XDG_DATA_HOME/opencode/auth.json`，XDG 未设置时为 `~/.local/share/opencode/auth.json` |
| `CODEX_SWITCH_CODEX_BINARY` | 指定可信 Codex 原生可执行文件；优先于旧兼容变量 `CODEX_REPAIR_BINARY` |

未指定 Codex 二进制时优先查找 `CODEX_HOME/packages/app-server-daemon/releases/*/bin/codex`，否则从 PATH 的**绝对目录**中安全查找 `codex`（忽略空/相对目录）。建议显式设置二进制以固定版本。Node 和 bwrap 的 PATH 也必须可信。

生成的 provider auth 命令与切换检查使用同一个 Node 路径、同一组参数和同一个已解析的认证文件路径；路径写入 config，不写入 API key。更改环境变量后需重新生成配置。auth 命令只在 Codex 请求认证时向其 stdout 返回 key；不要手动运行/记录该命令的输出。`status` 仅打印 key 是否存在。URL 不要包含 key/token，以免出现在配置或状态输出中。

`official` 指 Codex 内建 `openai` provider 的官方 ChatGPT 登录路由，**不负责执行登录**；请事先用可信 Codex 自己的登录流程完成认证。`krillcode` 为 API/转发调用，运行时从 OpenCode auth 的 `krillcode.key` 读取，不等同于官方登录，不自动把官方身份转换为 API 凭证。

默认 `https://api.kbsfdxc.net/backend-api/codex` 和预设模型只是 **Krillcode 特定 preset，非通用 API 地址或可用性承诺**；本项目不提供、不内嵌 key。使用前确认服务信任、权限和接口，按需用 `--url` / `--model` 覆盖。官方模式也会保留 Krillcode 定义，以容纳缺失 rollout 而未迁移的旧历史；所以生成配置仍需要 Node 路径。

## 使用（先备份，再 dry-run）

**先关闭全部 Codex 会话、后台 daemon 和其他历史写入进程。** 不要在修复中启动会话。`--force` 不绕过 writer 检查；无法安全检查同用户进程时会拒绝修复。

```sh
codex-switch status
codex-switch official --dry-run
codex-switch krillcode --model YOUR_MODEL --url https://YOUR_TRUSTED_ENDPOINT --dry-run
codex-switch repair-session SESSION_UUID --provider openai --dry-run
codex-switch repair-index --dry-run
```

`SESSION_UUID` 必须替换为自己本地的完整会话 UUID。检查预览后，去掉 `--dry-run` 才会实际修改。`retag` / `repair-index` 都进入同一套安全清理、provider 迁移与原生索引重建流程，并非仅修改标签。切换成功后重启 Codex。`--test` 网络测试已禁用，`--no-retag` 因安全原因被拒绝。

也可直接调用同目录 helper：

```sh
python3 codex_session_repair.py --home "$CODEX_HOME" --target-provider openai --all --dry-run
```

此示例要求已经设置 `CODEX_HOME`。首次使用应在自行复制的、权限受限的完整 home 上操作。CLI 假设现有 `config.toml`、`state_5.sqlite`、`thread_history_1.sqlite` 和相应 schema 已存在，不是新 home 初始化器。

### 行为与损失

- 保留不归工具管理的 TOML 数据；不能安全保留的复杂语法会报错，不强制覆盖。
- 删除不兼容的**内部加密 reasoning**；不是解密、重加密或密文转换。非空公开 reasoning summary/content 会保留，但内部隐藏推理信息会丢失。不要宣称加密转换成功。
- 在隔离 home 内用原生 `app-server` 回放并验证双向分页、ordinal/offset 索引，再提交。没有请求模型推理的 RPC；实际回放禁用网络。大量会话需要逐个 native replay、多次数据库备份及分页，可能很慢、占用大量磁盘；单次 native replay 目前有 120 秒期限。
- 全量修复遇到 missing rollout 会明确列出并跳过，保留 DB-only 历史原状；单会话显式修复则报错。被跳过的会话不代表已完成渠道迁移，恢复源 rollout 后再修复。
- legacy 路径仅在现有文件可验证且解析后位于指定 home 内时接受；外部路径、身份不匹配、不支持的 schema 会拒绝，不自动寻找其他用户的历史。
- dry-run 不写配置、不建备份、不执行 native replay；它不能证明随后原生回放会成功。Krillcode 切换预览仍会本地验证 auth helper 可用性，不进行网络调用。

### 备份和回滚限制

备份位于 `CODEX_HOME/.switch-backups`（配置快照）与 `CODEX_HOME/repair-backups`（rollout、SQLite、报告、隔离回放数据），**含敏感历史，不能公开上传**。应使用私有目录、限制权限并自行制定保留策略；工具不会自动清理这些备份。

批次先完成所有 native staging，再提交；普通异常时尝试恢复本批目标，配置提交失败也会触发该边界内的恢复。它**不是断电/崩溃原子事务**：SQLite WAL、多库与文件替换无法保证跨文件原子性。检测到外部写入会拒绝自动覆盖，回滚也可能失败。没有“一键保证恢复”命令；先停止所有写入、保留当前副本，再由熟悉 SQLite 的操作者结合本地 batch/report 和备份恢复，注意 WAL/SHM 一致性，切勿盲目覆盖整个数据库或其他会话。没有独立完整备份时不要运行实际修复。

## 离线开发验证

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile codex-switch codex_session_repair.py tests/test_offline.py
python3 codex-switch --help
python3 codex_session_repair.py --help
```

测试只创建临时合成 SQLite/rollout/auth 标记，native replay 被 mock；不依赖真实历史、真实凭证、已有临时目录、Codex 或 bwrap。覆盖未知配置保留、missing skip、legacy 路径计划、writer 拒绝、批次回滚、dry-run 无写、认证路径一致和不输出 key。它们不验证真实 Codex/bwrap 集成、网络接口、实际性能或特定发行版兼容性。

安全披露见 [SECURITY.md](SECURITY.md)，设计来源见 [NOTICE](NOTICE)，许可为 [MIT](LICENSE)。
