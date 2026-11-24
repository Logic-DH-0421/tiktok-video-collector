from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from diagnostics import log


APP_NAME = "TikTokVideoCollector"


def app_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class AppConfig:
    download_dir: str = str(Path.home() / "Downloads" / "TikTok视频")
    min_likes: int = 500_000
    min_comments: int = 0
    min_favorites: int = 0
    min_duration_seconds: int = 0
    max_duration_seconds: int = 0
    checkpoint_every: int = 50
    max_concurrent_downloads: int = 1
    scroll_delay_seconds: float = 2.2
    metric_timeout_seconds: float = 10.0
    download_timeout_seconds: float = 7200.0
    download_idle_timeout_seconds: float = 180.0
    max_file_size_mb: float = 0.0

    def __post_init__(self):
        for name in ("min_likes","min_comments","min_favorites","min_duration_seconds","max_duration_seconds"):
            value=getattr(self,name)
            if isinstance(value,bool) or not isinstance(value,int) or value<0:raise ValueError(name)
        if not isinstance(self.checkpoint_every,int) or self.checkpoint_every<1:raise ValueError("checkpoint_every")
        if not isinstance(self.max_concurrent_downloads,int) or not 1<=self.max_concurrent_downloads<=4:raise ValueError("max_concurrent_downloads")
        for name in ("metric_timeout_seconds","download_timeout_seconds","download_idle_timeout_seconds","scroll_delay_seconds"):
            value=getattr(self,name)
            if not isinstance(value,(float,int)) or not math.isfinite(value) or value<=0:raise ValueError(name)
        if not math.isfinite(self.max_file_size_mb) or self.max_file_size_mb<0:raise ValueError("max_file_size_mb")
        if self.max_duration_seconds and self.max_duration_seconds<self.min_duration_seconds:raise ValueError("duration bounds")

    @classmethod
    def load(cls) -> "AppConfig":
        path = app_data_dir() / "config.json"
        if not path.exists():
            log("INFO", "config", "config.default", "No saved config; defaults selected", config_version=1)
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            allowed = cls.__dataclass_fields__.keys()
            config = cls(**{k: v for k, v in raw.items() if k in allowed})
            log("INFO", "config", "config.loaded", "Configuration loaded", field_count=len(raw), config_version=1)
            return config
        except Exception as exc:
            log("WARN", "config", "config.load_fallback", "Configuration unreadable; defaults selected", error=exc, path=str(path))
            return cls()

    def save(self) -> None:
        path = app_data_dir() / "config.json"
        try:
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)
            log("INFO", "config", "config.saved", "Configuration saved atomically", field_count=len(asdict(self)), config_version=1)
        except Exception as exc:
            log("ERROR", "config", "config.save_failed", "Configuration save failed", error=exc, path=str(path))
            raise


def edge_executable() -> str | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def resource_path(relative: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / relative
