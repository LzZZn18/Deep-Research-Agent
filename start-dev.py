#!/usr/bin/env python3
"""DeepResearch 本地一键启动脚本。

在项目根目录运行：  python start-dev.py

会自动：起 db+redis（Docker）→ 等它们健康 → 依次起 backend(uvicorn)、
worker、前端(Vite)。每个服务一个独立窗口，日志互不干扰。
"""

import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(ROOT, "backend")
FRONTEND = os.path.join(ROOT, "frontend")

IS_WINDOWS = os.name == "nt"
# Windows 下每个服务单独开一个控制台窗口
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


def find_venv_python() -> str:
    """定位 backend 虚拟环境里的 python（Windows 用 Scripts/，Unix 用 bin/）。"""
    for rel in ("Scripts/python.exe", "bin/python"):
        p = os.path.join(BACKEND, ".venv", rel)
        if os.path.exists(p):
            return p
    return sys.executable  # 兜底：用当前解释器


def warn(msg: str) -> None:
    print(f"  [警告] {msg}")


def launch(name: str, args: list[str], cwd: str) -> None:
    """在新窗口启动一个长驻服务；cmd /k 让出错时窗口不闪退。"""
    print(f"  启动 {name} ...")
    if IS_WINDOWS:
        subprocess.Popen(
            ["cmd", "/k", *args],
            cwd=cwd,
            creationflags=CREATE_NEW_CONSOLE,
        )
    else:
        subprocess.Popen(args, cwd=cwd)


def docker_healthy(container: str, timeout: float = 90.0) -> bool:
    """轮询 docker 容器的健康状态，直到 healthy 或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0 and r.stdout.strip() == "healthy":
            return True
        time.sleep(2)
    return False


def main() -> int:
    print()
    print("  ==========================================================")
    print("   DeepResearch  本地一键启动")
    print("  ==========================================================")
    print()

    # ---- 前置检查（只提示，不中断） ----
    if shutil.which("docker") is None:
        warn("未找到 docker，请先安装并启动 Docker Desktop。")
        return 1
    if not os.path.exists(os.path.join(BACKEND, ".env")):
        warn("backend/.env 不存在，请复制 backend/.env.example 并填入密钥。")
    venv_py = find_venv_python()
    if not os.path.exists(os.path.join(BACKEND, ".venv")):
        warn("backend/.venv 不存在，请先创建虚拟环境并安装依赖。")
    if shutil.which("npm") is None:
        warn("未找到 npm。")
    if not os.path.exists(os.path.join(FRONTEND, "node_modules")):
        warn("frontend/node_modules 不存在，请先 npm install。")

    # ---- 1. DB + Redis ----
    print("  [1/5] 启动数据库与 Redis（Docker）...")
    r = subprocess.run(["docker", "compose", "up", "-d", "db", "redis"], cwd=ROOT)
    if r.returncode != 0:
        warn("Docker 启动失败，请确认 Docker Desktop 正在运行。")
        return 1

    # ---- 2. 等待健康 ----
    print("  [2/5] 等待数据库与 Redis 就绪 ...")
    ok_db = docker_healthy("deepsearch-db")
    ok_redis = docker_healthy("deepsearch-redis")
    if not (ok_db and ok_redis):
        warn("等待 db/redis 就绪超时，仍继续启动（若后端报连接错误请稍后重试）。")

    # ---- 3. 后端 ----
    print("  [3/5] 启动后端（uvicorn :8000）...")
    launch(
        "backend",
        [venv_py, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
        BACKEND,
    )

    # ---- 4. worker ----
    print("  [4/5] 启动 worker ...")
    launch("worker", [venv_py, "-m", "app.worker"], BACKEND)

    # ---- 5. 前端 ----
    print("  [5/5] 启动前端（Vite :5173）...")
    launch("frontend", ["npm", "run", "dev"], FRONTEND)

    print()
    print("  完成！服务清单：")
    print("    DB        localhost:5432")
    print("    Redis     localhost:6379")
    print("    Backend   http://localhost:8000/docs")
    print("    Frontend  http://localhost:5173（若 5173 被占用，Vite 会自动换端口）")
    print()
    print("  停止：关掉各服务窗口；再运行 docker compose down 可停 db/redis。")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
