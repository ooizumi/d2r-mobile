# D2R Mobile 源码构建与安装

`dm/tools/mobile_install.py` 是统一入口。它调用资源、构建、设备安装和鸿蒙调试导入 worker，并传递结构化 JSON。工具不会下载游戏、上传密钥、自动选择设备或绕过 HarmonyOS 签名。

先复制示例配置并修改路径：

```bash
cp dm/tools/mobile-install.example.json /path/to/mobile-install.json
python3 dm/tools/mobile_install.py doctor --config /path/to/mobile-install.json --json
```

先看完整计划，不写文件、不启动构建或设备：

```bash
python3 dm/tools/mobile_install.py run --config /path/to/mobile-install.json --dry-run --json
```

## 常用命令

准备资源。`game_dir` 和 `roms[]` 二选一；使用 ROM 时必须同时提供自己的 `keys`：

```bash
python3 dm/tools/mobile_install.py prepare --config /path/to/mobile-install.json --json
```

单独构建：

```bash
python3 dm/tools/mobile_install.py build --config /path/to/mobile-install.json --json
```

不知道连接了哪台设备时，只做只读枚举：

```bash
python3 dm/tools/mobile_install.py devices --config /path/to/mobile-install.json --json
```

安装要求配置中明确写出设备 `serial`。`install` 还要求明确的 `install.artifact` 和 `install.resources`：

```bash
python3 dm/tools/mobile_install.py install --config /path/to/mobile-install.json --json
```

完整串联 `prepare → build → install`：

```bash
python3 dm/tools/mobile_install.py run --config /path/to/mobile-install.json --json
```

未签名的 HAP 会停在 `awaiting_signing`，不会调用 HAP 安装。只有显式设置 `device.launch=true` 才会启动应用。

## 资源内容

准备好的导入目录有两种布局：Android 为 `android/ + data/`，HarmonyOS 为 `dm/ + data/`。`data/` 包含自有游戏导出的 18 个必需资源档案及其原版 `.build.config`；运行包包含改写后的程序、着色器、覆盖层和界面资源。生成过程还会写出 `resource-manifest.json`、来源信息和逐文件摘要。

当前资源配置接受 Switch **1.0.34.0 / PC 数据版本 3.3.94012**，以 `main`、`sdk`、`subsdk0` 的实际 SHA-256 校验版本。压缩包或 ROM 的文件名不能代替版本检测；不匹配的版本会停止，不会强行套用补丁。输入为 NSP/XCI 时还需要使用者自己的解密密钥和 `hactool`。

ROM、keys、提取结果、生成表、签名材料和构建输出都留在本机。不要把含有游戏内容的目录放进源码仓库或源码导出包。

## SDK、签名与设备

安装 Android SDK/NDK/CMake 只能解决编译问题；HarmonyOS 的 SDK 也一样。HAP 编译成功不等于可安装，正式安装前仍需用当前账号和包名对应的签名材料完成 Harmony 签名。工具不会复用无关证书，也不会把签名配置内容写进日志。

`build.build_mode` 可取 `debug` 或 `release`，缺省为 `debug`。缺省模式沿用原有个人安装流程，并使用 `entry@default`；发布版需显式选择 `release` 并提供对应签名材料。调试包可由 `hdc -b` 进入应用沙箱，且两种模式都不内置运行资源。

## 云存档个人安装包

Android 与 HarmonyOS 在进入游戏前检查云存档；首次两端都有不同存档，或两端都改动过时，必须选择“云存档覆盖本地”或“本地存档覆盖云端”。可以离线启动，下一次启动再处理；不会按时间自动选胜者。运行中只上传游戏已经提交的存档，网络失败会保留进度。

在安装配置的 `build.cloud_config` 填入源码目录外的私人 JSON 路径，内容为 `endpoint`（HTTPS、以 `/v1` 结尾）和 `token`。构建工具将它注入个人 APK/HAP，源码导出排除此文件。单独调用 `mobile_build.py` 时使用 `--cloud-config /private/path/cloud-save.json`。不要公开分发含有个人配对信息的安装包；未注入时可以在启动界面输入服务地址和配对码。

覆盖已有 Android 调试版时，设置相同 `build.app_id`，并用 `build.debug_keystore` 指向原调试签名（标准 debug alias/password）。工具仅把它复制到本次工作区的私人签名目录；不使用新生成的调试密钥替代已有签名。单独构建参数为 `--debug-keystore`。

云同步只传 `files/save` 内的存档；不传游戏资源、ROM、`Settings/` 或名称以 `.` / `_` 开头的本机数据。恢复前的本地完整版本留在 `files/cloud-save/restore-*`（具体目录前缀以客户端实现为准），云端历史版本不自动清理。服务、限额与协议见 [CLOUD_SAVE_PROTOCOL.md](CLOUD_SAVE_PROTOCOL.md) 和 [服务说明](../dm/cloud_save/README.md)。

Android 使用 `adb`，HarmonyOS 使用 `hdc`。安装始终按配置中的精确 `serial` 执行，不猜测唯一设备。HarmonyOS 使用文件选择器导入时，需要显式设置设备上的 `remote_dir`。当前示例采用 `device.import_mode="debug-sandbox"`，由调试应用自己完成首次导入，无需猜测公开文件夹；Android 配置应改为 `"external"`。

HarmonyOS 首次签名：用 DevEco 打开生成的 `<workspace>/build/tree/dm/harmony/app`，在项目结构的签名配置中登录自己的开发者账号，为当前包名和测试设备生成签名。把该私有 `build-profile.json5` 另存到工作区内独立位置，再填入 `build.build_profile`；下一次构建会重建 `build/tree`，不要把唯一签名配置留在会被重建的目录里。签名材料不进入源码 ZIP。

`awaiting_import` 表示应用已安装、文件已传输，仍需在应用中完成导入或资源访问授权，不代表已进入游戏。鸿蒙的公开目录因设备而异；`/data/local/tmp` 仅可用作桥接工具的临时位置，不能当作文件选择器可访问的导入目录。

### 鸿蒙调试包首次导入

`device.import_mode="debug-sandbox"` 时，`install` 和 `run` 只安装 HAP，返回 `awaiting_debug_import`。首次安装后先启动应用一次以创建沙箱，然后停止应用；未导入时显示缺少资源属于这个步骤的预期状态。对明确选定的设备执行：

```bash
hdc -t YOUR_SERIAL shell aa start -b com.xiao.boluo -a EntryAbility
hdc -t YOUR_SERIAL shell aa force-stop com.xiao.boluo
python3 dm/tools/mobile_install.py import-resources --config /path/to/mobile-install.json --json
hdc -t YOUR_SERIAL shell aa start -b com.xiao.boluo -a EntryAbility
```

应用包名改过时，上述命令也要使用自己的包名。导入工具通过官方 [hdc 沙箱文件传输](https://developer.huawei.com/consumer/en/doc/HarmonyOS-Guides/ide-device-file-explorer/) 把 ZIP 和请求写入应用公共缓存。设备上的 `hdc shell -b` 不一定有重命名、解压或删除权限，因此工具不依赖它改写模块目录，也不修改权限。应用会解压到自己的暂存目录，逐文件校验大小和 SHA-256，再发布到实际运行目录。

此模式只用于首次导入，发现已有运行包、设置、管线缓存或存档时会停止；首次空启动生成的 `boot.log` 会单独保留。`awaiting_app_import` 仅表示传输和请求已准备好；重新启动后，应用内的导入结果及 `d2r-installer-result.json` 才能确认成功。失败时保留诊断，不会把传输成功误报为游戏可运行。

## 导出可编辑源码

```bash
python3 dm/tools/mobile_install.py export-source \
  --config /path/to/mobile-install.json --json
```

导出包来自当前工作树，包括已修改和未忽略的新源码。根目录的 `SOURCE_EXPORT_MANIFEST.txt` 列出完整导出，`dm/SOURCE_EXPORT_MANIFEST.txt` 再列出 worker 所需的 `dm/` 文件。它排除游戏包、keys、NCA/TIK/data/XGS、补丁程序、证书与私有签名配置、`local.cmake`、`local.env`、缓存/工作区、`Gen/` 和 APK/HAP 等产物；只排除生成的 `dm/harmony/app/build-profile.json5`，保留源码中的 `entry/build-profile.json5`。这些规则不依赖 `.gitignore`，强制加入 Git 的私有文件也会被排除。输出路径必须在源码目录之外。

## 平台边界

macOS 是当前实际验证目标。Windows 配置解析和源码打包按便携方式设计，但尚未作为完整设备流程验收。从 ROM 包提取仍依赖 Bash 工具链；在原生 Windows 上更稳妥的做法是先在已验证的 Bash/WSL 环境提取，再把提取出的 `Update` 作为 `game_dir`。
