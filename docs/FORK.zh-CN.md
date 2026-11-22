# 本 fork 的改动与使用

仓库：<https://github.com/ooizumi/d2r-mobile>。
上游：<https://github.com/jhaizhou-ops/d2r-mobile>，起点为 v2.3.1 / `4a938fe`。
保留上游 MIT 许可证和第三方署名。

## 已完成的主要改动

- Android Mate 60 Pro 与 HarmonyOS NEXT 的兼容性、触控和渲染适配。
- 长按左上角 FPS 约 0.6 秒打开调试菜单；支持调整 3D 分辨率、Bloom、像素放大、性能采样和后台存档开关。界面与小地图沿用独立的输出分辨率。
- 管线使用记录自动落盘，后续启动预热已收集的状态。不能保证一次预热覆盖尚未遇到的所有管线。
- 游戏切后台时可请求本地存档，设置按设备保存；正常退出仍使用游戏的“保存并退出”。
- Android、HarmonyOS、macOS 的 HTTP 云存档，使用内容校验和版本比较处理冲突。双方独立修改时由玩家选择保留方向；网络失败可离线启动。
- 本机源码、用户自备游戏资源、私人签名材料分离的构建和安装工具。

手机、平板和 Mac 已有本机实际运行记录。帧率随硬件、场景、分辨率和温度变化；本 fork 不承诺所有设备稳定 60 FPS。

## 平台入口

| 用途 | 入口 |
|---|---|
| Android／HarmonyOS 源码构建、资源准备与安装 | [MOBILE_INSTALL.zh-CN.md](MOBILE_INSTALL.zh-CN.md) |
| macOS 本机应用 | [MAC.zh-CN.md](MAC.zh-CN.md) |
| 在 Windows 上制作手机包（S2Arm 接口） | [S2ARM.zh-CN.md](S2ARM.zh-CN.md) |
| 云存档服务与维护 | [CLOUD_SAVE.zh-CN.md](CLOUD_SAVE.zh-CN.md) |
| 云存档协议 | [CLOUD_SAVE_PROTOCOL.md](CLOUD_SAVE_PROTOCOL.md) |

当前源码没有 Windows 游戏运行后端。S2Arm 接口里的 Windows 支持是制作手机包的工具链，不是 Windows 原生游戏 EXE。若在 PC 通过安卓运行环境启动，还须单独验证 ARM64 指令兼容和 Vulkan 支持，不能将该结果称为原生 Windows 移植。

## 游戏文件与私人配置

仓库不提供 ROM、解密密钥、提取出的游戏资源、生成的游戏程序、玩家存档、个人云存档配对码或签名材料。使用自己的游戏文件在本机构建；版本校验以实际文件为准，不以 ROM 文件名为准。

云存档服务默认地址不附带任何账户权限。自行部署或配对的说明见云存档文档。`cloud-save.json` 放在源码树之外，只注入个人安装包；包含配对信息的安装包不要作为公开 Release 上传。

## 提交前回归入口

```sh
python3 -m unittest discover -s dm/tools/tests -p 'test_mobile*.py'
python3 -m unittest discover -s dm/tools/tests -p 'test_cloud_pairing.py'
python3 -m unittest discover -s dm/cloud_save/tests -p 'test*.py'
node dm/harmony/tests/cloud_save_test.cjs
```

原生存档交换、生命周期、管线自动保存和画面开关的宿主测试位于 `dm/host/tests/t_cloud_files.c`、`dm/android/tests/lifecycle_dispatch_test.c` 和 `dm/gfx/vulkan/engine/tests/`。这些测试不替代对应设备的构建和运行验收。
