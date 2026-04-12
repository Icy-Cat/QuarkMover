#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用辅助: Chrome 路径查找、端口探测、日志初始化"""
import os
import socket
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger


def find_chrome_path() -> Optional[str]:
    """跨平台查找本机 Chrome / Edge / Chromium。找不到返回 None。"""
    import platform
    system = platform.system()
    candidates: list = []

    if system == "Windows":
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env, "")
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(env, "")
            if base:
                candidates.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    elif system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:  # Linux
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/snap/bin/chromium",
        ]

    for p in candidates:
        if p and os.path.exists(p):
            return p

    # Fallback: try shutil.which
    import shutil
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def browser_check() -> Dict[str, Any]:
    """返回浏览器检测结果，供 API 调用。"""
    path = find_chrome_path()
    if path:
        name = "Chrome" if "chrome" in path.lower() else "Edge" if "edge" in path.lower() else "Chromium"
        return {"found": True, "path": path, "name": name}
    return {"found": False, "path": "", "name": ""}


def pick_free_port(preferred: int = 7788, max_tries: int = 20) -> int:
    """从 preferred 开始尝试，找到第一个可用端口。
    Windows 上不能用 SO_REUSEADDR（允许重复绑定），改用 SO_EXCLUSIVEADDRUSE。
    """
    for port in range(preferred, preferred + max_tries):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if sys.platform == "win32":
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"未找到可用端口（从 {preferred} 起试了 {max_tries} 个）")


def setup_logger(log_dir: Path) -> None:
    """配置 loguru：stderr 彩色 + logs/YYYY-MM-DD.log 文件轮转"""
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> <level>{level: <7}</level> <cyan>{name}</cyan> {message}",
    )
    logger.add(
        str(log_dir / "{time:YYYY-MM-DD}.log"),
        level="DEBUG",
        rotation="1 day",
        retention="14 days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {name}:{function}:{line} | {message}",
    )


def app_root() -> Path:
    """支持 PyInstaller 打包：运行时项目根目录"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent.resolve()
    return Path(__file__).parent.resolve()
