"""IA local do FTCam: objetos (YOLOX), rostos (YuNet + SFace). Tudo roda na CPU, sem nuvem.

Modelos do OpenCV Model Zoo (Apache-2.0 / MIT), ficam na pasta 'modelos'.
"""
import json
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np

from core import APP_DIR, DATA_DIR, LOG, RES_DIR, imread, imwrite

COCO = ["person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat", "traffic light",
        "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
        "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
        "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove", "skateboard", "surfboard",
        "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
        "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
        "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard",
        "cell phone", "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
        "scissors", "teddy bear", "hair drier", "toothbrush"]

KIND = {"person": "pessoa", "car": "veículo", "truck": "veículo", "bus": "veículo", "motorcycle": "veículo",
        "bicycle": "veículo", "bird": "animal", "cat": "animal", "dog": "animal", "horse": "animal",
        "cow": "animal", "sheep": "animal", "backpack": "pacote", "suitcase": "pacote", "handbag": "pacote"}
LABEL_PT = {"person": "pessoa", "car": "carro", "truck": "caminhão", "bus": "ônibus", "motorcycle": "moto",
            "bicycle": "bicicleta", "bird": "pássaro", "cat": "gato", "dog": "cachorro", "horse": "cavalo",
            "cow": "vaca", "sheep": "ovelha", "backpack": "mochila", "suitcase": "mala", "handbag": "bolsa"}

FACES_DIR = DATA_DIR / "rostos"
UNKNOWN_DIR = DATA_DIR / "rostos_desconhecidos"
FACES_DB = DATA_DIR / "rostos.json"
PETS_DIR = DATA_DIR / "pets"
PETS_DB = DATA_DIR / "pets.json"
MATCH_THRESHOLD = 0.40  # similaridade cosseno (SFace recomenda ~0.363)


def model_path(name):
    for d in (RES_DIR / "modelos", APP_DIR / "modelos"):
        if (d / name).exists():
            return str(d / name)
    return None


class Brain:
    def __init__(self):
        self.lock = threading.Lock()
        self.obj_net = None
        self.face_det = None
        self.face_rec = None
        self.loaded = False
        self.error = ""
        self.db = {}  # nome -> [embedding]
        self.db_lock = threading.Lock()
        FACES_DIR.mkdir(parents=True, exist_ok=True)
        UNKNOWN_DIR.mkdir(parents=True, exist_ok=True)
        PETS_DIR.mkdir(parents=True, exist_ok=True)
        self._load_db()
        self.pets = {}
        self._load_pets()
        self._grids = None
        self.device = ""
        self.speed_ms = 0

    # ------------------------------------------------------------ modelos --
    def ensure(self):
        if self.loaded:
            return True
        with self.lock:
            if self.loaded:
                return True
            try:
                y = model_path("object_detection_yolox_2022nov.onnx")
                if not y:
                    raise FileNotFoundError("modelo de objetos não encontrado na pasta 'modelos'")
                self.obj_net = cv2.dnn.readNet(y)
                self.device = self._pick_device()
                fd = model_path("face_detection_yunet_2023mar.onnx")
                fr = model_path("face_recognition_sface_2021dec.onnx")
                if fd and fr:
                    self.face_det = cv2.FaceDetectorYN.create(fd, "", (320, 320), 0.75, 0.3, 50)
                    self.face_rec = cv2.FaceRecognizerSF.create(fr, "")
                self.loaded = True
                LOG.info("IA carregada (objetos" + (" + rostos)" if self.face_rec else ")"))
            except Exception as e:
                self.error = str(e)
                LOG.info(f"IA indisponível: {e}")
        return self.loaded

    def _pick_device(self):
        """Testa a IA no processador e na placa de vídeo (OpenCL) e fica com a mais rápida."""
        test = np.random.randint(0, 255, (360, 640, 3), np.uint8)

        def bench():
            self.obj_net.setInput(np.zeros((1, 3, 640, 640), np.float32))
            self.obj_net.forward()  # aquecimento
            t = time.time()
            for _ in range(2):
                self.obj_net.setInput(cv2.dnn.blobFromImage(test, 1, (640, 640)))
                self.obj_net.forward()
            return (time.time() - t) / 2

        self.obj_net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.obj_net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        cpu = bench()
        best, best_t = "processador", cpu
        try:
            if cv2.ocl.haveOpenCL():
                cv2.ocl.setUseOpenCL(True)
                for name, target in (("placa de vídeo (OpenCL)", cv2.dnn.DNN_TARGET_OPENCL),
                                     ("placa de vídeo (OpenCL FP16)", cv2.dnn.DNN_TARGET_OPENCL_FP16)):
                    try:
                        self.obj_net.setPreferableTarget(target)
                        t = bench()
                        if t < best_t * 0.85:  # só troca se for bem mais rápida
                            best, best_t = name, t
                    except Exception:
                        pass
        except Exception:
            pass
        self.obj_net.setPreferableTarget({"processador": cv2.dnn.DNN_TARGET_CPU,
                                          "placa de vídeo (OpenCL)": cv2.dnn.DNN_TARGET_OPENCL,
                                          "placa de vídeo (OpenCL FP16)": cv2.dnn.DNN_TARGET_OPENCL_FP16}[best])
        LOG.info(f"IA rodando no {best}: {best_t * 1000:.0f} ms por imagem (processador: {cpu * 1000:.0f} ms)")
        self.speed_ms = int(best_t * 1000)
        return best

    def available(self):
        return model_path("object_detection_yolox_2022nov.onnx") is not None

    # ------------------------------------------------------------ objetos --
    def _grid(self):
        if self._grids is None:
            grids, strides = [], []
            for s in (8, 16, 32):
                hs, ws = 640 // s, 640 // s
                xv, yv = np.meshgrid(np.arange(ws), np.arange(hs))
                g = np.stack((xv, yv), 2).reshape(1, -1, 2)
                grids.append(g)
                strides.append(np.full((*g.shape[:2], 1), s))
            self._grids = (np.concatenate(grids, 1), np.concatenate(strides, 1))
        return self._grids

    def detect_objects(self, frame, conf=0.45, nms=0.5):
        """Retorna lista de {label, nome, tipo, conf, box:(x1,y1,x2,y2)} em coordenadas do frame."""
        if not self.ensure():
            return []
        h, w = frame.shape[:2]
        r = min(640 / h, 640 / w)
        padded = np.full((640, 640, 3), 114, np.uint8)
        nh, nw = int(h * r), int(w * r)
        padded[:nh, :nw] = cv2.cvtColor(cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR), cv2.COLOR_BGR2RGB)
        blob = padded.astype(np.float32).transpose(2, 0, 1)[None]
        with self.lock:
            self.obj_net.setInput(blob)
            out = self.obj_net.forward()
        dets = out[0]
        grids, strides = self._grid()
        dets[:, :2] = (dets[:, :2] + grids[0]) * strides[0]
        dets[:, 2:4] = np.exp(dets[:, 2:4]) * strides[0]
        scores = dets[:, 4:5] * dets[:, 5:]
        cls = scores.argmax(1)
        sc = scores[np.arange(len(cls)), cls]
        keep = sc > conf
        if not keep.any():
            return []
        b, sc, cls = dets[keep, :4], sc[keep], cls[keep]
        xyxy = np.stack([b[:, 0] - b[:, 2] / 2, b[:, 1] - b[:, 3] / 2, b[:, 0] + b[:, 2] / 2, b[:, 1] + b[:, 3] / 2], 1) / r
        boxes = [[float(x1), float(y1), float(x2 - x1), float(y2 - y1)] for x1, y1, x2, y2 in xyxy]
        idx = cv2.dnn.NMSBoxesBatched(boxes, sc.tolist(), cls.tolist(), conf, nms)
        res = []
        for i in np.array(idx).flatten():
            label = COCO[int(cls[i])]
            if label not in KIND:
                continue
            x1, y1, x2, y2 = xyxy[i]
            res.append({
                "label": label, "nome": LABEL_PT.get(label, label), "tipo": KIND[label], "conf": float(sc[i]),
                "box": (max(0, int(x1)), max(0, int(y1)), min(w - 1, int(x2)), min(h - 1, int(y2))),
            })
        return res

    # -------------------------------------------------------------- rostos --
    def detect_faces(self, frame):
        """Retorna lista de {box, score, emb, nome, sim, row}."""
        if not self.ensure() or not self.face_det:
            return []
        h, w = frame.shape[:2]
        with self.lock:
            self.face_det.setInputSize((w, h))
            _, faces = self.face_det.detect(frame)
        out = []
        if faces is None:
            return out
        for row in faces:
            x, y, fw, fh = row[:4]
            if fw < 24 or fh < 24:  # rosto pequeno demais pra reconhecer
                continue
            with self.lock:
                aligned = self.face_rec.alignCrop(frame, row)
                emb = self.face_rec.feature(aligned).flatten()
            emb = emb / (np.linalg.norm(emb) + 1e-9)
            nome, sim = self.match(emb)
            out.append({"box": (int(x), int(y), int(x + fw), int(y + fh)), "score": float(row[-1]),
                        "emb": emb, "nome": nome, "sim": sim, "crop": aligned})
        return out

    def match(self, emb):
        best, best_sim = None, 0.0
        with self.db_lock:
            for nome, samples in self.db.items():
                for smp in samples:
                    sim = float(np.dot(emb, smp["e"]))
                    if sim > best_sim:
                        best, best_sim = nome, sim
        return (best, best_sim) if best_sim >= MATCH_THRESHOLD else (None, best_sim)

    # ----------------------------------------------- banco de rostos (pessoas) --
    # formato: {nome: [{"f": "arquivo.jpg", "e": embedding, "pb": True/False}]}
    def _load_db(self):
        self.db = {}
        if not FACES_DB.exists():
            return
        try:
            raw = json.loads(FACES_DB.read_text(encoding="utf-8"))
        except Exception:
            return
        for nome, items in raw.items():
            pics = sorted((FACES_DIR / nome).glob("*.jpg")) if (FACES_DIR / nome).exists() else []
            out = []
            for i, it in enumerate(items):
                if isinstance(it, dict):
                    out.append({"f": it.get("f"), "e": np.array(it["e"], np.float32), "pb": bool(it.get("pb"))})
                else:  # formato antigo: só o vetor
                    out.append({"f": pics[i].name if i < len(pics) else None, "e": np.array(it, np.float32), "pb": False})
            self.db[nome] = out

    def _save_db(self):
        FACES_DB.write_text(json.dumps({k: [{"f": x["f"], "e": x["e"].tolist(), "pb": x["pb"]} for x in v]
                                        for k, v in self.db.items()}), encoding="utf-8")

    @staticmethod
    def _is_gray(img):
        if img.ndim == 2:
            return True
        b, g, r = cv2.split(img.astype(np.int16))
        return float(np.mean(np.abs(b - g)) + np.mean(np.abs(g - r))) < 6

    @staticmethod
    def _to_ir(img):
        """Imita a visão noturna (infravermelho): preto e branco, mais contraste."""
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        g = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4)).apply(g)
        return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)

    def _emb_from_aligned(self, aligned):
        with self.lock:
            e = self.face_rec.feature(aligned).flatten()
        return e / (np.linalg.norm(e) + 1e-9)

    def _add_sample(self, nome, aligned, emb=None, auto_pb=True):
        """Guarda uma amostra; se for colorida, cria também a versão preto e branco."""
        nome = "".join(c for c in nome.strip() if c.isalnum() or c in " -_").strip()[:40]
        if not nome or not self.ensure() or not self.face_rec:
            return False
        d = FACES_DIR / nome
        d.mkdir(parents=True, exist_ok=True)
        base = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:4]}"
        gray = self._is_gray(aligned)
        if emb is None:
            emb = self._emb_from_aligned(aligned)
        imwrite((d / f"{base}.jpg"), aligned)
        samples = [{"f": f"{base}.jpg", "e": emb.astype(np.float32), "pb": gray}]
        if auto_pb and not gray:
            ir = self._to_ir(aligned)
            imwrite((d / f"{base}_pb.jpg"), ir)
            samples.append({"f": f"{base}_pb.jpg", "e": self._emb_from_aligned(ir).astype(np.float32), "pb": True})
        with self.db_lock:
            self.db.setdefault(nome, []).extend(samples)
            self._save_db()
        return True

    def save_unknown(self, face, camera):
        """Guarda um rosto desconhecido pra você cadastrar depois (mantém no máx. 200)."""
        fid = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
        imwrite((UNKNOWN_DIR / f"{fid}.jpg"), face["crop"])
        np.save(UNKNOWN_DIR / f"{fid}.npy", face["emb"])
        (UNKNOWN_DIR / f"{fid}.txt").write_text(camera, encoding="utf-8")
        files = sorted(UNKNOWN_DIR.glob("*.jpg"))
        for old in files[:-200]:
            for s in (".jpg", ".npy", ".txt"):
                old.with_suffix(s).unlink(missing_ok=True)
        return fid

    def list_faces(self):
        known = []
        with self.db_lock:
            for nome, samples in sorted(self.db.items()):
                cover = next((x["f"] for x in samples if x["f"] and not x["pb"]), None) or \
                    next((x["f"] for x in samples if x["f"]), None)
                known.append({"nome": nome, "amostras": len(samples),
                              "coloridas": sum(1 for x in samples if not x["pb"]),
                              "pb": sum(1 for x in samples if x["pb"]),
                              "foto": f"{nome}/{cover}" if cover else None})
        unknown = []
        for jpg in sorted(UNKNOWN_DIR.glob("*.jpg"), reverse=True)[:120]:
            cam = jpg.with_suffix(".txt").read_text(encoding="utf-8") if jpg.with_suffix(".txt").exists() else ""
            unknown.append({"id": jpg.stem, "quando": int(jpg.stem.split("_")[0]), "camera": cam})
        return {"conhecidos": known, "desconhecidos": unknown}

    def person_samples(self, nome):
        with self.db_lock:
            return [{"f": x["f"], "pb": x["pb"]} for x in self.db.get(nome, []) if x["f"]]

    def delete_sample(self, nome, f):
        with self.db_lock:
            items = self.db.get(nome, [])
            self.db[nome] = [x for x in items if x["f"] != f]
            if not self.db[nome]:
                self.db.pop(nome)
            self._save_db()
        (FACES_DIR / nome / f).unlink(missing_ok=True)

    def enroll_unknown(self, fid, nome):
        src = UNKNOWN_DIR / f"{fid}.jpg"
        if not src.exists():
            return False
        aligned = imread((src))
        emb = np.load(UNKNOWN_DIR / f"{fid}.npy") if (UNKNOWN_DIR / f"{fid}.npy").exists() else None
        ok = self._add_sample(nome, aligned, emb)
        if ok:
            self.discard_unknown(fid)
        return ok

    def enroll_photo(self, img, nome):
        faces = self.detect_faces(img)
        if not faces:
            return False, "Nenhum rosto encontrado na foto"
        f = max(faces, key=lambda x: (x["box"][2] - x["box"][0]) * (x["box"][3] - x["box"][1]))
        return self._add_sample(nome, f["crop"], f["emb"]), ""

    def discard_unknown(self, fid):
        for s in (".jpg", ".npy", ".txt"):
            (UNKNOWN_DIR / f"{fid}{s}").unlink(missing_ok=True)

    def remove_person(self, nome):
        import shutil
        with self.db_lock:
            self.db.pop(nome, None)
            self._save_db()
        shutil.rmtree(FACES_DIR / nome, ignore_errors=True)

    # -------------------------------------------------------------- pets --
    # Reconhece o pet pela APARÊNCIA (cor/padrão do pelo), não pelo rosto.
    def _pet_sig(self, crop):
        c = cv2.resize(crop, (128, 128))
        gray = cv2.cvtColor(c, cv2.COLOR_BGR2GRAY)
        hg = cv2.calcHist([gray], [0], None, [32], [0, 256])
        cv2.normalize(hg, hg)
        hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
        hc = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256])
        cv2.normalize(hc, hc)
        return {"g": hg.flatten().tolist(), "c": hc.flatten().tolist()}

    def _load_pets(self):
        try:
            self.pets = json.loads(PETS_DB.read_text(encoding="utf-8")) if PETS_DB.exists() else {}
        except Exception:
            self.pets = {}

    def _save_pets(self):
        PETS_DB.write_text(json.dumps(self.pets), encoding="utf-8")

    def add_pet_photo(self, nome, img):
        nome = "".join(c for c in nome.strip() if c.isalnum() or c in " -_").strip()[:40]
        if not nome:
            return False, "Nome inválido"
        objs = [o for o in self.detect_objects(img, conf=0.3) if o["tipo"] == "animal"] if self.ensure() else []
        if objs:
            o = max(objs, key=lambda o: (o["box"][2] - o["box"][0]) * (o["box"][3] - o["box"][1]))
            x1, y1, x2, y2 = o["box"]
            crop, label = img[y1:y2, x1:x2], o["label"]
        else:
            crop, label = img, None
        if crop.size == 0:
            return False, "Não achei o bicho na foto"
        d = PETS_DIR / nome
        d.mkdir(parents=True, exist_ok=True)
        base = f"{int(time.time() * 1000)}"
        th = cv2.resize(crop, (240, int(240 * crop.shape[0] / max(1, crop.shape[1]))))
        imwrite((d / f"{base}.jpg"), th)
        ir = self._to_ir(th)
        imwrite((d / f"{base}_pb.jpg"), ir)
        pet = self.pets.setdefault(nome, {"tipo": label or "cachorro", "amostras": []})
        if label:
            pet["tipo"] = label
        pet["amostras"].append({"f": f"{base}.jpg", **self._pet_sig(crop)})
        pet["amostras"].append({"f": f"{base}_pb.jpg", **self._pet_sig(ir)})
        self._save_pets()
        return True, "" if objs else "Não achei o bicho na foto; usei a foto inteira (melhor mandar uma onde ele apareça bem)"

    def match_pet(self, crop, label):
        """Retorna o nome do pet se o bicho detectado parecer com um cadastrado."""
        if not self.pets or crop.size == 0:
            return None
        sig = self._pet_sig(crop)
        gray = self._is_gray(crop)
        best, best_s = None, 0.0
        for nome, pet in self.pets.items():
            if pet.get("tipo") and pet["tipo"] != label:
                continue
            for a in pet["amostras"]:
                if gray:
                    sc = cv2.compareHist(np.float32(sig["g"]), np.float32(a["g"]), cv2.HISTCMP_CORREL)
                else:
                    sc = 0.6 * cv2.compareHist(np.float32(sig["c"]), np.float32(a["c"]), cv2.HISTCMP_CORREL) + \
                        0.4 * cv2.compareHist(np.float32(sig["g"]), np.float32(a["g"]), cv2.HISTCMP_CORREL)
                if sc > best_s:
                    best, best_s = nome, sc
        return best if best_s >= 0.55 else None

    def list_pets(self):
        return [{"nome": n, "tipo": LABEL_PT.get(p.get("tipo"), p.get("tipo")), "amostras": [a["f"] for a in p["amostras"]]}
                for n, p in sorted(self.pets.items())]

    def delete_pet_sample(self, nome, f):
        p = self.pets.get(nome)
        if not p:
            return
        p["amostras"] = [a for a in p["amostras"] if a["f"] != f]
        if not p["amostras"]:
            self.pets.pop(nome)
        self._save_pets()
        (PETS_DIR / nome / f).unlink(missing_ok=True)

    def remove_pet(self, nome):
        import shutil
        self.pets.pop(nome, None)
        self._save_pets()
        shutil.rmtree(PETS_DIR / nome, ignore_errors=True)


BRAIN = Brain()


def in_grid(box, grid, w, h, cols=16, rows=9):
    """Usa o 'pé' do objeto (base central da caixa) pra saber se está numa área marcada."""
    if not grid or len(grid) != cols * rows or all(grid):
        return True
    x = (box[0] + box[2]) / 2
    y = box[3]
    c = min(cols - 1, max(0, int(x / w * cols)))
    r = min(rows - 1, max(0, int(y / h * rows)))
    if grid[r * cols + c]:
        return True
    # também aceita se o centro da caixa estiver numa área marcada
    yc = (box[1] + box[3]) / 2
    r2 = min(rows - 1, max(0, int(yc / h * rows)))
    return bool(grid[r2 * cols + c])


def grid_mask(grid, w, h, cols=16, rows=9):
    """Máscara 0/255 do tamanho da imagem pra detecção de movimento do FTCam."""
    if not grid or len(grid) != cols * rows or all(grid):
        return None
    m = np.zeros((rows, cols), np.uint8)
    for i, v in enumerate(grid):
        if v:
            m[i // cols, i % cols] = 255
    return cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST)


COLORS = {"pessoa": (79, 77, 255), "veículo": (36, 165, 245), "animal": (113, 204, 46), "pacote": (255, 141, 76)}


def annotate(frame, objs, faces):
    img = frame.copy()
    th = max(1, img.shape[1] // 400)
    for o in objs:
        x1, y1, x2, y2 = o["box"]
        col = COLORS.get(o["tipo"], (255, 255, 255))
        cv2.rectangle(img, (x1, y1), (x2, y2), col, th + 1)
        txt = f"{o['nome']} {int(o['conf'] * 100)}%"
        (tw, tth), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.45 * th, th)
        cv2.rectangle(img, (x1, max(0, y1 - tth - 6)), (x1 + tw + 6, y1), col, -1)
        cv2.putText(img, txt, (x1 + 3, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45 * th, (0, 0, 0), th, cv2.LINE_AA)
    for f in faces:
        x1, y1, x2, y2 = f["box"]
        col = (46, 204, 113) if f["nome"] else (0, 215, 255)
        cv2.rectangle(img, (x1, y1), (x2, y2), col, th + 1)
        cv2.putText(img, f["nome"] or "desconhecido", (x1, y2 + 14 * th), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45 * th, col, th, cv2.LINE_AA)
    return img
