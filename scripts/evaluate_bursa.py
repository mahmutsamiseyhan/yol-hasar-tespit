"""Bursa test setinde modeli ölçer ve RDD2022 test sonucuyla karşılaştırır.

Girdi: etiket düzeltme aracında (/etiket) incelenmiş görüntüler.
  - "tamam" (hasarlı) ve "hasarsiz" görüntüler değerlendirmeye girer
  - "kapsam_disi" (yol görünmüyor) ve henüz incelenmemiş görüntüler dışarıda kalır

İki tür sonuç üretir:
  1. Standart nesne tespiti metrikleri (precision, recall, mAP50): RDD sonucuyla birebir kıyas için
  2. Görüntü düzeyi, belediye için anlaşılır metrikler (uygulamanın güven eşiğinde):
     - Sağlam yolda yanlış alarm oranı: hasarsız görüntülerin yüzde kaçında model hasar dedi
     - Hasarlı yolda yakalama oranı: hasarlı görüntülerin yüzde kaçında en az bir hasar doğru bölgede bulundu

Kullanım:
    python scripts/evaluate_bursa.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.labeling import CLASSES, bounding_box, format_shape, read_shapes  # noqa: E402

RDD_REFERENCE = {"precision": 0.622, "recall": 0.578, "mAP50": 0.621, "mAP50-95": 0.316}


def collect_reviewed(label_dir: Path) -> tuple[list[dict], dict]:
    states_path = label_dir / "_durum.json"
    states = json.loads(states_path.read_text(encoding="utf-8")) if states_path.exists() else {}
    images = sorted(p for p in label_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
    reviewed, skipped = [], {"kapsam_disi": 0, "incelenmemis": 0}
    for img in images:
        status = states.get(img.name, {}).get("durum")
        if status in ("tamam", "hasarsiz"):
            shapes = read_shapes(img.with_suffix(".txt")) if status == "tamam" else []
            reviewed.append({"image": img, "status": status, "shapes": shapes,
                             "boxes": [bounding_box(sh["noktalar"]) for sh in shapes]})
        elif status == "kapsam_disi":
            skipped["kapsam_disi"] += 1
        else:
            skipped["incelenmemis"] += 1
    return reviewed, skipped


def build_yolo_dataset(reviewed: list[dict], out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    (out / "images" / "test").mkdir(parents=True)
    (out / "labels" / "test").mkdir(parents=True)
    for r in reviewed:
        shutil.copy2(r["image"], out / "images" / "test" / r["image"].name)
        # Çokgenler segmentasyon formatında yazılır; tespit değerlendirmesinde Ultralytics bunları kutuya çevirir
        lines = [format_shape(sh["sinif"], sh["noktalar"]) for sh in r["shapes"]]
        (out / "labels" / "test" / f"{r['image'].stem}.txt").write_text("\n".join(lines))
    yaml = out / "data.yaml"
    yaml.write_text(f"path: {out.resolve()}\ntrain: images/test\nval: images/test\ntest: images/test\n"
                    "names:\n" + "".join(f"  {i}: {c}\n" for i, c in enumerate(CLASSES)))
    return yaml


def iou(a: dict, b: dict) -> float:
    ax1, ay1, ax2, ay2 = a["x"] - a["w"] / 2, a["y"] - a["h"] / 2, a["x"] + a["w"] / 2, a["y"] + a["h"] / 2
    bx1, by1, bx2, by2 = b["x"] - b["w"] / 2, b["y"] - b["h"] / 2, b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
    inter = max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(0.0, min(ay2, by2) - max(ay1, by1))
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


def image_level(reviewed: list[dict], predictions: dict[str, list[dict]], min_iou: float = 0.3) -> dict:
    """Görüntü düzeyi metrikler. Bölge eşleşmesinde sınıf aranmaz (hasar var mı, doğru yerde mi?)."""
    clean = [r for r in reviewed if r["status"] == "hasarsiz"]
    damaged = [r for r in reviewed if r["status"] == "tamam"]
    false_alarm = [r["image"].name for r in clean if predictions.get(r["image"].name)]
    caught = [r["image"].name for r in damaged
              if any(iou(p, g) >= min_iou for p in predictions.get(r["image"].name, []) for g in r["boxes"])]
    return {
        "hasarsiz_goruntu": len(clean),
        "yanlis_alarm_goruntu": len(false_alarm),
        "yanlis_alarm_orani": round(len(false_alarm) / len(clean), 3) if clean else None,
        "hasarli_goruntu": len(damaged),
        "yakalanan_goruntu": len(caught),
        "yakalama_orani": round(len(caught) / len(damaged), 3) if damaged else None,
        "yanlis_alarm_dosyalari": false_alarm,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--labels", type=Path, default=ROOT / "data" / "bursa_test" / "etiket")
    ap.add_argument("--model", type=Path, default=ROOT / "models" / "best.pt")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "bursa_test" / "degerlendirme")
    ap.add_argument("--conf", type=float, default=0.35, help="Uygulamadaki güven eşiği")
    args = ap.parse_args()

    reviewed, skipped = collect_reviewed(args.labels)
    if not reviewed:
        raise SystemExit("İncelenmiş görüntü yok. Önce http://localhost:8000/etiket sayfasında etiketleri kontrol edin.")
    n_boxes = sum(len(r["shapes"]) for r in reviewed)
    print(f"{len(reviewed)} görüntü ({n_boxes} hasar şekli) değerlendirilecek; "
          f"yol görünmeyen {skipped['kapsam_disi']}, incelenmemiş {skipped['incelenmemis']} görüntü dışarıda.")

    from ultralytics import YOLO

    model = YOLO(str(args.model))
    yaml = build_yolo_dataset(reviewed, args.out / "veri")

    detection = None
    if n_boxes:
        m = model.val(data=str(yaml), split="test", imgsz=640, batch=8, plots=True, verbose=False,
                      project=str(args.out.resolve()), name="metrikler", exist_ok=True)
        per_class = {}
        for i, cls_id in enumerate(m.box.ap_class_index):
            p, r, ap50, ap = m.box.class_result(i)
            per_class[m.names[int(cls_id)]] = {"precision": round(p, 3), "recall": round(r, 3),
                                               "AP50": round(ap50, 3), "AP50-95": round(ap, 3)}
        detection = {"precision": round(m.box.mp, 3), "recall": round(m.box.mr, 3),
                     "mAP50": round(m.box.map50, 3), "mAP50-95": round(m.box.map, 3), "siniflar": per_class}

    predictions = {}
    for r in reviewed:
        res = model.predict(str(r["image"]), conf=args.conf, imgsz=640, verbose=False)[0]
        predictions[r["image"].name] = [{"x": b[0], "y": b[1], "w": b[2], "h": b[3]} for b in res.boxes.xywhn.tolist()]
    images = image_level(reviewed, predictions)

    result = {"goruntu_sayisi": len(reviewed), "hasar_kutusu": n_boxes, "dislanan": skipped,
              "tespit_metrikleri": detection, "rdd2022_referans": RDD_REFERENCE,
              "goruntu_duzeyi": {**images, "guven_esigi": args.conf}}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "bursa_metrikleri.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n=== Nesne tespiti: RDD2022 test vs Bursa test ===")
    if detection:
        for k in ("precision", "recall", "mAP50", "mAP50-95"):
            print(f"  {k:<10} RDD {RDD_REFERENCE[k]:.3f}   Bursa {detection[k]:.3f}")
        for c, v in detection["siniflar"].items():
            print(f"  {c}: AP50 {v['AP50']:.3f}  (P {v['precision']:.3f}, R {v['recall']:.3f})")
    else:
        print("  Hasarlı görüntü yok; yalnızca yanlış alarm oranı ölçüldü.")
    print(f"\n=== Görüntü düzeyi (güven eşiği {args.conf}) ===")
    if images["yanlis_alarm_orani"] is not None:
        print(f"  Sağlam yolda yanlış alarm: {images['yanlis_alarm_goruntu']}/{images['hasarsiz_goruntu']} "
              f"(%{100 * images['yanlis_alarm_orani']:.0f})")
    if images["yakalama_orani"] is not None:
        print(f"  Hasarlı yolda yakalama:    {images['yakalanan_goruntu']}/{images['hasarli_goruntu']} "
              f"(%{100 * images['yakalama_orani']:.0f})")
    print(f"\nSonuçlar: {args.out / 'bursa_metrikleri.json'}")


if __name__ == "__main__":
    main()
