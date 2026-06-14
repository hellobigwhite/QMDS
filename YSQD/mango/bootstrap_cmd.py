# bootstrap_cmd.py
import os
import sys
import subprocess

def relaunch_in_cmd():
    """
    如果不是在独立 CMD 中运行，则自动打开 CMD 并重启自己
    """
    if os.name != "nt":
        return

    # 防止无限递归
    if os.environ.get("SHOPIFY_IN_CMD") == "1":
        return

    python = sys.executable
    script = os.path.abspath(sys.argv[0])
    args = " ".join(f'"{a}"' for a in sys.argv[1:])

    cmd = f'set SHOPIFY_IN_CMD=1 && "{python}" "{script}" {args}'

    subprocess.Popen(
        ["cmd", "/k", cmd],
        creationflags=subprocess.CREATE_NEW_CONSOLE
    )

    sys.exit(0)
