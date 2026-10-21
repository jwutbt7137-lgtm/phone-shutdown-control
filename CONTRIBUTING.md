# 参与贡献

感谢愿意花时间改这个项目。它是个人工具长出来的，欢迎把它变得更稳、更好装。

## 先跑起来

```powershell
git clone https://github.com/jwutbt7137-lgtm/phone-shutdown-control.git
cd phone-shutdown-control

# 电脑端依赖 + 逻辑测试（不需要蓝牙，不会关机）
cd pc-server
python -m pip install -r requirements.txt
python test_protocol.py
```

Android 端用标准 Gradle：

```powershell
cd android
.\gradlew.bat assembleDebug
```

## 提交 Issue

- 一个 Issue 只讲一件事
- Bug 请带：Windows 版本 / 手机型号 + Android 版本 / Python 版本 / 复现步骤 / `pc_server.log` 相关片段
- 不要贴完整 PIN、蓝牙 MAC、个人路径；贴之前自己打码

## 提交 Pull Request

1. 从 `main` 切分支：`fix/xxx`、`feat/xxx`、`docs/xxx`
2. 只改和主题相关的文件；顺手重构请单独开 PR
3. 提交信息用一句话说清"改了什么、为什么"：

   ```
   fix(pc-server): 认证失败计数改为按会话重置

   原来全局计数，A 手机关掉后 B 手机接着数，5 次就误踢。
   ```

4. PR 描述里写：改了什么 / 怎么验证的 / 有没有破坏协议兼容
5. 确认 CI 绿了再请求评审

## 代码约定

- **电脑端**：Python 3.9+ 兼容，只用标准库 + `winrt-*`；不引入需要编译的依赖
- **手机端**：Kotlin，`minSdk 26` / `targetSdk 35`；不新增重型框架
- **协议**：手机端和电脑端必须同步改，`docs/协议与架构.md` 与 `test_protocol.py` 的契约断言一起更新
- **关机相关改动**：默认按"会作用在真实电脑上"评审；新增命令必须过 PIN 校验、必须写审计日志
- **格式**：UTF-8 / LF（`.bat` 除外），2 空格缩进 YAML/JSON，4 空格缩进 Python/Kotlin

## 不建议的改动

- 引入联网依赖、账号体系、云服务（这个项目刻意保持"纯本地蓝牙"）
- 去掉关机倒计时或"取消关机"
- 把 PIN 校验做成可关闭的选项（要的话请先开 Issue 讨论威胁模型）

## 行为准则

参与即表示同意 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
