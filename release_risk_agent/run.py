"""发布风险评审命令行入口。默认无子命令时等价于start。"""
import os
import sys

# No tracing service or hidden model telemetry is enabled by this project.
os.environ.setdefault("LANGSMITH_TRACING", "false")
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
os.environ.setdefault("DO_NOT_TRACK", "1")

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    from release_guard.cli import main
    raise SystemExit(main())
