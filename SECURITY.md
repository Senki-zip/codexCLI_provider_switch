# 安全说明

使用可信的 Codex、Node、bwrap 和 API 接口；URL 中不要携带 key/token。以普通用户运行，将 CODEX_HOME 和备份保存在本人拥有、权限受限的目录中。

**请勿向 issue、PR、CI artifacts 或公共仓库上传：**

- auth.json、API key、token、登录 Cookie 或含凭证的环境变量；
- SQLite 数据库（含 WAL/SHM）、真实 rollout/session、日志、备份与修复报告；
- encrypted_content 或其他密文、内部 reasoning、用户真实消息；
- 包含真实 UUID、用户路径、服务凭证的终端截图。

反馈问题时使用合成数据和脱敏描述，提交前检查暂存区。备份与修复报告按真实历史同等保护。

凭证泄露时先撤销或轮换，再联系维护者。安全问题可通过 GitHub 私密漏洞报告（若已启用）反馈，否则先发送不含敏感细节的联络请求。

bwrap 为原生回放禁用网络并限制文件写入；只读根目录仍可见宿主文件，因此只运行可信二进制。
