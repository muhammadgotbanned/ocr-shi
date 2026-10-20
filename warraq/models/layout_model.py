"""PP-DocLayoutV3 page-layout detector (via transformers)."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

from PIL import Image

from . import hardware, hub

log = logging.getLogger(__name__)


@dataclass
class Detection:
    label: str
    score: float
    bbox: tuple[float, float, float, float]
    order: int
    polygon: list[tuple[float, float]] | None


class LayoutModel:
    def __init__(self):
        self.model = None
        self.processor = None
        self.device = "cpu"
        self.signature_: tuple | None = None
        self.lock = threading.RLock()

    def signature(self, settings) -> tuple:
        hw = settings.hardware
        return (settings.models.layout_model, hw.device, hw.gpu_index, hw.layout_device)

    @property
    def is_loaded(self) -> bool:
        return self.model is not None

    def load(self, settings, progress: Callable[[dict], None] | None = None) -> None:
        with self.lock:
            sig = self.signature(settings)
            if self.model is not None and sig == self.signature_:
                return
            self.unload()
            from transformers import PPDocLayoutV3ForObjectDetection, PPDocLayoutV3ImageProcessor

            hub.configure_environment(settings.models)
            path = hub.ensure_model(settings.models.layout_model, settings.models, "main", progress, "PP-DocLayoutV3")
            if settings.hardware.layout_device == "cpu":
                device = "cpu"
            else:
                device, _ = hardware.resolve(settings.hardware)
            if progress:
                progress({"stage": "model", "message": "Loading layout model…"})
            self.processor = PPDocLayoutV3ImageProcessor.from_pretrained(path)
            model = PPDocLayoutV3ForObjectDetection.from_pretrained(path)
            # The detector is small; float32 keeps its box regression precise everywhere.
            self.model = model.to(device).eval()
            self.device = device
            self.signature_ = sig
            log.info("Layout model loaded on %s", device)

    def unload(self) -> None:
        with self.lock:
            self.model = None
            self.processor = None
            self.signature_ = None

    def detect(self, image: Image.Image, threshold: float = 0.3) -> list[Detection]:
        import torch

        with self.lock:
            if self.model is None:
                raise RuntimeError("Layout model is not loaded")
            img = image.convert("RGB")
            inputs = self.processor(images=[img], return_tensors="pt")
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            with torch.inference_mode():
                outputs = self.model(**inputs)
            target = torch.tensor([img.size[::-1]], device=self.device)
            try:
                raw = self.processor.post_process_object_detection(outputs, threshold=threshold, target_sizes=target)[0]
            except Exception as exc:  # malformed masks on odd crops
                log.warning("layout post-processing failed: %s", exc)
                return []
            id2label = self.model.config.id2label
            polys = raw.get("polygon_points")
            orders = raw.get("order_seq")
            dets: list[Detection] = []
            for i in range(len(raw["scores"])):
                box = [float(v) for v in raw["boxes"][i]]
                poly = None
                if polys is not None and i < len(polys) and polys[i] is not None:
                    try:
                        poly = [(float(p[0]), float(p[1])) for p in polys[i]]
                    except Exception:
                        poly = None
                dets.append(
                    Detection(
                        label=str(id2label.get(int(raw["labels"][i]), "text")),
                        score=float(raw["scores"][i]),
                        bbox=(box[0], box[1], box[2], box[3]),
                        order=int(orders[i]) if orders is not None else i,
                        polygon=poly,
                    )
                )
            return dets
