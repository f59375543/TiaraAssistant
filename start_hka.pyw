"""Window launcher and supervised loopback service for the portable Windows build."""
import msvcrt
import os
from pathlib import Path
import shutil
import subprocess
import sys
import traceback

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def main():
    if "--desktop-service" not in sys.argv and "--no-browser" not in sys.argv:
        subprocess.Popen([str(ROOT / "蒂亚拉小助手.exe")], cwd=ROOT)
        return

    desktop = "--desktop-service" in sys.argv
    (ROOT / "logs").mkdir(exist_ok=True)
    with (ROOT / "assistant.lock").open("a+b") as lock:
        if not lock.tell():
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("此目录已有蒂亚拉小助手服务，请先关闭原来的助手。") from None
        config = ROOT / "config.json"
        if not config.exists():
            shutil.copyfile(ROOT / "config.example.json", config)
        with (ROOT / "logs" / "assistant-service.log").open("a", encoding="utf-8", buffering=1) as log:
            pipe = sys.stdout
            sys.stdout = log
            try:
                from hka.web import serve
                serve(config, 0 if desktop else 8765, fallback_port=True,
                      ready_path=ROOT / "logs" / "assistant-url.txt",
                      ready=(lambda address: print(address, file=pipe, flush=True)) if desktop else None,
                      request_window_close=(lambda: print("close-after-run", file=pipe, flush=True)) if desktop else None,
                      control=sys.stdin if desktop else None, open_browser=False)
            except Exception:
                traceback.print_exc(file=log)
                raise
            finally:
                sys.stdout = pipe


if __name__ == "__main__":
    main()
