"""API + interface web do FTCam (a mesma tela abre na janela do programa e no navegador)."""
import os
import queue
import shutil
import threading
import time
from functools import wraps

from flask import Flask, Response, abort, jsonify, request, send_file

import base64
import json

import cv2
import numpy as np

import ai
import engine
import onvif
import tapo_ctrl
from core import RES_DIR, VERSAO, find_ffmpeg, set_autostart

try:
    import psutil
except Exception:
    psutil = None


WIN = {"w": None, "cheia": False}  # janela do app (preenchida pelo app.py)
UPD = {"restart_args": lambda: []}  # preenchido pelo app.py


def _move_recordings(src, dst):
    """Mudou a pasta das gravações: move tudo da pasta antiga pra nova."""
    from core import LOG
    try:
        if not src or not src.exists() or src.resolve() == dst.resolve():
            return
        if dst.resolve().is_relative_to(src.resolve()):
            return  # pasta nova dentro da antiga: não mexe
        n = 0
        for item in list(src.iterdir()):
            alvo = dst / item.name
            if item.name == "_buffer":
                shutil.rmtree(item, ignore_errors=True)
                continue
            if item.is_dir() and alvo.exists():
                for f in item.rglob("*"):
                    if f.is_file():
                        t = alvo / f.relative_to(item)
                        t.parent.mkdir(parents=True, exist_ok=True)
                        if not t.exists():
                            shutil.move(str(f), str(t)); n += 1
                shutil.rmtree(item, ignore_errors=True)
            elif not alvo.exists():
                shutil.move(str(item), str(alvo)); n += 1
        LOG.info(f"gravações movidas de {src} para {dst} ({n} itens)")
    except Exception as e:
        LOG.info(f"não consegui mover todas as gravações antigas: {e}")


def create_app(cfg, mgr):
    app = Flask(__name__, static_folder=None)
    app.config["JSON_AS_ASCII"] = False
    discover_state = {"rodando": False, "resultado": [], "quando": None}

    def auth(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            senha = cfg.get()["painel"].get("senha") or ""
            local = request.remote_addr in ("127.0.0.1", "::1")
            if senha and not local:
                a_ = request.authorization
                if not a_ or a_.password != senha:
                    return Response("Senha necessária", 401, {"WWW-Authenticate": 'Basic realm="FTCam"'})
            return fn(*a, **kw)
        return wrapper

    def public_cam(c):
        c = dict(c)
        c["senha"] = "••••••" if c.get("senha") else ""
        c["senha_nuvem_tapo"] = "••••••" if c.get("senha_nuvem_tapo") else ""
        return c

    def cam_by_id(cid):
        for c in cfg.get()["cameras"]:
            if c["id"] == cid:
                return c
        abort(404)

    # ------------------------------------------------------------- páginas --
    @app.get("/")
    @auth
    def index():
        return send_file(RES_DIR / "ui" / "index.html")

    @app.get("/icone.png")
    def icon():
        p = RES_DIR / "icon.png"
        return send_file(p) if p.exists() else abort(404)

    # -------------------------------------------------------------- estado --
    @app.get("/api/estado")
    @auth
    def estado():
        c = cfg.get()
        root = cfg.recordings_dir()
        du = shutil.disk_usage(root)
        sistema = {
            "versao": VERSAO,
            "ffmpeg": bool(find_ffmpeg()),
            "disco_livre_gb": round(du.free / 1024 ** 3, 1),
            "disco_total_gb": round(du.total / 1024 ** 3, 1),
            "pasta": str(root),
            "ip": onvif.local_ip(),
            "porta": c["painel"]["porta"],
            "tapo_extra": tapo_ctrl.available(),
            "ia": ai.BRAIN.available(),
            "ia_erro": ai.BRAIN.error,
            "ia_onde": ai.BRAIN.device,
            "ia_ms": ai.BRAIN.speed_ms,
        }
        if psutil:
            vm = psutil.virtual_memory()
            sistema.update({
                "cpu": psutil.cpu_percent(interval=None),
                "mem_usada_gb": round(vm.used / 1024 ** 3, 1),
                "mem_total_gb": round(vm.total / 1024 ** 3, 1),
            })
        cams = []
        for cam in c["cameras"]:
            w = mgr.worker(cam["id"])
            s = w.status() if w else {
                "id": cam["id"], "nome": cam["nome"], "ip": cam["ip"], "modo": cam["deteccao"],
                "online": False, "gravador": "desligada", "ao_vivo": "desligada", "deteccao": "-", "erro": "",
            }
            s["ativa"] = cam["ativa"]
            cams.append(s)
        return jsonify({"sistema": sistema, "cameras": cams, "notificacoes": list(mgr.alerts)[:30],
                        "clipes": engine.CLIP_SEQ[0]})

    # -------------------------------------------------------------- config --
    @app.get("/api/config")
    @auth
    def get_config():
        c = cfg.get()
        c["cameras"] = [public_cam(x) for x in c["cameras"]]
        return jsonify(c)

    @app.post("/api/config")
    @auth
    def set_config():
        data = request.get_json(force=True) or {}
        for k in ("dias_guardar", "espaco_livre_minimo_gb", "tamanho_maximo_gb", "segundos_antes", "segundos_depois",
                  "clipe_minimo_segundos", "clipe_maximo_segundos"):
            if k in data:
                data[k] = max(0, float(data[k])) if k in ("espaco_livre_minimo_gb", "tamanho_maximo_gb") else max(0, int(data[k]))
        if "clipe_minimo_segundos" in data and "clipe_maximo_segundos" in data:
            data["clipe_maximo_segundos"] = max(data["clipe_maximo_segundos"], data["clipe_minimo_segundos"])
        before = cfg.get()
        old_dir = cfg.recordings_dir() if "pasta_gravacoes" in data else None
        new = cfg.update_global(data)
        if "iniciar_com_windows" in data:
            set_autostart(bool(data["iniciar_com_windows"]))
        restart = before["painel"] != new["painel"] or before["pasta_gravacoes"] != new["pasta_gravacoes"]
        if before["pasta_gravacoes"] != new["pasta_gravacoes"]:
            mgr.stop_all()
            _move_recordings(old_dir, cfg.recordings_dir())  # leva os vídeos antigos junto
            mgr.sync()
        threading.Thread(target=mgr.cleanup, daemon=True).start()
        return jsonify({"ok": True, "reiniciar": restart})

    # ------------------------------------------------------------- câmeras --
    @app.post("/api/cameras")
    @auth
    def save_camera():
        data = request.get_json(force=True) or {}
        old = None
        if data.get("id"):
            old = next((c for c in cfg.get()["cameras"] if c["id"] == data["id"]), None)
        if old:  # mantém senhas se vieram mascaradas
            for k in ("senha", "senha_nuvem_tapo"):
                if data.get(k) == "••••••":
                    data[k] = old[k]
        if not data.get("ip"):
            return jsonify({"ok": False, "erro": "Informe o IP"}), 400
        cam = cfg.upsert_camera(data)
        mgr.sync()
        return jsonify({"ok": True, "camera": public_cam(cam)})

    @app.delete("/api/cameras/<cid>")
    @auth
    def delete_camera(cid):
        cfg.remove_camera(cid)
        mgr.sync()
        return jsonify({"ok": True})

    @app.post("/api/cameras/testar")
    @auth
    def test_camera():
        data = request.get_json(force=True) or {}
        if data.get("id") and data.get("senha") == "••••••":
            data["senha"] = cam_by_id(data["id"])["senha"]
        cam = dict(cfg._fix_camera(data))
        ok, res = engine.grab_test_frame(cam)
        onv = None
        if ok:
            try:
                cl = onvif.OnvifClient(cam["ip"], cam["porta_onvif"], cam["usuario"], cam["senha"])
                cl.sync_time()
                cl.events_url()
                onv = True
            except Exception:
                onv = False
        return jsonify({"ok": ok, "imagem" if ok else "erro": res, "onvif": onv})

    @app.post("/api/descobrir")
    @auth
    def start_discover():
        if not discover_state["rodando"]:
            discover_state["rodando"] = True

            def run():
                try:
                    known = {c["ip"] for c in cfg.get()["cameras"]}
                    res = onvif.discover()
                    for r in res:
                        r["ja_adicionada"] = r["ip"] in known
                    discover_state["resultado"] = res
                    discover_state["quando"] = time.time()
                finally:
                    discover_state["rodando"] = False

            threading.Thread(target=run, daemon=True).start()
        return jsonify({"ok": True})

    @app.get("/api/descobrir")
    @auth
    def get_discover():
        return jsonify(discover_state)

    @app.get("/api/cameras/<cid>/tapo")
    @auth
    def tapo_status(cid):
        return jsonify(tapo_ctrl.status(cam_by_id(cid)))

    @app.post("/api/cameras/<cid>/tapo")
    @auth
    def tapo_action(cid):
        d = request.get_json(force=True) or {}
        return jsonify(tapo_ctrl.action(cam_by_id(cid), d.get("acao"), d.get("valor")))

    @app.post("/api/cameras/<cid>/qualidade")
    @auth
    def live_quality(cid):
        alta = bool((request.get_json(force=True) or {}).get("alta"))
        cam = cam_by_id(cid)
        cam["ao_vivo_alta"] = alta
        cfg.upsert_camera(cam)
        w = mgr.worker(cid)
        if w:
            w.set_live_quality(alta)
            mgr.signatures[cid] = mgr._sig(next(c for c in cfg.get()["cameras"] if c["id"] == cid))
        return jsonify({"ok": True})

    @app.post("/api/cameras/<cid>/grade")
    @auth
    def save_grid(cid):
        grade = (request.get_json(force=True) or {}).get("grade") or []
        grade = [1 if v else 0 for v in grade][:144]
        if len(grade) != 144 or all(grade):
            grade = []
        cam = cam_by_id(cid)
        cam["grade"] = grade
        cfg.upsert_camera(cam)
        w = mgr.worker(cid)
        if w:
            w.set_grid(grade)
            mgr.signatures[cid] = mgr._sig(next(c for c in cfg.get()["cameras"] if c["id"] == cid))
        return jsonify({"ok": True})

    # -------------------------------------------------------------- rostos --
    @app.get("/api/rostos")
    @auth
    def faces():
        return jsonify(ai.BRAIN.list_faces())

    @app.get("/rosto/conhecido/<nome>/<arq>")
    @auth
    def face_known(nome, arq):
        p = (ai.FACES_DIR / nome / arq).resolve()
        if ai.FACES_DIR.resolve() not in p.parents or not p.exists():
            abort(404)
        return send_file(p, mimetype="image/jpeg")

    @app.get("/rosto/desconhecido/<fid>")
    @auth
    def face_unknown(fid):
        p = (ai.UNKNOWN_DIR / f"{fid}.jpg").resolve()
        if ai.UNKNOWN_DIR.resolve() not in p.parents or not p.exists():
            abort(404)
        return send_file(p, mimetype="image/jpeg")

    @app.post("/api/rostos/cadastrar")
    @auth
    def face_enroll():
        d = request.get_json(force=True) or {}
        ok = all(ai.BRAIN.enroll_unknown(fid, d.get("nome", "")) for fid in (d.get("ids") or [d.get("id")]))
        return jsonify({"ok": ok})

    @app.post("/api/rostos/foto")
    @auth
    def face_photo():
        d = request.get_json(force=True) or {}
        try:
            raw = base64.b64decode(d.get("imagem", "").split(",")[-1])
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            img = None
        if img is None:
            return jsonify({"ok": False, "erro": "Imagem inválida"})
        ok, err = ai.BRAIN.enroll_photo(img, d.get("nome", ""))
        return jsonify({"ok": ok, "erro": err})

    @app.delete("/api/rostos/desconhecido/<fid>")
    @auth
    def face_discard(fid):
        ai.BRAIN.discard_unknown(fid)
        return jsonify({"ok": True})

    @app.delete("/api/rostos/pessoa/<nome>")
    @auth
    def face_remove(nome):
        ai.BRAIN.remove_person(nome)
        return jsonify({"ok": True})

    LIVE_FIELDS = {"disparar_com", "ia_ao_vivo", "sensibilidade", "reconhecer_rostos", "fps_ao_vivo"}

    @app.post("/api/cameras/<cid>/ajuste")
    @auth
    def quick_setting(cid):
        """Muda um ajuste na hora, sem reiniciar a gravação."""
        d = request.get_json(force=True) or {}
        cam = cam_by_id(cid)
        for k, v in d.items():
            if k in LIVE_FIELDS:
                cam[k] = v
        cfg.upsert_camera(cam)
        w = mgr.worker(cid)
        if w:
            old_fps = w.cam.get("fps_ao_vivo")
            for k, v in d.items():
                if k in LIVE_FIELDS:
                    w.cam[k] = v
            # no Alta o vídeo é o original da câmera (fps dela); o limite vale pro modo Leve
            mgr.signatures[cid] = mgr._sig(next(c for c in cfg.get()["cameras"] if c["id"] == cid))
        return jsonify({"ok": True})

    @app.get("/api/cameras/<cid>/deteccoes")
    @auth
    def detections(cid):
        w = mgr.worker(cid)
        return jsonify(w.detections() if w else {"objetos": [], "dim": [0, 0]})

    @app.get("/api/rostos/pessoa/<nome>")
    @auth
    def person(nome):
        return jsonify(ai.BRAIN.person_samples(nome))

    @app.delete("/api/rostos/pessoa/<nome>/<arq>")
    @auth
    def person_sample_del(nome, arq):
        ai.BRAIN.delete_sample(nome, arq)
        return jsonify({"ok": True})

    def _img_from_body(d):
        try:
            raw = base64.b64decode(d.get("imagem", "").split(",")[-1])
            return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        except Exception:
            return None

    @app.get("/api/pets")
    @auth
    def pets():
        return jsonify(ai.BRAIN.list_pets())

    @app.post("/api/pets/foto")
    @auth
    def pet_photo():
        d = request.get_json(force=True) or {}
        img = _img_from_body(d)
        if img is None:
            return jsonify({"ok": False, "erro": "Imagem inválida"})
        ok, msg = ai.BRAIN.add_pet_photo(d.get("nome", ""), img)
        return jsonify({"ok": ok, "erro": msg if not ok else "", "aviso": msg if ok else ""})

    @app.get("/pet/<nome>/<arq>")
    @auth
    def pet_img(nome, arq):
        p = (ai.PETS_DIR / nome / arq).resolve()
        if ai.PETS_DIR.resolve() not in p.parents or not p.exists():
            abort(404)
        return send_file(p, mimetype="image/jpeg")

    @app.delete("/api/pets/<nome>/<arq>")
    @auth
    def pet_sample_del(nome, arq):
        ai.BRAIN.delete_pet_sample(nome, arq)
        return jsonify({"ok": True})

    @app.delete("/api/pets/<nome>")
    @auth
    def pet_del(nome):
        ai.BRAIN.remove_pet(nome)
        return jsonify({"ok": True})

    @app.post("/api/cameras/<cid>/disparar")
    @auth
    def manual_trigger(cid):
        w = mgr.worker(cid)
        if not w:
            abort(404)
        w.trigger("manual")
        return jsonify({"ok": True})

    # -------------------------------------------------------------- vídeo --
    @app.get("/api/ao-vivo/<cid>")
    @auth
    def live(cid):
        w = mgr.worker(cid)
        if not w:
            abort(404)
        fps = 1 / 15  # até ~15 quadros/s no ao vivo

        def gen():
            last = None
            while True:
                data = w.jpeg()
                if data is not None and data is not last:
                    last = data
                    yield b"--f\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n"
                time.sleep(fps)
                if w.stop_ev.is_set():
                    break

        return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=f",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/hd/<cid>")
    @auth
    def live_hd(cid):
        """Vídeo original da câmera (MP4 fragmentado); a janela decodifica na placa de vídeo."""
        w = mgr.worker(cid)
        if not w or not w.cam.get("ao_vivo_alta"):
            abort(404)
        for _ in range(40):  # espera o cabeçalho (até 10 s)
            if w.hd_init:
                break
            time.sleep(0.25)
        if not w.hd_init:
            abort(503)
        q = w.hd_subscribe()
        init = w.hd_init

        def gen():
            try:
                yield init
                while not w.stop_ev.is_set() and w.cam.get("ao_vivo_alta"):
                    if w.hd_init is not init:
                        break  # gravador reiniciou: cabeçalho novo, a janela reconecta na hora
                    try:
                        yield q.get(timeout=2)
                    except queue.Empty:
                        continue
                    except Exception:
                        break
            finally:
                w.hd_unsubscribe(q)

        return Response(gen(), mimetype="video/mp4",
                        headers={"Cache-Control": "no-store", "X-Codec": w.hd_codec or ""})

    @app.get("/api/audio/<cid>")
    @auth
    def live_audio(cid):
        w = mgr.worker(cid)
        if not w:
            abort(404)
        if w.no_audio:
            # alguém quer ouvir: tenta religar o som (se faz mais de 1 min que desligou)
            if time.time() - getattr(w, "no_audio_t", 0) > 60:
                from core import LOG
                w.no_audio = False
                w.rec_restart = True
                LOG.info(f"[{w.cam['nome']}] pediram o som ao vivo; religando")
            abort(503)
        q = w.audio_subscribe()

        def gen():
            try:
                while not w.stop_ev.is_set():
                    try:
                        yield q.get(timeout=5)
                    except Exception:
                        yield b""
            finally:
                w.audio_unsubscribe(q)

        return Response(gen(), mimetype="audio/mpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/foto/<cid>")
    @auth
    def photo(cid):
        w = mgr.worker(cid)
        if not w:
            abort(404)
        after = request.args.get("apos")
        if after is not None:  # modo ao vivo: espera o próximo quadro
            w.wait_frame(int(after))
        q = int(request.args.get("q", 88))
        data = w.jpeg(max(40, min(95, q)))
        if not data:
            abort(404)
        return Response(data, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store", "X-Quadro": str(w.frame_seq)})

    # ------------------------------------------------------------- alertas --
    @app.get("/api/alertas")
    @auth
    def alerts():
        a = request.args
        tipos = [t for t in a.get("tipo", "").split(",") if t] or None
        f = lambda k: float(a[k]) if a.get(k) else None  # noqa: E731
        return jsonify(mgr.list_alerts(a.get("camera") or None, f("desde"), f("ate"), tipos))

    @app.get("/clipe/<path:aid>")
    @auth
    def clip(aid):
        p = mgr.clip_path(aid, ".mp4")
        if not p:
            abort(404)
        dl = request.args.get("baixar")
        return send_file(p, mimetype="video/mp4", conditional=True, as_attachment=bool(dl),
                         download_name=aid.replace("/", "_") + ".mp4")

    @app.get("/miniatura/<path:aid>")
    @auth
    def thumb(aid):
        p = mgr.clip_path(aid, ".jpg")
        if not p:
            abort(404)
        return send_file(p, mimetype="image/jpeg", max_age=86400)

    @app.get("/api/alertas/deteccoes/<path:aid>")
    @auth
    def alert_detections(aid):
        p = mgr.clip_path(aid, ".json")
        if not p:
            return jsonify([])
        try:
            return jsonify(json.loads(p.read_text(encoding="utf-8")).get("deteccoes") or [])
        except Exception:
            return jsonify([])

    @app.delete("/api/alertas")
    @auth
    def delete_alerts():
        ids = (request.get_json(force=True) or {}).get("ids", [])
        n = sum(1 for i in ids if mgr.delete_alert(i))
        return jsonify({"ok": True, "apagados": n})

    @app.post("/api/janela")
    @auth
    def janela():
        """Tela cheia da janela do programa (Esc sai)."""
        acao = (request.get_json(force=True, silent=True) or {}).get("acao")
        if acao == "alternar":
            acao = "normal" if WIN["cheia"] else "cheia"
        w = WIN.get("w")
        if w is not None and acao in ("normal", "cheia") and WIN["cheia"] != (acao == "cheia"):
            try:
                w.toggle_fullscreen()
                WIN["cheia"] = acao == "cheia"
            except Exception:
                pass
        return jsonify({"cheia": WIN["cheia"]})

    @app.get("/api/atualizacao")
    @auth
    def upd_state():
        import updater
        from core import VERSAO
        return jsonify({"versao": VERSAO, "nova": updater.STATE["nova"], "erro": updater.STATE["erro"],
                        "ultima": updater.STATE["ultima"], "auto": cfg.get().get("atualizar_sozinho", True)})

    @app.post("/api/atualizar")
    @auth
    def upd_now():
        import updater
        msg = updater.run_now(cfg, mgr, UPD["restart_args"], force=True)
        return jsonify({"msg": msg})

    @app.post("/api/log")
    @auth
    def ui_log():
        from core import LOG
        msg = str((request.get_json(force=True, silent=True) or {}).get("msg", ""))[:300]
        if msg:
            LOG.info(f"[janela] {msg}")
        return jsonify({"ok": True})

    @app.post("/api/sair")
    @auth
    def quit_app():
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return jsonify({"ok": False})

        def bye():
            time.sleep(0.5)
            from core import shutdown
            shutdown(mgr)

        threading.Thread(target=bye, daemon=True).start()
        return jsonify({"ok": True})

    @app.get("/api/pasta-gravacoes")
    @auth
    def rec_dir_info():
        d = cfg.recordings_dir()
        videos = os.path.join(os.path.expanduser("~"), "Videos", "FTCam")
        livre = shutil.disk_usage(d).free / 1e9
        return jsonify({"atual": str(d), "videos": videos, "livre_gb": round(livre, 1),
                        "pode_escolher": WIN.get("w") is not None and request.remote_addr in ("127.0.0.1", "::1")})

    @app.post("/api/escolher-pasta")
    @auth
    def pick_dir():
        w = WIN.get("w")
        if w is None or request.remote_addr not in ("127.0.0.1", "::1"):
            return jsonify({"pasta": None})
        try:
            import webview
            r = w.create_file_dialog(webview.FOLDER_DIALOG, directory=str(cfg.recordings_dir()))
            return jsonify({"pasta": (r[0] if isinstance(r, (list, tuple)) else r) if r else None})
        except Exception:
            return jsonify({"pasta": None})

    @app.post("/api/abrir-pasta")
    @auth
    def open_folder():
        if os.name == "nt" and request.remote_addr in ("127.0.0.1", "::1"):
            os.startfile(cfg.recordings_dir())  # noqa
            return jsonify({"ok": True})
        return jsonify({"ok": False})

    return app
