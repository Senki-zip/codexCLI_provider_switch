# codexCLI_provider_switch

Linux 上的 Codex CLI provider 切换与历史修复工具，管理 `openai` 与 `krillcode` 两个渠道。

## 功能

- 切换渠道、模型与 API 地址，保留配置中不归工具管理的 TOML 数据。
- 查看当前渠道、会话数量与凭证是否存在，不打印 key。
- 清理跨渠道不兼容的加密 reasoning，迁移会话 provider，并用原生 Codex 重建历史索引。
- 支持单会话或全量修复，以及不写入的 dry-run 预览。

`official` 使用 Codex 内建 `openai` provider 的官方 ChatGPT 登录路由；请先通过 Codex 自身的登录流程完成认证。`krillcode` 使用 API/转发接口，运行时读取 OpenCode auth 中的 `krillcode.key`，与官方登录分开。

默认 `https://api.kbsfdxc.net/backend-api/codex` 和预设模型属于 Krillcode 特定 preset。使用其他服务时通过 `--url` / `--model` 指定接口与模型；项目不提供 key。

## 依赖安装

- Linux：需要 `/proc`、`fcntl` 锁及系统允许的 namespace。
- Python **3.11+**：使用标准库 `tomllib`、SQLite 等，无 Python 第三方依赖。
- Node.js：用于运行 provider auth 命令。
- Codex 原生二进制与 **bubblewrap (`bwrap`)**：用于实际修复时的离线回放。

原生回放通过 bwrap 禁用网络，根文件系统只读，隔离 home 可写。

把根目录的 **`codex-switch` 与 `codex_session_repair.py` 两个文件放在同一目录，必须一起安装/升级**：

```sh
mkdir -p "$HOME/.local/bin"
install -m 755 codex-switch codex_session_repair.py "$HOME/.local/bin/"
python3 "$HOME/.local/bin/codex-switch" --help
```

将该目录加入 PATH，确保 `python3` 是 3.11+。CLI 使用现有 `config.toml`、`state_5.sqlite`、`thread_history_1.sqlite` 及其 schema，不负责初始化新 home。

## 用法

**先备份完整 Codex home，关闭全部 Codex 会话、后台 daemon 和其他历史写入进程。** 修复期间保持关闭；`--force` 不绕过 writer 检查。

```sh
codex-switch status
codex-switch official --dry-run
codex-switch krillcode --model YOUR_MODEL --url https://YOUR_TRUSTED_ENDPOINT --dry-run
codex-switch repair-session SESSION_UUID --provider openai --dry-run
codex-switch repair-index --dry-run
```

将 `SESSION_UUID` 替换为本地完整会话 UUID。检查预览后，去掉 `--dry-run` 执行修改，完成切换后重启 Codex。`retag` / `repair-index` 都执行清理、provider 迁移与原生索引重建，不只是修改标签。`--test` 已禁用，`--no-retag` 会被拒绝。

dry-run 不写配置、不建备份、不执行 native replay。Krillcode 切换预览仍会本地验证 auth helper 可用性，不进行网络调用。

也可直接调用同目录 helper：

```sh
python3 codex_session_repair.py --home "${CODEX_HOME:-$HOME/.codex}" --target-provider openai --all --dry-run
```

## 环境变量

| 环境变量 | 默认/用途 |
| --- | --- |
| `CODEX_HOME` | `~/.codex`；现有配置、rollout 和数据库所在目录 |
| `CODEX_SWITCH_NODE` | 否则从 PATH 查找 `node`；建议使用绝对路径 |
| `CODEX_SWITCH_OPENCODE_AUTH` | 否则 `$XDG_DATA_HOME/opencode/auth.json`，XDG 未设置时为 `~/.local/share/opencode/auth.json` |
| `CODEX_SWITCH_CODEX_BINARY` | 指定 Codex 原生可执行文件；优先于旧兼容变量 `CODEX_REPAIR_BINARY` |

未指定 Codex 二进制时，优先查找 `CODEX_HOME/packages/app-server-daemon/releases/*/bin/codex`，否则从 PATH 的绝对目录中查找 `codex`，忽略空/相对目录。可显式指定二进制以固定版本。

生成的 auth 命令与切换检查使用同一个 Node 路径、参数和已解析的认证文件路径。配置只保存命令与路径，不保存 key；修改这些环境变量后需重新生成配置。auth 命令通过 stdout 向 Codex 提供 key，请勿记录该输出。

官方模式也保留 Krillcode 定义，以容纳缺失 rollout 而未迁移的旧历史，因此生成配置仍需 Node 路径。

## 历史修复行为和备份

- 清除带有 `encrypted_content` 的内部加密 reasoning 数据，**不是隐藏、解密或转换密文**。非空公开 summary、reasoning content 及消息正文保留；没有 summary/content 的加密 reasoning 对象会被移除。
- 在隔离 home 内用原生 `app-server` 回放，验证双向分页及 ordinal/offset 索引，再提交。回放禁用网络，不请求模型推理。大量会话需要逐个 native replay、多次数据库备份和分页，耗时与磁盘占用会增加；单次 native replay 的期限为 120 秒。
- 全量修复遇到 missing rollout 会明确列出并跳过，保留 DB-only 历史原状；单会话显式修复则报错。被跳过的会话不代表已完成渠道迁移，恢复源 rollout 后再修复。
- legacy rollout 路径需存在、身份匹配且解析后位于指定 home 内；外部路径或不支持的 schema 会报错。
- 不归工具管理的 TOML 数据保持不变；无法保留的复杂语法会报错。

### 备份与恢复

备份位于 `CODEX_HOME/.switch-backups`（配置快照）与 `CODEX_HOME/repair-backups`（rollout、SQLite、报告及隔离回放数据）。工具不会自动清理备份，请预留磁盘空间并自行管理保留周期。

批次先完成所有 native staging 再提交，遇到异常时尝试恢复本批目标，配置提交失败也会触发恢复。SQLite WAL、多库与文件替换不是跨文件崩溃原子事务，因此仍需独立完整备份。检测到外部写入会拒绝覆盖；若自动恢复未完成，停止所有写入、保留当前副本，结合本地 batch/report 与备份恢复，注意 WAL/SHM 一致性和其他会话的数据。

## 开发测试

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile codex-switch codex_session_repair.py tests/test_offline.py
python3 codex-switch --help
python3 codex_session_repair.py --help
```

测试创建临时合成 SQLite、rollout 和 auth 标记，native replay 使用 mock；不需要真实历史、凭证、Codex 或 bwrap。覆盖未知配置保留、missing skip、legacy 路径计划、writer 拒绝、批次回滚、dry-run 无写、认证路径一致和不输出 key。`--help` 同样不读取真实凭证。

当前验证范围为离线合成测试，不包含真实 Codex/bwrap 集成或网络推理。

安全披露见 [SECURITY.md](SECURITY.md)，设计来源见 [NOTICE](NOTICE)，许可为 [MIT](LICENSE)。
