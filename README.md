# YAO-design-studio

一个仅监听 `127.0.0.1` 的本地可视化控制台。它内置 TokenHub MiniMax H3 客户端，也可通过要务网关调用 Seedance 2.0 和 2.5 生成视频，通过 APIMart 调用 Midjourney 生图。可独立运行。

## 运行要求

- macOS（用于通过钥匙串保存 Key、在 Finder 中打开生成视频）；
- 用户自己的 TokenHub API Key（用于 MiniMax H3 视频）；
- 用户自己的 APIMart API Key（仅在使用“生成图片”页签时需要）；
- 用户自己的要务国内测试网关 API Key（仅在使用 Seedance 时需要）。

双击 `start.command` 时，会优先使用本机的 Python 3.9–3.12。需要时会从 Astral 的
python-build-standalone 下载对应 Mac 架构的 Python 3.12 到 `.runtime/`，后续启动直接复用。

## 启动

```bash
cd '/你解压工具包的位置/YAO-design-studio'
./start.command
```

也可以双击 `start.command`，然后打开 <http://127.0.0.1:8123>。

首次使用时，点击右上角密钥入口并选择服务后输入 API Key。各服务密钥分别通过 `security` 写入 macOS Keychain，不会写入此目录或浏览器存储。后续启动会自动读取对应 Key。

Seedance 2.0 和 2.5 共用要务国内测试网关 `https://qallm-api.mechlabs.cn/v1`，模型 ID 分别为 `doubao-seedance-2-0` 和 `doubao-seedance-2-5`。工作台的清晰度菜单提供 480p、720p 和 1080p。工作台优先读取进程环境中的 `QALLM_API_KEY`，其次读取 macOS 钥匙串中服务 `com.openai.codex.mechlabs.seedance`、账户 `QALLM_API_KEY` 对应的密码。若已通过本地 Seedance 技能配置同一条钥匙串记录，工作台启动后会直接读取。 [要务网关接入指南](https://qallm-api.mechlabs.cn/guide#video-generation)

MiniMax H3 任务提交到 TokenHub，Midjourney 任务提交到 APIMart。页面会轮询任务状态，显示失败原因，并把成功结果下载到本目录下的 `outputs/`。

## 分享和更新

从本仓库 [Releases](../../releases/latest) 下载 `YAO-design-studio-macOS.zip`，解压后即可分享整个工作台。收件人用 `start.command` 启动，以后双击文件夹内的 `更新工作台.command` 检查更新。更新成功后，关闭旧的启动终端，再打开 `start.command`。已生成文件、上传记录、浏览器任务队列和钥匙串密钥会保留。

维护者将改动推到 `main` 后，创建并推送一个更高的版本标签，例如 `v1.0.1`。GitHub Actions 会构建工作台压缩包及更新文件，并发布 GitHub Release。收件人继续从同一个仓库更新。
