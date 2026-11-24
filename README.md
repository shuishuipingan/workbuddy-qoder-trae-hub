# WorkBuddy × Qoder × Trae Hub

一个 Python 进程运行三家引擎，通过一个面板和一把合并 Key 接入 OpenAI / Anthropic 兼容接口。

| 引擎 | 版本 |
| --- | --- |
| WorkBuddy | 2.1.3（指定的本地源码快照） |
| Qoder | [1.9.14](https://github.com/shuishuipingan/qoder2api-hub/releases/tag/v1.9.14) |
| Trae | [1.7.2](https://github.com/shuishuipingan/trae2api-hub/releases/tag/v1.7.2) |

## 启动

```bash
python -m hub.server --host 127.0.0.1
```

打开 <http://127.0.0.1:8817/>，在面板添加引擎账号和合并 Key。默认面板口令为 `admin`，首次登录后请修改。
API Base URL：`http://127.0.0.1:8817/v1`。引擎独立 Key 只走对应引擎。

Docker 用户可用 Compose 从源码构建：

```bash
docker compose up -d --build
```

默认宿主机面板端口为 `18791`，数据保存在 `./data`，升级时保留此目录。

[English README](README.en.md) · [使用说明](docs/merge/15-一个产品怎么用.md) · [发布记录](https://github.com/shuishuipingan/workbuddy-qoder-trae-hub/releases)
