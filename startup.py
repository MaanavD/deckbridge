#!/usr/bin/env python3
"""Install, inspect, or remove Deckbridge's per-user macOS LaunchAgent.

The command is deliberately the whole startup interface.  Callers do not need
to know launchctl domains, plist locations, environment requirements, or which
Deckbridge children constitute a healthy full hardware stack.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


LABEL = "com.deckbridge.agent"
ROOT = Path(__file__).resolve().parent
HOME = Path.home()
UID = os.environ.get("DECKBRIDGE_UID", str(os.getuid()))
DOMAIN = f"gui/{UID}"
TARGET = f"{DOMAIN}/{LABEL}"
LAUNCHCTL = os.environ.get("DECKBRIDGE_LAUNCHCTL", "/bin/launchctl")
RUNTIME_ROOT = Path(
    os.environ.get(
        "DECKBRIDGE_RUNTIME_ROOT",
        str(HOME / "Library" / "Application Support" / "Deckbridge"),
    )
)
RUN_DIR = Path(
    os.environ.get(
        "DECKBRIDGE_INSTALLED_RUN_DIR",
        str(HOME / "Library" / "Caches" / "Deckbridge" / "run"),
    )
)
LOG_DIR = Path(
    os.environ.get(
        "DECKBRIDGE_INSTALLED_LOG_DIR",
        str(HOME / "Library" / "Logs" / "Deckbridge"),
    )
)
DECKBRIDGE = os.environ.get(
    "DECKBRIDGE_COMMAND", str(RUNTIME_ROOT / "deckbridge.sh")
)
AGENTS_DIR = Path(
    os.environ.get(
        "DECKBRIDGE_LAUNCH_AGENTS_DIR",
        str(Path.home() / "Library" / "LaunchAgents"),
    )
)
PLIST_PATH = AGENTS_DIR / f"{LABEL}.plist"


def launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [LAUNCHCTL, *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def wait_for_supervisor_release() -> tuple[bool, str]:
    """Wait until the unloaded generation finishes its EXIT cleanup.

    ``launchctl bootout`` can return before the supervisor has stopped its
    children and released the shared lifecycle lock. Replacing the runtime in
    that gap deletes the old process's cwd; worse, its late cleanup reads the
    shared pidfiles and terminates the newly bootstrapped generation.
    """
    lock = RUN_DIR / "launchd-supervisor.lock"
    timeout = float(os.environ.get("DECKBRIDGE_UNLOAD_TIMEOUT", "10"))
    interval = float(os.environ.get("DECKBRIDGE_UNLOAD_INTERVAL", "0.05"))
    deadline = time.monotonic() + max(0.0, timeout)
    while lock.exists():
        try:
            owner = int((lock / "pid").read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError, OSError):
            owner = 0
        owner_alive = False
        if owner > 0:
            try:
                os.kill(owner, 0)
                owner_alive = True
            except ProcessLookupError:
                owner_alive = False
            except PermissionError:
                owner_alive = True
        if not owner_alive:
            try:
                (lock / "pid").unlink(missing_ok=True)
                lock.rmdir()
            except OSError:
                pass
            if not lock.exists():
                return True, ""
        if time.monotonic() >= deadline:
            return False, f"supervisor cleanup did not finish within {timeout:g}s"
        time.sleep(max(0.01, interval))
    return True, ""


def unload() -> tuple[bool, str]:
    """Unload the active generation without guessing about launchctl errors.

    `bootout` reports an error when a job is already absent, which is harmless,
    but it can also fail while the job remains live.  Only the latter must stop
    install/uninstall: replacing or deleting the runtime underneath that live
    supervisor can strand its children and make the next bootstrap ambiguous.
    """
    result = launchctl("bootout", TARGET)
    if result.returncode == 0 or launchctl("print", TARGET).returncode != 0:
        return wait_for_supervisor_release()
    detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
    return False, detail


def health(
    *, quiet: bool = False, feeds: bool = False, connections: bool = False
) -> bool:
    environment = os.environ.copy()
    environment.update(
        {
            "DECKBRIDGE_RUN_DIR": str(RUN_DIR),
            "DECKBRIDGE_LOG_DIR": str(LOG_DIR),
            "OPEN_BROWSER": "0",
        }
    )
    command = [DECKBRIDGE, "health", "--full", "--hw"]
    if feeds:
        command.append("--feeds")
    if connections:
        command.append("--connections")
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=environment,
    )
    if not quiet and result.stdout:
        print(result.stdout.rstrip())
    return result.returncode == 0


def plist() -> dict:
    path = ":".join(
        (
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        )
    )
    return {
        "Label": LABEL,
        "Program": str(RUNTIME_ROOT / "deckbridge_launchd.sh"),
        "ProgramArguments": [str(RUNTIME_ROOT / "deckbridge_launchd.sh")],
        # The generated runtime is atomically replaced during every install.
        # launchd may retain this cwd across the bootout/bootstrap boundary;
        # pointing it inside the replaced tree can make the next shell start
        # with ENOENT before deckbridge_launchd.sh gets a chance to cd to its
        # own freshly resolved directory. Its parent is stable across cutover.
        "WorkingDirectory": str(RUNTIME_ROOT.parent),
        "EnvironmentVariables": {
            "HOME": str(HOME),
            "PATH": path,
            "OPEN_BROWSER": "0",
            "PYTHONUNBUFFERED": "1",
            "DECKBRIDGE_RUN_DIR": str(RUN_DIR),
            "DECKBRIDGE_LOG_DIR": str(LOG_DIR),
        },
        "RunAtLoad": True,
        # The runner exits nonzero whenever any required child fails. launchd
        # then owns retry/backoff instead of leaving a half-alive deck behind.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": ["Aqua"],
        "StandardOutPath": str(LOG_DIR / "launchagent.log"),
        "StandardErrorPath": str(LOG_DIR / "launchagent-error.log"),
    }


def write_plist() -> None:
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{LABEL}.", dir=AGENTS_DIR)
    try:
        with os.fdopen(fd, "wb") as f:
            plistlib.dump(plist(), f, fmt=plistlib.FMT_XML, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, PLIST_PATH)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def stage_runtime() -> Path:
    """Copy the source checkout into launchd-readable Application Support.

    launchd is denied access to Downloads/Desktop/Documents by macOS privacy
    controls even for a per-user LaunchAgent. The source checkout remains the
    editable authority; this generated runtime is replaced on every install.
    """
    parent = RUNTIME_ROOT.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = parent / f".{RUNTIME_ROOT.name}.stage-{os.getpid()}"
    shutil.rmtree(stage, ignore_errors=True)

    ignored = shutil.ignore_patterns(
        ".git",
        ".run",
        "logs",
        "__pycache__",
        ".pytest_cache",
        ".DS_Store",
        "*.pyc",
    )
    shutil.copytree(ROOT, stage, symlinks=True, ignore=ignored)
    return stage


def activate_runtime(stage: Path) -> None:
    """Replace the generated runtime, retaining the previous copy on failure."""
    backup = RUNTIME_ROOT.parent / f".{RUNTIME_ROOT.name}.old-{os.getpid()}"
    shutil.rmtree(backup, ignore_errors=True)
    had_runtime = RUNTIME_ROOT.exists()
    if had_runtime:
        os.replace(RUNTIME_ROOT, backup)
    try:
        os.replace(stage, RUNTIME_ROOT)
    except Exception:
        if had_runtime and backup.exists():
            os.replace(backup, RUNTIME_ROOT)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def refresh_hammerspoon_bridge() -> None:
    """Refresh the optional, GUI-trusted Accessibility bridge after cutover."""
    config = HOME / ".hammerspoon" / "init.lua"
    installer = RUNTIME_ROOT / "install_hammerspoon_bridge.py"
    module = RUNTIME_ROOT / "hammerspoon_deckbridge.lua"
    hs = shutil.which("hs")
    if not config.exists() or not installer.exists() or not module.exists() or not hs:
        return
    installed = subprocess.run(
        [sys.executable, str(installer), "--config", str(config),
         "--module", str(module)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    if installed.returncode != 0:
        print(f"warning: Hammerspoon bridge update failed: {installed.stdout.strip()}",
              file=sys.stderr)
        return
    # The module is loaded with dofile, so replacing the generated runtime does
    # not update the in-memory functions until Hammerspoon reloads its config.
    subprocess.run(
        [hs, "-c", "hs.reload()"], text=True, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def install_mic_helper(stage: Path) -> tuple[bool, str]:
    """Install the stable Accessibility identity outside generated runtime.

    Routine startup reinstalls replace ``RUNTIME_ROOT``. Keeping the native app
    under ``~/Applications`` means those updates do not silently replace the
    binary the user granted in Privacy & Security. Exit 4 means the helper is
    correctly installed but still awaits the required one-time user grant.
    """
    if os.environ.get("DECKBRIDGE_SKIP_MIC_HELPER") == "1":
        return True, ""
    installer = stage / "install_mic_helper.sh"
    if not installer.exists():
        return False, f"missing mic helper installer: {installer}"
    result = subprocess.run(
        [str(installer), "install"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    detail = (result.stdout or "").strip()
    return result.returncode in (0, 4), detail


def install() -> int:
    if sys.platform != "darwin" and "DECKBRIDGE_LAUNCHCTL" not in os.environ:
        print("install is supported on macOS only", file=sys.stderr)
        return 2

    try:
        stage = stage_runtime()
        write_plist()
    except Exception as exc:
        print(f"failed to stage Deckbridge runtime: {exc}", file=sys.stderr)
        return 1

    helper_ok, helper_detail = install_mic_helper(stage)
    if not helper_ok:
        shutil.rmtree(stage, ignore_errors=True)
        print(f"failed to install Deckbridge Mic: {helper_detail}", file=sys.stderr)
        return 1
    if helper_detail:
        print(helper_detail)

    # Staging and plist rendering are complete before cutover. An already loaded
    # generation keeps running until this point; bootout gives its EXIT trap time
    # to stop all children before its generated runtime is replaced.
    unloaded, detail = unload()
    if not unloaded:
        shutil.rmtree(stage, ignore_errors=True)
        print(
            f"refusing to replace a still-loaded {LABEL}: {detail}",
            file=sys.stderr,
        )
        return 1
    # A first install can replace a manually started checkout whose pidfiles
    # live under the source tree rather than the LaunchAgent's persistent run
    # directory. Stop that old owner only after staging/bootout, immediately at
    # cutover, so ports and the exclusive USB device are free for launchd.
    if os.environ.get("DECKBRIDGE_SKIP_SOURCE_STOP") != "1":
        source_command = ROOT / "deckbridge.sh"
        if source_command.exists() and source_command != Path(DECKBRIDGE):
            subprocess.run(
                [str(source_command), "stop"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
    try:
        activate_runtime(stage)
    except Exception as exc:
        shutil.rmtree(stage, ignore_errors=True)
        print(f"failed to activate Deckbridge runtime: {exc}", file=sys.stderr)
        return 1
    refresh_hammerspoon_bridge()
    bootstrap_timeout = float(
        os.environ.get("DECKBRIDGE_BOOTSTRAP_TIMEOUT", "8")
    )
    bootstrap_interval = float(
        os.environ.get("DECKBRIDGE_BOOTSTRAP_INTERVAL", "0.5")
    )
    bootstrap_deadline = time.monotonic() + bootstrap_timeout
    while True:
        loaded = launchctl("bootstrap", DOMAIN, str(PLIST_PATH))
        if loaded.returncode == 0 or time.monotonic() >= bootstrap_deadline:
            break
        # macOS can return EIO for a short window after bootout even though the
        # old job is no longer printable. A bounded retry bridges that domain
        # teardown window without hiding a persistently invalid plist.
        time.sleep(bootstrap_interval)
    if loaded.returncode != 0:
        detail = loaded.stderr.strip() or loaded.stdout.strip() or "unknown error"
        print(f"failed to bootstrap {LABEL}: {detail}", file=sys.stderr)
        return 1

    timeout = float(os.environ.get("DECKBRIDGE_STARTUP_TIMEOUT", "30"))
    interval = float(os.environ.get("DECKBRIDGE_STARTUP_INTERVAL", "1"))
    deadline = time.monotonic() + timeout
    while time.monotonic() <= deadline:
        if launchctl("print", TARGET).returncode == 0 and health(quiet=True):
            print(f"installed and healthy: {LABEL}")
            print(f"plist: {PLIST_PATH}")
            return 0
        time.sleep(interval)

    print(f"installed {LABEL}, but its full child stack is not healthy", file=sys.stderr)
    print("inspect: ./install_startup.sh status", file=sys.stderr)
    health()
    return 1


def status() -> int:
    loaded = launchctl("print", TARGET)
    if loaded.returncode != 0:
        print(f"not loaded: {LABEL}")
        if PLIST_PATH.exists():
            print(f"plist exists at {PLIST_PATH}; run: ./install_startup.sh install")
        else:
            print("run: ./install_startup.sh install")
        return 1

    print(f"loaded: {LABEL}")
    if health(feeds=True):
        print("startup service, required children, and external feeds are healthy")
        return 0
    print("startup service is loaded, but required child health check failed")
    return 1


def connections() -> int:
    """Check every installed transport, including optional USB availability."""
    loaded = launchctl("print", TARGET)
    if loaded.returncode != 0:
        print(f"not loaded: {LABEL}")
        return 1
    print(f"loaded: {LABEL}")
    if health(feeds=True, connections=True):
        print("all installed Deckbridge connections are healthy")
        return 0
    print("startup service is running, but one or more connections are degraded")
    return 1


def uninstall() -> int:
    # bootout is intentionally idempotent: "not loaded" is already the desired
    # state. The runner's TERM/EXIT traps stop its children before it disappears.
    unloaded, detail = unload()
    if not unloaded:
        print(
            f"refusing to remove a still-loaded {LABEL}: {detail}",
            file=sys.stderr,
        )
        return 1
    try:
        PLIST_PATH.unlink()
    except FileNotFoundError:
        pass
    shutil.rmtree(RUNTIME_ROOT, ignore_errors=True)
    print(f"uninstalled: {LABEL}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"install", "status", "connections", "uninstall"}:
        print(
            f"usage: {Path(argv[0]).name} install|status|connections|uninstall",
            file=sys.stderr,
        )
        return 2
    return {
        "install": install,
        "status": status,
        "connections": connections,
        "uninstall": uninstall,
    }[argv[1]]()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
