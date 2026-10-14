#!/usr/bin/env python3
from __future__ import annotations
import argparse, pathlib, platform, subprocess, sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("output")
    args = ap.parse_args()
    system = platform.system()
    if system == "Darwin":
        p = subprocess.run(["pbpaste"], capture_output=True)
    elif system == "Windows":
        p = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"], capture_output=True)
    else:
        # Try common Linux clipboard commands.
        for cmd in (["wl-paste"], ["xclip", "-selection", "clipboard", "-o"]):
            try:
                p = subprocess.run(cmd, capture_output=True)
                if p.returncode == 0:
                    break
            except FileNotFoundError:
                p = None
        if p is None or p.returncode != 0:
            print("No supported clipboard command found", file=sys.stderr)
            sys.exit(2)
    if p.returncode != 0:
        print("Clipboard read failed", file=sys.stderr)
        sys.exit(p.returncode)
    pathlib.Path(args.output).write_bytes(p.stdout)
    print(args.output)

if __name__ == "__main__":
    main()
