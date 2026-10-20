"""Compute-device discovery and selection (CUDA / Apple Metal / CPU)."""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")


@dataclass
class DeviceInfo:
    kind: str  # cuda | mps | cpu
    name: str
    index: int = 0
    memory_gb: float | None = None
    capability: str | None = None
    bf16: bool = False


@dataclass
class HardwareReport:
    os: str
    python: str
    torch: str | None
    cuda: str | None
    devices: list[DeviceInfo] = field(default_factory=list)
    ram_gb: float | None = None
    cpu: str = ""
    cpu_threads: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["best"] = asdict(self.devices[0]) if self.devices else None
        return d


def _torch():
    try:
        import torch  # noqa: F401

        return torch
    except Exception:  # pragma: no cover - torch missing
        return None


def detect() -> HardwareReport:
    torch = _torch()
    try:
        import psutil

        ram = round(psutil.virtual_memory().total / 1024**3, 1)
    except Exception:  # pragma: no cover
        ram = None
    report = HardwareReport(
        os=f"{platform.system()} {platform.release()} ({platform.machine()})",
        python=sys.version.split()[0],
        torch=getattr(torch, "__version__", None),
        cuda=getattr(getattr(torch, "version", None), "cuda", None) if torch else None,
        ram_gb=ram,
        cpu=_cpu_name(),
        cpu_threads=os.cpu_count() or 1,
    )
    if torch is None:
        report.warnings.append("PyTorch is not installed. Run the installer again.")
        report.devices.append(DeviceInfo("cpu", report.cpu or "CPU"))
        return report
    try:
        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                props = torch.cuda.get_device_properties(i)
                report.devices.append(
                    DeviceInfo(
                        kind="cuda",
                        name=props.name,
                        index=i,
                        memory_gb=round(props.total_memory / 1024**3, 1),
                        capability=f"{props.major}.{props.minor}",
                        bf16=props.major >= 8,
                    )
                )
    except Exception as exc:  # pragma: no cover - broken driver
        report.warnings.append(f"CUDA error: {exc}")
    try:
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            report.devices.append(DeviceInfo(kind="mps", name=_apple_chip_name(), memory_gb=ram, bf16=False))
    except Exception:  # pragma: no cover
        pass
    report.devices.append(DeviceInfo("cpu", report.cpu or "CPU", memory_gb=ram))
    if sys.platform == "win32" and not any(d.kind == "cuda" for d in report.devices):
        report.warnings.append(
            "No NVIDIA GPU detected — running on the CPU will be slow. If you have an NVIDIA card, update its driver and re-run the installer."
        )
    if sys.platform == "darwin" and platform.machine() != "arm64":
        report.warnings.append("Intel Macs are not supported by current PyTorch builds; an Apple Silicon Mac is recommended.")
    return report


def _cpu_name() -> str:
    name = ""
    try:
        if sys.platform == "darwin":
            import subprocess

            name = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=2).stdout.strip()
        elif sys.platform == "win32":
            import winreg  # type: ignore

            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            name = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        else:
            with open("/proc/cpuinfo", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("model name"):
                        name = line.split(":", 1)[1].strip()
                        break
    except Exception:  # pragma: no cover
        pass
    name = " ".join(name.replace("(R)", "").replace("(TM)", "").replace("CPU", "").split())
    return name or "CPU"


def _apple_chip_name() -> str:
    try:
        import subprocess

        out = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=2)
        name = out.stdout.strip()
        return name or "Apple GPU"
    except Exception:  # pragma: no cover
        return "Apple GPU"


def resolve(hw_settings) -> tuple[str, Any]:
    """Return (device string, torch dtype) from the hardware settings."""
    torch = _torch()
    if torch is None:
        raise RuntimeError("PyTorch is not installed.")
    want = hw_settings.device
    device = "cpu"
    if want in ("auto", "cuda") and torch.cuda.is_available():
        idx = min(hw_settings.gpu_index, torch.cuda.device_count() - 1)
        device = f"cuda:{idx}"
    elif want in ("auto", "mps") and getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        device = "mps"
    elif want in ("cuda", "mps"):
        device = "cpu"  # requested accelerator unavailable; fall back gracefully
    dtype_name = hw_settings.dtype
    if dtype_name == "auto":
        if device.startswith("cuda"):
            major = torch.cuda.get_device_properties(int(device.split(":")[1])).major
            dtype_name = "bfloat16" if major >= 8 else "float16"
        elif device == "mps":
            dtype_name = "float16"
        else:
            dtype_name = "float32"
    if device == "cpu" and dtype_name == "float16":
        dtype_name = "float32"  # fp16 matmuls are not supported well on CPU
    dtype = {"bfloat16": torch.bfloat16, "float16": torch.float16, "float32": torch.float32}[dtype_name]
    if hw_settings.cpu_threads:
        try:
            torch.set_num_threads(int(hw_settings.cpu_threads))
        except Exception:  # pragma: no cover
            pass
    return device, dtype


def describe(device: str) -> str:
    torch = _torch()
    if torch is None:
        return "CPU"
    if device.startswith("cuda"):
        idx = int(device.split(":")[1]) if ":" in device else 0
        props = torch.cuda.get_device_properties(idx)
        return f"{props.name} · {props.total_memory / 1024**3:.0f} GB"
    if device == "mps":
        return f"{_apple_chip_name()} · Metal"
    return "CPU"


def device_memory_gb(device: str) -> float | None:
    """Total memory of the given device (GPU VRAM, or system RAM for mps/cpu)."""
    torch = _torch()
    try:
        if torch is not None and device.startswith("cuda"):
            idx = int(device.split(":")[1]) if ":" in device else 0
            return torch.cuda.get_device_properties(idx).total_memory / 1024**3
        import psutil

        return psutil.virtual_memory().total / 1024**3
    except Exception:  # pragma: no cover
        return None


def empty_cache() -> None:
    torch = _torch()
    if torch is None:
        return
    try:
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:  # pragma: no cover
        pass
