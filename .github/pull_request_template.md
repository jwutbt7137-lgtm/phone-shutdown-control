## 改了什么

<!-- 一句话说清这个 PR 做的事 -->

## 为什么

<!-- 关联 Issue：Fixes #123 -->

## 怎么验证的

- [ ] `python pc-server/test_protocol.py` 通过
- [ ] 用 `python pc_server.py --dry-run` + 真机走过一次（说明手机型号 / Windows 版本）
- [ ] 改了协议：两端同步改 + 更新 `docs/协议与架构.md`

## 检查清单

- [ ] 只改了和主题相关的文件
- [ ] 没有提交 `config.json`、`pc_server.log`、构建产物、本地路径
- [ ] 新增命令都过了 PIN 校验，并写了审计日志
- [ ] 关机 / 重启相关改动没有绕开倒计时与「取消关机」
