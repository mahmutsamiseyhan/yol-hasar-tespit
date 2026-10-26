"""Tespit ve segmentasyon modellerini aynı test görüntülerinde, insanların çizdiği orijinal kutulara göre kıyaslar.

Neden ayrı bir betik? Segmentasyon modeli SAM'in ürettiği çokgenlerle eğitilir; Ultralytics'in kendi
segmentasyon değerlendirmesi kutuları da bu çokgenlerden türetir. Kıyas adil olsun diye burada her iki
modelin kutu tahmini, prepare_rdd2022.py'nin ürettiği orijinal (insan) kutularıyla ölçülür.

İki model de Ultralytics'in standart tespit doğrulayıcısından `model.val()` ile aynı ayarlarla geçer
(conf=0.001, NMS IoU=0.7, çoklu etiket, rect, IoU 0.50:0.95). Tek fark: segmentasyon modelinin çıktısındaki maske katsayıları NMS'e girmeden ayrılır.

Kullanım:
    python scripts/compare_box_map.py --data /content/rdd_yolo/data.yaml \
        --models tespit=models/best.pt segmentasyon=models/best-seg.pt
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ultralytics.models.yolo.detect import DetectionValidator
from ultralytics.utils import nms

KEYS = ("precision", "recall", "mAP50", "mAP50-95")


class BoxValidator(DetectionValidator):
    """Standart tespit doğrulayıcısı; segmentasyon modelinin kutu çıktısını da ölçebilir."""

    def postprocess(self, preds):
        if isinstance(preds, (list, tuple)):  # segmentasyon: (tahminler, maske prototipleri)
            preds = preds[0]
        # nc açıkça verilir: kutu (4) + sınıf (nc) sonrası kanallar maske katsayısıdır ve sınıf sayılmaz
        outputs = nms.non_max_suppression(
            preds, self.args.conf, self.args.iou, nc=self.nc, multi_label=True,
            agnostic=self.args.single_cls or self.args.agnostic_nms, max_det=self.args.max_det,
            end2end=self.end2end,
        )
        return [{"bboxes": x[:, :4], "conf": x[:, 4], "cls": x[:, 5], "extra": x[:, 6:]} for x in outputs]


def evaluate(model_path: Path, data: Path, split: str, imgsz: int, batch: int, out_dir: Path) -> dict:
    v = BoxValidator(args=dict(model=str(model_path), data=str(data), split=split, imgsz=imgsz, batch=batch,
                               conf=0.001, iou=0.7, rect=True, plots=False, verbose=False,
                               project=str(out_dir), name=model_path.stem, exist_ok=True))
    v()
    m = v.metrics.box
    per_class = {}
    for i, cls_id in enumerate(m.ap_class_index):
        p, r, ap50, ap = m.class_result(i)
        per_class[v.names[int(cls_id)]] = {"precision": round(float(p), 3), "recall": round(float(r), 3),
                                           "AP50": round(float(ap50), 3), "AP50-95": round(float(ap), 3)}
    return {"precision": round(float(m.mp), 3), "recall": round(float(m.mr), 3),
            "mAP50": round(float(m.map50), 3), "mAP50-95": round(float(m.map), 3), "siniflar": per_class}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, required=True, help="Orijinal kutu etiketli veri setinin data.yaml'ı")
    ap.add_argument("--split", default="test")
    ap.add_argument("--models", nargs="+", required=True, help="ad=yol biçiminde, örn. tespit=models/best.pt")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--out", type=Path, help="Sonuçların yazılacağı JSON dosyası")
    args = ap.parse_args()

    work = (args.out.parent if args.out else Path("runs")) / "kiyas"
    results = {}
    for spec in args.models:
        name, _, path = spec.partition("=")
        print(f"\n{name} ({path}) değerlendiriliyor...")
        results[name] = evaluate(Path(path), args.data, args.split, args.imgsz, args.batch, work)

    print("\nİnsan kutularına göre (aynı test görüntüleri):")
    print("model".ljust(16) + "".join(k.rjust(11) for k in KEYS))
    for name, m in results.items():
        print(name.ljust(16) + "".join(f"{m[k]:11.3f}" for k in KEYS))
    classes = sorted({c for m in results.values() for c in m["siniflar"]})
    for c in classes:
        print(f"  {c} AP50: " + "  ".join(f"{n} {m['siniflar'].get(c, {}).get('AP50', float('nan')):.3f}"
                                         for n, m in results.items()))
    if args.out:
        args.out.write_text(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"\nSonuçlar: {args.out}")


if __name__ == "__main__":
    main()
