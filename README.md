# NapCat_Go

一键部署与连接 NapCat 到 AstrBot 的插件。

## 前置要求

1. 已安装 **QQNT 9.9.27+**
2. Python 环境（AstrBot 自带），已安装 `aiohttp`
3. 已经创建好机器人了

## 安装插件

1. 在 AstrBot 插件市场安装本插件，或手动放入 `data/plugins/`
2. 重载插件
3. 打开 WebUI → 插件 → NapCat_Go → 打开页面

## 首次使用

1. 页面点「启动」按钮
   - 插件会自动检测 QQ 是否安装
   - 检测 NapCat 是否已下载；没有则自动从 GitHub Release 下载 `NapCat.Shell.zip` 并解压到 `napcat/`
2. 等待日志区出现 `WebUi User Panel Url: http://127.0.0.1:6099/webui?token=xxx`
3. 点「在浏览器打开 WebUI」按钮，在浏览器完成 QQ 扫码登录
4. 登录成功后，插件会自动：
   - 在端口范围内选一个可用端口
   - 在 AstrBot 中创建 aiocqhttp 机器人实例
   - 将反向 WS 地址写入 NapCat 的 `onebot11_*.json`
   - 重启 NapCat 使配置生效

## 常见问题

- **启动后一直显示「未安装 QQ」**：请从 https://im.qq.com 安装最新版 QQNT
- **端口被占用**：修改配置里的 `napcat_port`，或在启动前用「清理残留进程」按钮清理
- **插件重载卡住**：点「清理残留进程」按钮，或命令行执行 `taskkill /F /IM NapCatWinBootMain.exe`
- **出现NapCat下载失败时，请到releases界面手动下载到路径："C:\user\{User_name}\.astrbot\data\plugins\pulid_napcat_go_to_astrbot"

```bash
https://github.com/NapNeko/NapCatQQ/releases
```
