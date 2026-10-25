# -*- coding: utf-8 -*-
"""Detached helper for the portable updater (issue #560).

Launched by wb_update_portable.request_install() / request_rollback() with the
gateway's pid and port. For an install it downloads and verifies the release,
stops the gateway, replaces the managed files, restarts, and rolls back if the
new version never becomes healthy. For a rollback it restores a retained backup
instead of downloading anything. All progress goes to updates/status.json for
the panel to poll.
"""
import argparse
import sys

from wb_update_portable import (keep_backups_setting, run_rollback, run_update)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", default="install",
                        choices=("install", "rollback"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--package-root", required=True)
    parser.add_argument("--tag", default="")
    parser.add_argument("--version", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--lan", default="0")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--repo", default="")
    # -1 means "read the panel's setting", which is what the panel's own
    # requests pass; an explicit number is for an operator running this helper
    # by hand.
    parser.add_argument("--keep-backups", type=int, default=-1)
    args = parser.parse_args(argv)
    keep = args.keep_backups
    if keep < 0:
        keep = keep_backups_setting(args.root)
    if args.action == "rollback":
        return run_rollback(args.root, args.package_root, args.version,
                            args.port, args.pid, keep_backups=keep)
    return run_update(args.root, args.package_root, args.tag or "v" + args.version,
                      args.version, args.port, str(args.lan).strip() == "1",
                      args.pid, args.repo, keep_backups=keep)


if __name__ == "__main__":
    sys.exit(main())
