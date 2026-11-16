#!/usr/bin/env bash
# 入口转发：真正的安装逻辑在 deploy/install.sh
# 保留这个薄壳是为了 `sudo ./install.sh` 这个常见习惯继续可用。
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/deploy/install.sh" "$@"
