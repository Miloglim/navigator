# Prospector 客户端下载入口

## 范围

Navigator 在 `8795` 同源提供独立的 Prospector Windows 客户端下载页。该入口不读取运价业务库，不要求 Navigator 产品准入，也不替代 Prospector 自动更新服务。

公开读取路由：

- `GET /download`：下载页面。
- `GET /api/downloads/prospector/latest`：当前稳定版清单。
- `GET /downloads/prospector/<fileName>`：当前清单登记的安装包。

文件下载只接受清单中的唯一文件名；目录穿越、旧版本文件名和未登记文件均返回 `404`。

## 运行目录

默认目录：

```text
Database/.runtime/downloads/prospector/
```

生产环境可通过 `NAVIGATOR_DOWNLOADS_DIR` 指向只读分发目录。该目录必须包含 `latest.json` 和对应安装包，不应放入源码、日志、Token 或业务数据库。

## 清单

`latest.json` 由 Prospector 发布脚本生成，页面每次请求重新读取，因此发布新版本不需要重启 Navigator。字段和最小示例见 `download-manifest.example.json`。

接口统一返回 `{ok,data|error}`。清单缺失返回 `ok:false,code:not_found`；清单损坏、文件大小不一致或签名状态无效返回 `ok:false,code:unavailable`。

## 发布

在 Prospector 仓库执行：

```powershell
npm.cmd run package:formal -- -DownloadsRoot "C:/Users/YQN/Documents/正式版运价库/运价项目/Database/.runtime/downloads/prospector"
```

脚本先执行正式打包，再将安装包和 `latest.json` 原子写入下载目录。脚本不会发布到公网、不会部署生产库，也不会删除历史安装包。

## 边界

- 下载页公开只读，当前不校验 Prospector 产品权限。
- 安装包签名状态必须如实写入清单；未签名包不能因页面展示而被描述为已签名。
- 若 Navigator 后续成为正式自动更新源，需另接 `latest.yml`、blockmap 和 Electron updater 验证，不能只替换下载页。

