<div align="center">

# WorkBuddy x Qoder x Trae-hub

**One local gateway for three AI desktop clients — OpenAI & Anthropic compatible**

[![release](https://img.shields.io/github/v/release/shuishuipingan/workbuddy-qoder-trae-hub?style=flat-square&color=orange)](https://github.com/shuishuipingan/workbuddy-qoder-trae-hub/releases)
[![license](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](./LICENSE)

</div>

## What it is

WorkBuddy, Qoder and Trae each ship their own local reverse-proxy gateway: own accounts, own port, own dashboard.
This repo vendors all three under `hub/engines/` and adds a thin **merge layer** (`hub/`):

- **one process** — three engines in a single Python process (module prefixes keep them apart)
- **one port** — only `8817` is exposed; engine ports stay on loopback
- **one key** — a merged key routes by model availability across engines and realms, failing over automatically
- **one panel** — accounts, models, metrics, request log, gateway keys, rewards and settings in one React dashboard

## Quick start

```bash
# Windows: double-click  WorkBuddy x Qoder x Trea-hub.bat
# Linux / macOS
./start-hub.sh

# Docker
docker run -d -p 18791:8817 -v "$PWD/data:/app/data" \
  ghcr.io/shuishuipingan/workbuddy-qoder-trae-hub:latest
```

Panel: <http://127.0.0.1:8817> (default password `admin` — change it right after login).

Client config:

```
Base URL : http://127.0.0.1:8817/v1
API Key  : the merged key (mrg-…) from the panel
Model    : auto, or any model name from the merged list
```

## Endpoints

`/v1/chat/completions` · `/v1/responses` · `/v1/messages` · `/v1/models` — also available under `/api/v1/*`.

## Updates

The panel checks GitHub Releases and can update in one click. Only code paths are replaced:
accounts, usage, logs, `hub/settings.json` and `state/` are never touched (`hub/core/updater.py`).

## License

Merge layer (`hub/`, `admin/`, `docs/`) is MIT. Vendored engines keep their upstream licenses — see `hub/engines/*/README.md`.

中文说明见 [README.md](./README.md)。
