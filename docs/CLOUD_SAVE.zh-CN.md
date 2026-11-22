# 暗黑2云存档

Android 与 HarmonyOS 共用独立云存档服务，默认地址为
`https://mythstribe.com/d2r-cloud/v1`。服务运行在 120.53.45.109，
与 Joplin 复用机器，但不使用 Joplin 账户、数据库或数据目录。

启动时检查本地和云端。只有一边变动时同步该版本；首次两边已有
不同存档，或两边都在上次同步后变动时，必须明确选择：

* **云存档覆盖本地**：保留本地完整备份，再恢复云端版本。
* **本地存档覆盖云端**：提交本地版本，云端原版本继续保留。
* **暂不处理，离线启动**：本次不自动上传，下一次启动再比较。

选择期间云端又变化会重新提示。不会按设备时间判断哪个版本胜出。
游戏运行时每 30 秒尝试上传已提交存档，退后台也会尝试；断网、系统
结束进程或上传中断不会删除本地存档，下次启动可继续同步。离线启动
后本次游戏不会自动覆盖云端。

同步的是 `files/save` 整套存档，包括角色、共享仓库和宿主存档侧文件。
`Settings/` 和任意层级以 `.` / `_` 开头的本机条目不上传，恢复时保留。
游戏资源、ROM、`dm/` 和旧版 `files/saves` 不参与同步。

## 构建和配对

普通源码中不含账户密钥。启动界面支持输入 HTTPS 服务地址与配对码。
个人安装包可以通过 `mobile_install.py` 配置项 `build.cloud_config`
注入配对 JSON；文件须位于源码目录之外，结构为 `endpoint`、`token`。
`mobile_build.py` 对应参数是 `--cloud-config`。真实配对文件及含配对信息的
私人 APK/HAP 不要公开发布。源码导出会排除 `cloud-save.json` 与指定私人配置。

本任务的构建配置位于工程的 `work/cloud-save/android-build.json` 和
`work/cloud-save/harmony-build.json`，只配置构建，没有设备安装或启动操作。
Android 目标为当前修改版 `com.xiaoboluo.d2r.mate60`，沿用原调试签名，
与下载的原版 `com.xiaoboluo.d2r` 是不同应用。配置项 `build.debug_keystore`
（单独构建参数 `--debug-keystore`）复用已有标准 Android debug key，
仅复制到构建工作区的私人目录，便于覆盖升级并保留原存档。

## 找回旧版本

本地被覆盖的完整存档保留在应用私有目录 `files/cloud-save/restore-*`。
这些目录不自动删除。目录可能是恢复过程中尚未安装的暂存版本，因此
手动恢复时先核对角色文件与内容，在应用完全停止后操作，先保留当前版本。

云端历史可用源码中的工具查看和导出。命令只读取私人配对文件，不打印密钥：

```sh
python3 dm/tools/cloud_save_admin.py --config /private/cloud-save.json history
python3 dm/tools/cloud_save_admin.py --config /private/cloud-save.json download \
  --revision CHOSEN_REVISION --output /private/recovered-save
```

需要把某个历史版本重新设为云端当前版本时，明确指定历史版本和当前版本：

```sh
python3 dm/tools/cloud_save_admin.py --config /private/cloud-save.json restore-cloud \
  --revision CHOSEN_REVISION --expected-current CURRENT_REVISION
```

期间若其他设备更新云端，此命令返回 409，旧版本仍保留。客户端下次启动
会重新比较，已有独立本地进度时仍要求选择覆盖方向。

## 服务维护

* systemd：`d2r-cloud-save.service`，回环地址 `127.0.0.1:8765`。
* 程序：`/opt/d2r-cloud-save/dm/cloud_save/server.py`。
* 独立认证：`/etc/d2r-cloud-save/auth.json`，只存配对码的 SHA-256。
* 存档及历史：`/var/lib/d2r-cloud-save/accounts/`。
* HTTPS 路由：主域名 nginx server 引入
  `/etc/nginx/d2r-cloud-save.location.conf`。
* 健康检查：`https://mythstribe.com/d2r-cloud/healthz`。

默认每个账户最多 2 GiB 历史数据、8192 个版本和 4 MiB 历史索引。
达到任一限额时停止新增上传并返回 507，保留已有数据；本地仍可游戏。
单个快照最多 2048 文件、单文件 8 MiB、合计 24 MiB。历史不自动清理。
调整配额前应先检查磁盘空间，云端历史整理应先导出需要保留的版本。

停用云服务只需停用独立 systemd 服务并移除主域名 server 中的新增
`include`，通过 `nginx -t` 后 reload nginx。保留 `/var/lib/d2r-cloud-save`
可供恢复，不需要停止或修改 Joplin。原 nginx 配置备份在
`/etc/d2r-cloud-save/deploy-backups/`；不要整份覆盖之后其他任务的 nginx 改动。

协议与服务验证用例见 [协议](CLOUD_SAVE_PROTOCOL.md) 和
[服务说明](../dm/cloud_save/README.md)。
