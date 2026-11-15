"""Canlı tarama (/canli) için telefonda çalışacak modeli hazırlar: .pt -> ONNX + sınıf eşikleri.

Telefonun tarayıcısı her kareyi bu modelle tarar; bu yüzden küçük model seçilir (varsayılan v1, YOLO11s).
Telefonun gönderdiği kareye sunucu ana modelle (best.pt) bir kez daha bakar.

Kullanım:
    python scripts/export_canli_model.py --weights models/best_v1.pt --thresholds models/esikler_v1.json
Çıktı: models/canli.onnx ve models/esikler_canli.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weights", type=Path, default=ROOT / "models" / "best_v1.pt")
    ap.add_argument("--thresholds", type=Path, default=ROOT / "models" / "esikler_v1.json")
    ap.add_argument("--imgsz", type=int, default=640, help="Sunucudaki LIVE_IMGSZ ile aynı olmalı")
    ap.add_argument("--out", type=Path, default=ROOT / "models")
    args = ap.parse_args()

    from ultralytics import YOLO

    with tempfile.TemporaryDirectory() as tmp:
        # Dışa aktarma çıktıyı ağırlık dosyasının yanına yazar; asıl models/ klasörü kirlenmesin
        weights = Path(tmp) / "canli.pt"
        shutil.copy(args.weights, weights)
        onnx = Path(YOLO(str(weights)).export(format="onnx", imgsz=args.imgsz, simplify=True, dynamic=False,
                                              opset=17))
        args.out.mkdir(parents=True, exist_ok=True)
        shutil.copy(onnx, args.out / "canli.onnx")

    thresholds = json.loads(args.thresholds.read_text(encoding="utf-8")) if args.thresholds.exists() else {}
    thresholds = thresholds.get("esikler", thresholds)
    (args.out / "esikler_canli.json").write_text(json.dumps(
        {"esikler": thresholds, "kaynak": f"{args.weights.name}, {args.thresholds.name}", "girdi_boyutu": args.imgsz},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ {args.out / 'canli.onnx'} ({(args.out / 'canli.onnx').stat().st_size / 1e6:.1f} MB)")
    print(f"✓ {args.out / 'esikler_canli.json'}: {thresholds}")


if __name__ == "__main__":
    main()
