"""Pre-flight check (PRD 19.2).

    python -m apps.api.scripts.doctor

Answers one question: will Comgu actually run here, and if not, what do I fix?
Every failed check names the remedy rather than only the symptom — a doctor that
says "Docker not found" and stops has done half the job.

Exit code is the number of hard failures, so it works in CI.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "fail"

MARK = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}


@dataclass
class Check:
    name: str
    status: str
    detail: str
    remedy: str | None = None


results: list[Check] = []


def record(name: str, status: str, detail: str, remedy: str | None = None) -> None:
    results.append(Check(name, status, detail, remedy))


def sh(*args: str, timeout: int = 10) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or p.stderr).strip()
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


# --- host --------------------------------------------------------------------


def check_python() -> None:
    v = sys.version_info
    if (v.major, v.minor) == (3, 11):
        record("python", OK, platform.python_version())
    else:
        record(
            "python", WARN, f"{platform.python_version()} (expected 3.11)",
            "DataHub's SDK is only tested on 3.11: uv venv --python 3.11",
        )


def check_architecture() -> None:
    m = platform.machine()
    record("architecture", OK, f"{platform.system()} {m}")
    if m in ("arm64", "aarch64"):
        record(
            "datahub images", WARN, "arm64 host",
            "DataHub quickstart images are x86-first; prefer a remote x86 host "
            "(see infra/README.md) over emulation",
        )


def _bytes_h(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def check_memory() -> None:
    total = None
    if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names:
        try:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError):
            total = None
    if total is None:
        rc, out = sh("sysctl", "-n", "hw.memsize")
        total = int(out) if rc == 0 and out.isdigit() else None

    if total is None:
        record("memory", WARN, "could not determine", None)
        return

    gb = total / (1024 ** 3)
    if gb >= 15:
        record("memory", OK, f"{gb:.0f} GB")
    elif gb >= 7:
        record(
            "memory", WARN, f"{gb:.0f} GB",
            "DataHub quickstart wants ~8 GB on its own; run DataHub remotely and "
            "point DATAHUB_GMS_URL at it",
        )
    else:
        record("memory", FAIL, f"{gb:.0f} GB", "too little to run DataHub locally")


def check_disk() -> None:
    free = shutil.disk_usage(Path.cwd()).free
    gb = free / (1024 ** 3)
    if gb >= 20:
        record("disk", OK, f"{_bytes_h(free)} free")
    elif gb >= 13:
        record("disk", WARN, f"{_bytes_h(free)} free", "DataHub quickstart needs ~13 GB")
    else:
        record("disk", FAIL, f"{_bytes_h(free)} free", "free space before starting DataHub")


# --- tooling -----------------------------------------------------------------


def check_clis() -> None:
    required = {
        "git": "required to generate patches and open pull requests",
        "uv": "required to run the lab's own interpreter",
    }
    optional = {
        "gh": "needed only for real pull requests; dry-run works without it",
        "docker": "needed only to run DataHub on this machine",
        "datahub": "needed only to seed or manage a local DataHub",
    }
    for cmd, why in required.items():
        rc, out = sh(cmd, "--version")
        if rc == 0:
            record(f"cli:{cmd}", OK, out.splitlines()[0][:60])
        else:
            record(f"cli:{cmd}", FAIL, "not found", why)
    for cmd, why in optional.items():
        rc, out = sh(cmd, "--version")
        record(
            f"cli:{cmd}",
            OK if rc == 0 else WARN,
            out.splitlines()[0][:60] if rc == 0 else "not found",
            None if rc == 0 else why,
        )


def check_docker() -> None:
    rc, _ = sh("docker", "info")
    if rc == 127:
        record("docker", WARN, "not installed", "only needed to host DataHub locally")
        return
    if rc != 0:
        record("docker", WARN, "installed but not responding", "start Docker, or use a remote DataHub")
        return

    rc, out = sh("docker", "info", "--format", "{{.MemTotal}}")
    if rc == 0 and out.isdigit():
        gb = int(out) / (1024 ** 3)
        record(
            "docker memory",
            OK if gb >= 8 else WARN,
            f"{gb:.1f} GB allocated",
            None if gb >= 8 else "DataHub quickstart wants >=8 GB allocated to Docker",
        )
    else:
        record("docker", OK, "running")


def check_ports() -> None:
    for port, what in ((8000, "Comgu API"), (18080, "DataHub tunnel"), (9002, "DataHub UI")):
        s = socket.socket()
        s.settimeout(0.4)
        free = s.connect_ex(("127.0.0.1", port)) != 0
        s.close()
        if port == 18080:
            # In use here means the tunnel is up, which is what we want.
            record(
                f"port {port}", OK if not free else WARN,
                "tunnel open" if not free else "nothing listening",
                None if not free else "open the DataHub tunnel (see infra/README.md)",
            )
        else:
            record(f"port {port}", OK, f"free ({what})" if free else f"in use ({what} already running?)")


# --- configuration -----------------------------------------------------------


def check_environment() -> None:
    required = {
        "DATAHUB_GMS_URL": "where DataHub lives; without it no run can retrieve context",
    }
    recommended = {
        "COMGU_LAB_PATH": "path to the comgu-commerce-lab checkout",
        "COMGU_AUTH_SECRET": "without it, issued tokens die on restart",
        "COMGU_DEMO_PASSPHRASE": "without it, nobody can sign in to the demo",
    }
    optional = {
        "GITHUB_LAB_REPO": "pull requests are skipped without it",
        "COMGU_PR_LIVE": "pull requests stay dry-run unless set",
        "SHOPIFY_WEBHOOK_SECRET": "webhooks are refused without it",
    }
    for k, why in required.items():
        record(f"env:{k}", OK if os.environ.get(k) else FAIL, "set" if os.environ.get(k) else "unset",
               None if os.environ.get(k) else why)
    for k, why in recommended.items():
        record(f"env:{k}", OK if os.environ.get(k) else WARN, "set" if os.environ.get(k) else "unset",
               None if os.environ.get(k) else why)
    for k, why in optional.items():
        record(f"env:{k}", OK if os.environ.get(k) else WARN, "set" if os.environ.get(k) else "unset",
               None if os.environ.get(k) else why)


def check_lab() -> None:
    from packages.lab import bridge

    try:
        path = bridge.lab_path()
    except Exception as e:
        record("commerce lab", FAIL, str(e)[:70],
               "git clone https://github.com/AmirmLotfy/comgu-commerce-lab")
        return

    record("commerce lab", OK, str(path))
    venv = path / ".venv" / "bin" / "python"
    if venv.exists():
        record("lab interpreter", OK, str(venv))
    else:
        record(
            "lab interpreter", FAIL, "missing",
            f"cd {path} && uv venv --python 3.11 && uv pip install -e '.[dev]' — "
            "validation runs the lab's own interpreter, not Comgu's",
        )


def check_datahub() -> None:
    import json
    import urllib.request

    gms = os.environ.get("DATAHUB_GMS_URL")
    if not gms:
        return
    try:
        with urllib.request.urlopen(gms.rstrip("/") + "/config", timeout=8) as r:
            cfg = json.load(r)
        v = (cfg.get("versions", {}).get("acryldata/datahub", {})).get("version", "?")
        record("datahub", OK, f"reachable, {v}")
    except Exception as e:
        record(
            "datahub", FAIL, f"{type(e).__name__}",
            f"cannot reach {gms} — is the tunnel up? Runs will fail at context retrieval",
        )


# --- entrypoint --------------------------------------------------------------


def main() -> int:
    print("comgu doctor\n" + "─" * 64)
    for fn in (
        check_python, check_architecture, check_memory, check_disk,
        check_clis, check_docker, check_ports,
        check_environment, check_lab, check_datahub,
    ):
        try:
            fn()
        except Exception as e:  # a broken check must not hide the others
            record(fn.__name__, WARN, f"check errored: {type(e).__name__}: {e}")

    for c in results:
        print(f"[{MARK[c.status]}] {c.name:<26} {c.detail}")
        if c.remedy:
            print(f"{'':>10}{'':<26} → {c.remedy}")

    fails = [c for c in results if c.status == FAIL]
    warns = [c for c in results if c.status == WARN]
    print("─" * 64)
    print(f"{len(results)} checks · {len(fails)} failed · {len(warns)} warnings")
    if not fails:
        print("\nReady. Next: python -m packages.datahub.smoke")
    return len(fails)


if __name__ == "__main__":
    raise SystemExit(main())
