"""Motor do FTCam: uma 'CameraWorker' por câmera + gerente com limpeza automática."""
import base64
import json
import os
import queue
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
import cv2  # noqa: E402
import numpy as np  # noqa: E402

import ai  # noqa: E402
import onvif  # noqa: E402

AI_KINDS = ("pessoa", "veículo", "animal")
MIN_CONF = {"pessoa": 0.5, "veículo": 0.5, "animal": 0.62, "pacote": 0.55}
MIN_CONF_LABEL = {"bird": 0.8, "cat": 0.7, "horse": 0.75, "cow": 0.75, "sheep": 0.75}  # pássaro confunde fácil
from core import LOG, NO_WINDOW, find_ffmpeg, imwrite  # noqa: E402


def _cred(v):
    # O RTSP do ffmpeg NÃO decodifica %xx no usuário/senha: "@" e ":" vão crus
    # (ele separa pelo último "@"). Só codifica o que quebraria a URL de vez.
    return quote(v, safe="@:!$&'()*+,;=~-._")


def rtsp_url(cam, stream):
    return f"rtsp://{_cred(cam['usuario'])}:{_cred(cam['senha'])}@{cam['ip']}:{cam['porta_rtsp']}/{stream}"


def open_capture(url):
    params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 6000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 6000]
    try:
        return cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)
    except TypeError:  # OpenCV antigo sem parâmetros
        return cv2.VideoCapture(url, cv2.CAP_FFMPEG)


def grab_test_frame(cam, timeout=12):
    """Tenta pegar 1 quadro do stream leve. Retorna (ok, jpeg_base64|mensagem)."""
    result = {}

    def run():
        cap = open_capture(rtsp_url(cam, cam.get("stream_leve", "stream2")))
        if not cap.isOpened():
            result["r"] = (False, "Não conectou. Confira IP, usuário e senha da 'Conta da câmera'.")
            return
        ok, frame = cap.read()
        cap.release()
        if not ok:
            result["r"] = (False, "Conectou mas não recebeu imagem.")
            return
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        result["r"] = (True, base64.b64encode(buf.tobytes()).decode())

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return result.get("r", (False, "A câmera demorou demais pra responder."))


def safe_unlink(p):
    """Apaga sem travar o programa se o Windows disser que o arquivo está em uso."""
    try:
        p.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _mtime(p):
    """Data do arquivo, sem quebrar se ele sumiu no meio (o Windows apaga/renomeia enquanto a gente olha)."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def kill_orphan_ffmpeg(rec_root):
    """Encerra ffmpeg que ficou rodando de uma execução anterior do FTCam."""
    try:
        import psutil
    except Exception:
        return 0
    root = str(rec_root).lower()
    n = 0
    for pr in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (pr.info["name"] or "").lower()
            cmd = " ".join(pr.info["cmdline"] or []).lower()
            if "ffmpeg" in name and "_buffer" in cmd and root in cmd:
                pr.kill()
                n += 1
        except Exception:
            continue
    if n:
        LOG.info(f"encerrados {n} ffmpeg perdidos de uma execução anterior")
        time.sleep(1)
    return n


CLIP_SEQ = [0]  # muda quando um clipe novo fica pronto (a interface recarrega a lista)


def seg_time(p):
    try:
        return datetime.strptime(p.stem, "%Y%m%d-%H%M%S").timestamp()
    except ValueError:
        return None


def _moof_is_key(moof):
    """O pedaço de vídeo começa com quadro-chave? (lê as flags do 1º quadro no moof)"""
    import struct
    try:
        i = moof.find(b"tfhd")
        dflags = None
        if i > 0:
            fl = int.from_bytes(moof[i + 5:i + 8], "big")
            o = i + 8 + 4  # track_ID
            if fl & 0x1: o += 8
            if fl & 0x2: o += 4
            if fl & 0x8: o += 4
            if fl & 0x10: o += 4
            if fl & 0x20:
                dflags = struct.unpack(">I", moof[o:o + 4])[0]
        j = moof.find(b"trun")
        if j < 0:
            return True
        fl = int.from_bytes(moof[j + 5:j + 8], "big")
        o = j + 8 + 4  # sample_count
        if fl & 0x1: o += 4
        if fl & 0x4:
            sf = struct.unpack(">I", moof[o:o + 4])[0]
        else:
            if fl & 0x100: o += 4
            if fl & 0x200: o += 4
            if fl & 0x400:
                sf = struct.unpack(">I", moof[o:o + 4])[0]
            elif dflags is not None:
                sf = dflags
            else:
                return True
        return not (sf & 0x00010000)  # bit "não é quadro-chave" desligado = é chave
    except Exception:
        return True


class CameraWorker:
    def __init__(self, cam, cfg, on_alert, on_identity=lambda cid, info: None):
        self.on_identity = on_identity
        self.started_at = time.time()
        self.cam = cam
        self.cfg = cfg
        self.on_alert = on_alert
        self.stop_ev = threading.Event()
        root = cfg.recordings_dir() / cam["id"]
        self.clips_dir = root
        self.buf_dir = root / "_buffer"
        self.buf_dir.mkdir(parents=True, exist_ok=True)
        for p in self.buf_dir.glob("*.ts"):
            safe_unlink(p)

        self.proc = None
        self.frame = None
        self.frame_t = 0.0
        self.frame_lock = threading.Lock()
        self.frame_seq = 0
        self._last_emit = 0.0
        self._tokens = 1.0
        self._fps_count = 0
        self._fps_t0 = time.time()
        self.fps_real = 0.0
        self.frame_cv = threading.Condition()
        self._jpeg = (0.0, None, -1)
        self.last_motion = 0.0
        self.last_kind = "movimento"
        self.onvif_active = {}  # tipo -> (ativo, desde)
        self.recording_event = False
        self.auth_fails = 0
        self.rec_restart = False
        self.hd_fails = 0
        self.no_audio = False
        self.hd_init = None
        self.hd_codec = ""
        self.hd_t = time.time()
        self.hd_subs = []
        self.hd_lock = threading.Lock()
        self.no_hwaccel = False
        self.grid_motion_t = 0.0
        self._gm_prev = None
        self._gm_t = 0.0
        self.audio_subs = []
        self.audio_lock = threading.Lock()
        self.audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.audio_sock.bind(("127.0.0.1", 0))
        self.audio_sock.settimeout(1.0)
        self.audio_port = self.audio_sock.getsockname()[1]
        self.candidate_until = 0.0
        self.ev_info = self._new_info()
        self.ai_state = ""
        self.last_objs = []
        self.last_objs_t = 0.0
        self._prev_kinds = set()
        self._face_busy = False
        self.motion_box = ((0, 0, 1, 1), 0.0)
        self._zoom_t = 0.0
        self.last_faces = (0.0, [])
        self.last_dim = (0, 0)
        self.live_restart = False
        self.seen_topics = set()
        self.st = {
            "gravador": "iniciando",
            "ao_vivo": "iniciando",
            "deteccao": "iniciando",
            "erro": "",
        }
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.threads = []

    # ---------------------------------------------------------- ciclo vida --
    def start(self):
        parts = [self._recorder, self._live, self._events, self._ai_loop, self._audio_loop]
        if self.cam["deteccao"] == "tapo":
            parts.append(self._onvif)
        for fn in parts:
            t = threading.Thread(target=self._supervise, args=(fn,), daemon=True)
            t.start()
            self.threads.append(t)
        if self.cam["deteccao"] != "tapo":
            self.st["deteccao"] = "ftcam"
        LOG.info(f"[{self.cam['nome']}] iniciada")

    def _supervise(self, fn):
        """Mantém cada parte rodando PRA SEMPRE: se parar ou der erro, reinicia em 3 s."""
        name = fn.__name__.strip("_")
        restarts = 0
        while not self.stop_ev.is_set():
            try:
                fn()
                if self.stop_ev.is_set():
                    break
                why = "parou"
            except Exception as e:
                why = f"erro: {e}"
                if name == "recorder":
                    self._kill_proc()  # não deixa ffmpeg órfão segurando a câmera
                if restarts < 3 or restarts % 50 == 0:
                    LOG.exception(f"[{self.cam['nome']}] {name}: {why}")
            restarts += 1
            if restarts <= 3 or restarts % 50 == 0:
                LOG.info(f"[{self.cam['nome']}] {name} {why}; reiniciando (x{restarts})")
            self.stop_ev.wait(3)

    def stop(self):
        self.stop_ev.set()
        self._kill_proc()

    # --------------------------------------------------------------- status --
    def status(self):
        s = dict(self.st)
        s.update({
            "id": self.cam["id"],
            "nome": self.cam["nome"],
            "ip": self.cam["ip"],
            "modo": self.cam["deteccao"],
            "qualidade": "alta" if self.cam.get("ao_vivo_alta") else "leve",
            "hd_codec": self.hd_codec if self.cam.get("ao_vivo_alta") and time.time() - self.hd_t < 10 else "",
            "fps": self.fps_real if time.time() - self.frame_t < 5 else 0,
            "som": not self.no_audio,
            "fps_limite": int(self.cam.get("fps_ao_vivo") or 0),
            "resolucao": self._res(),
            "gravando_alerta": self.recording_event and self.st["gravador"] == "ok" and not self.auth_fails,
            "ultimo_alerta": self.last_motion or None,
            # online = recebeu imagem nos últimos 15 s (não pisca em reconexões rápidas)
            "online": time.time() - self.frame_t < 15,
            "ia": bool(self.cam.get("ia_ativa", True) and ai.BRAIN.available()),
            "verificando": time.time() < self.candidate_until,
            "objetos_agora": [{"nome": o["nome"], "tipo": o["tipo"], "conf": round(o["conf"], 2), "box": o["box"]}
                              for o in self.last_objs] if time.time() - self.last_objs_t < 3 else [],
            "ia_ao_vivo": bool(self.cam.get("ia_ao_vivo")),
            "disparar_com": self.cam.get("disparar_com") or [],
        })
        return s

    def _res(self):
        f = self.frame
        return f"{f.shape[1]}×{f.shape[0]}" if f is not None else ""

    def _audio_loop(self):
        """Recebe o MP3 do ffmpeg e repassa pra quem estiver ouvindo no navegador."""
        while not self.stop_ev.is_set():
            try:
                data = self.audio_sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            with self.audio_lock:
                for q in list(self.audio_subs):
                    try:
                        q.put_nowait(data)
                    except queue.Full:  # ouvinte travado: descarta o atraso
                        try:
                            while True:
                                q.get_nowait()
                        except queue.Empty:
                            pass
        try:
            self.audio_sock.close()
        except OSError:
            pass

    def audio_subscribe(self):
        q = queue.Queue(maxsize=200)
        with self.audio_lock:
            self.audio_subs.append(q)
        return q

    def audio_unsubscribe(self, q):
        with self.audio_lock:
            if q in self.audio_subs:
                self.audio_subs.remove(q)

    def input_args(self):
        return ["-rtsp_transport", "tcp", "-i", rtsp_url(self.cam, self.cam["stream_gravacao"])]

    @staticmethod
    def _drain_err(proc, lines):
        try:
            for raw in iter(proc.stderr.readline, b""):
                lines.append(raw.decode("utf-8", "ignore").rstrip())
        except Exception:
            pass

    def _kill_proc(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.stdin.write(b"q")
                p.stdin.flush()
                p.wait(timeout=3)
            except Exception:
                p.kill()

    def _save_leve(self):
        """Grava 'leve' na configuração, senão ela puxa de volta pro alto e fica trocando sem parar."""
        try:
            c = next((c for c in self.cfg.get()["cameras"] if c["id"] == self.cam["id"]), None)
            if c and c.get("ao_vivo_alta"):
                c = dict(c, ao_vivo_alta=False)
                self.cfg.upsert_camera(c)
        except Exception:
            pass

    def set_live_quality(self, alta):
        """Leve = stream2 próprio. Alta = o gravador também gera o ao vivo (reinicia o gravador ~2 s)."""
        alta = bool(alta)
        if alta == bool(self.cam.get("ao_vivo_alta")):
            return
        self.cam["ao_vivo_alta"] = alta
        self.rec_restart = True
        self.hd_fails = 0

    def _pipe_mp4(self, proc):
        """Lê o MP4 fragmentado do ffmpeg, guarda o cabeçalho e repassa cada pedaço (GOP) pra janela."""
        import struct
        self.hd_init, self.hd_codec, self.hd_t = None, "", time.time()
        self.hd_gop = []
        head = b""
        t0 = time.time()
        got = False
        f = proc.stdout
        try:
            while proc.poll() is None and not self.stop_ev.is_set():
                h = f.read(8)
                if len(h) < 8:
                    break
                size, typ = struct.unpack(">I4s", h)
                hdr = h
                if size == 1:
                    ext = f.read(8)
                    size = struct.unpack(">Q", ext)[0]
                    hdr += ext
                body = f.read(size - len(hdr)) if size > len(hdr) else b""
                box = hdr + body
                if typ in (b"ftyp",):
                    head = box
                elif typ == b"moov":
                    self.hd_init = head + box
                    i = box.find(b"avcC")
                    if i > 0:  # codec pro navegador, ex.: avc1.640028
                        self.hd_codec = "avc1.%02x%02x%02x" % (box[i + 5], box[i + 6], box[i + 7])
                    else:
                        self.hd_codec = "hvc1" if b"hvcC" in box else ""
                elif typ == b"moof":
                    self.hd_pending = box
                elif typ == b"mdat" and getattr(self, "hd_pending", None):
                    key = _moof_is_key(self.hd_pending)
                    frag = self.hd_pending + box
                    self.hd_pending = None
                    self.hd_t = time.time()
                    got = True
                    with self.hd_lock:
                        # guarda desde o último quadro-chave: quem abrir agora já começa com imagem
                        if key:
                            self.hd_gop = [frag]
                        elif self.hd_gop and len(self.hd_gop) < 200:
                            self.hd_gop.append(frag)
                        for q in list(self.hd_subs):
                            try:
                                q.put_nowait(frag)
                            except queue.Full:
                                pass  # espectador travado: perde esse pedaço
        except Exception:
            pass
        if got:
            self.hd_fails = 0
        elif self.cam.get("ao_vivo_alta") and time.time() - t0 < 30 and not self.stop_ev.is_set():
            self.hd_fails = getattr(self, "hd_fails", 0) + 1
            if self.hd_fails in (2, 10) or self.hd_fails % 50 == 0:  # mantém o alto e continua tentando
                LOG.info(f"[{self.cam['nome']}] vídeo em alta não chegou do ffmpeg (x{self.hd_fails}); tentando de novo")

    def hd_subscribe(self):
        q = queue.Queue(maxsize=400)
        with self.hd_lock:
            for frag in getattr(self, "hd_gop", []):
                q.put_nowait(frag)
            self.hd_subs.append(q)
        return q

    def hd_unsubscribe(self, q):
        with self.hd_lock:
            if q in self.hd_subs:
                self.hd_subs.remove(q)

    def _pipe_frames(self, proc):
        """Lê os JPEGs que o ffmpeg do gravador solta (modo Alta) e usa como imagem ao vivo."""
        buf = b""
        got = False
        last_dec = 0.0
        t0 = time.time()
        try:
            while proc.poll() is None and not self.stop_ev.is_set():
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    a = buf.find(b"\xff\xd8")
                    b = buf.find(b"\xff\xd9", a + 2)
                    if a < 0 or b < 0:
                        if len(buf) > 8_000_000:
                            buf = b""
                        break
                    jpg = buf[a:b + 2]
                    buf = buf[b + 2:]
                    now = time.time()
                    # a tela usa o JPEG pronto; só descompacta ~4 por segundo pra IA e pra grade
                    if now - last_dec >= 0.25:
                        frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_REDUCED_COLOR_2
                                             if not (self.recording_event or self.candidate_until > now
                                                     or self.cam.get("ia_ao_vivo")) else cv2.IMREAD_COLOR)
                        if frame is not None:
                            last_dec = now
                            with self.frame_lock:
                                self.frame, self.frame_t = frame, now
                            self._grid_motion(frame, now)
                    else:
                        self.frame_t = now  # imagem chegando: mantém "online"
                    before = self.frame_seq
                    self._new_frame()
                    if self.frame_seq != before:
                        self._jpeg = (now, jpg, self.frame_seq)
                    got = True
                    self.st["ao_vivo"] = "ok"
        except Exception:
            pass
        if not got and self.cam.get("ao_vivo_alta") and time.time() - t0 < 30 and not self.stop_ev.is_set():
            self.hd_fails = getattr(self, "hd_fails", 0) + 1
            if self.hd_fails in (2, 10):
                LOG.info(f"[{self.cam['nome']}] vídeo em alta não chegou; tentando de novo")

    def _new_frame(self):
        """Avisa a tela que tem quadro novo, respeitando o limite de fps escolhido."""
        now = time.time()
        lim = int(self.cam.get("fps_ao_vivo") or 0)
        if lim:  # balde de fichas: mantém a média no limite mesmo com quadros chegando em rajada
            self._tokens = min(max(1.0, lim * 0.5), self._tokens + (now - self._last_emit) * lim)
            self._last_emit = now
            if self._tokens < 1:
                return
            self._tokens -= 1
        else:
            self._last_emit = now
        self._fps_count += 1
        if now - self._fps_t0 >= 2:
            self.fps_real = round(self._fps_count / (now - self._fps_t0), 1)
            self._fps_count, self._fps_t0 = 0, now
        with self.frame_cv:
            self.frame_seq += 1
            self.frame_cv.notify_all()

    def wait_frame(self, after, timeout=1.5):
        """Espera um quadro mais novo que 'after' (a tela recebe cada quadro assim que chega)."""
        with self.frame_cv:
            self.frame_cv.wait_for(lambda: self.frame_seq > after or self.stop_ev.is_set(), timeout)
        return self.frame_seq

    def jpeg(self, quality=75):
        """JPEG do quadro atual; codifica uma vez por quadro (vários espectadores reaproveitam)."""
        t, data, seq = self._jpeg
        if data and seq == self.frame_seq:
            return data
        with self.frame_lock:
            f = None if self.frame is None else self.frame
            seq = self.frame_seq
            if f is None or time.time() - self.frame_t > 15:
                return None
            ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return None
        data = buf.tobytes()
        self._jpeg = (time.time(), data, seq)
        return data

    def wants(self, kind):
        allowed = self.cam.get("disparar_com") or []
        return kind == "manual" or not allowed or kind in allowed

    def needs_ai(self):
        """Só grava com confirmação da IA quando o usuário escolheu tipos (pessoa, carro, animal)
        e não marcou 'qualquer movimento'."""
        allowed = self.cam.get("disparar_com") or []
        return (self.cam.get("ia_ativa", True) and ai.BRAIN.available()
                and any(k in AI_KINDS for k in allowed) and "movimento" not in allowed)

    def trigger(self, kind="movimento"):
        """Algo aconteceu (movimento da Tapo, do FTCam, evento inteligente ou botão manual)."""
        now = time.time()
        if kind == "manual":
            return self._fire("manual")
        if kind in AI_KINDS:  # a própria câmera já disse que é pessoa/carro/animal
            if self.wants(kind):
                self._fire(kind)
            return
        # movimento genérico
        if self.recording_event:
            if self.wants("movimento") or not self.needs_ai():
                self.last_motion = now  # mantém o clipe atual vivo
            return
        if not self._moved_in_grid():
            # movimento só fora dos quadrados azuis (árvore, rua...): ignora, nem liga a IA
            if now - getattr(self, "_ign_log", 0) > 60:
                self._ign_log = now
                LOG.info(f"[{self.cam['nome']}] movimento ignorado: fora da grade de detecção")
            return
        if self.needs_ai():
            if now >= self.candidate_until:
                self.ev_info = self._new_info()
                if now - getattr(self, "_conf_log", 0) > 60:
                    self._conf_log = now
                    LOG.info(f"[{self.cam['nome']}] IA conferindo se é {', '.join(self.cam.get('disparar_com') or [])}...")
            self.candidate_until = now + 12  # a IA tem 12 s pra confirmar
            return
        if self.wants("movimento"):
            self._fire("movimento")

    def _fire(self, kind):
        self.candidate_until = 0.0
        self.last_motion = time.time()
        if kind != "movimento" or not self.recording_event:
            self.last_kind = kind

    @staticmethod
    def _new_info():
        return {"objetos": {}, "rostos": set(), "pets": set(), "best": None, "best_score": 0.0, "faces_emb": [], "trilha": []}

    # ------------------------------------------------------------- gravador --
    def _recorder(self):
        ff = find_ffmpeg()
        while not ff and not self.stop_ev.is_set():  # espera o ffmpeg aparecer (sem desistir)
            self.st["gravador"] = "erro"
            self.st["erro"] = "ffmpeg não encontrado (coloque o ffmpeg.exe na pasta do FTCam)"
            self.stop_ev.wait(15)
            ff = find_ffmpeg()
        while not self.stop_ev.is_set():
            if self.no_audio and time.time() - getattr(self, "no_audio_t", 0) > 600:
                self.no_audio = False  # 10 min depois tenta o som de novo (pode ter sido falha passageira)
                LOG.info(f"[{self.cam['nome']}] tentando ligar o som ao vivo de novo")
            seg = str(self.cfg.get()["tamanho_segmento_segundos"])
            alta = bool(self.cam.get("ao_vivo_alta"))
            cmd = [ff, "-hide_banner", "-loglevel", "error", *self.input_args(),
                   "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k",
                   "-f", "segment", "-segment_time", seg, "-segment_format", "mpegts",
                   "-reset_timestamps", "1", "-strftime", "1",
                   str(self.buf_dir / "%Y%m%d-%H%M%S.ts")]
            if not self.no_audio:
                # som ao vivo: sai da mesma conexão do gravador (a câmera recusa conexões extras)
                cmd += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", "22050", "-c:a", "libmp3lame", "-b:a", "64k",
                        "-f", "mp3", f"udp://127.0.0.1:{self.audio_port}?pkt_size=1024"]
            if alta:
                # a câmera só libera UMA conexão no stream de alta: o mesmo ffmpeg manda o vídeo
                # ORIGINAL (sem decodificar nem recompactar) pra janela, que decodifica na placa de vídeo
                # pedaços de 0,3 s (não espera o próximo quadro-chave): imagem contínua, sem "1 fps"
                cmd += ["-map", "0:v:0", "-an", "-c:v", "copy", "-f", "mp4", "-frag_duration", "300000",
                        "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-flush_packets", "1", "pipe:1"]
            self.rec_restart = False
            try:
                self.proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE if alta else subprocess.DEVNULL,
                    stderr=subprocess.PIPE, creationflags=NO_WINDOW,
                )
                # lê o stderr o tempo todo: se ninguém lê, o cano enche (no Windows são só 4 KB),
                # o ffmpeg TRAVA esperando e a imagem/gravação para do nada
                self._errlines = deque(maxlen=60)
                threading.Thread(target=self._drain_err, args=(self.proc, self._errlines), daemon=True).start()
                if alta:
                    threading.Thread(target=self._pipe_mp4, args=(self.proc,), daemon=True).start()
            except OSError as e:
                self.st["gravador"] = "erro"
                self.st["erro"] = f"ffmpeg não abriu: {e}"
                self.stop_ev.wait(10)
                continue
            self.st["gravador"] = "ok"
            started = time.time()
            last_check = 0.0
            stuck = None
            while not self.stop_ev.is_set() and self.proc.poll() is None:
                if self.rec_restart:
                    self._kill_proc()
                    break
                now = time.time()
                # vigia: se a imagem ou a gravação travar, derruba e reconecta sozinho
                if now - started > 8 and now - last_check > 1:
                    last_check = now
                    # o 1º pedaço HD pode demorar (espera 2 quadros-chave); depois disso, 8 s sem nada = travou
                    if alta and now - self.hd_t > 8 and now - started > 15:
                        stuck = "imagem parou"
                    else:
                        segs = list(self.buf_dir.glob("*.ts"))
                        newest = max((_mtime(p) for p in segs), default=started)
                        if now - newest > 25:
                            stuck = "gravação parou"
                    if stuck:
                        LOG.info(f"[{self.cam['nome']}] {stuck}; reconectando sozinho")
                        self._kill_proc()
                        break
                time.sleep(0.5)
            if time.time() - started > 60:
                self.auth_fails = 0
                self.st["_hd_cedo"] = 0
            if self.stop_ev.is_set():
                break
            if self.rec_restart or (self.proc.returncode is not None and self.rec_restart is None):
                continue
            time.sleep(0.3)  # deixa o leitor pegar as últimas linhas
            err = [l for l in list(self._errlines) if l.strip()]
            self.st["gravador"] = "reconectando"
            if alta and not self.no_hwaccel and any(k in l.lower() for l in err for k in ("hwaccel", "dxva", "d3d11", "qsv", "cuda", "hardware")):
                self.no_hwaccel = True
                LOG.info(f"[{self.cam['nome']}] decodificação pela placa falhou; usando o processador")
                continue
            # 406 = a câmera diz que o vídeo principal já está em uso (sobrou um ffmpeg nosso preso nele)
            if any("406" in l for l in err):
                self.st["_406"] = self.st.get("_406", 0) + 1
                if self.st["_406"] >= 2:
                    n = kill_orphan_ffmpeg(self.buf_dir)
                    LOG.info(f"[{self.cam['nome']}] câmera ocupada (406): fechei {n} ffmpeg perdido(s) que seguravam ela")
                    self.st["_406"] = 0
                self.stop_ev.wait(3)
                continue
            self.st["_406"] = 0
            # só erro de verdade do som (aviso de mp3 no meio do log não conta)
            audio_err = stuck is None and any(k in l.lower() for l in err for k in (
                "matches no streams", "does not contain any stream", "unknown encoder", "exactly one mp3",
                "error opening output file udp", "encoder for output stream"))
            early = time.time() - started < 25
            if stuck == "imagem parou" and early and not self.no_audio:
                self.st["_hd_cedo"] = self.st.get("_hd_cedo", 0) + 1
            if not self.no_audio and (audio_err or self.st.get("_hd_cedo", 0) >= 3
                                      or (stuck is None and time.time() - started < 10 and self.st.get("_falhas_cedo", 0) >= 2)):
                self.no_audio = True
                self.no_audio_t = time.time()
                LOG.info(f"[{self.cam['nome']}] som ao vivo desligado (erro: {' | '.join(l[:160] for l in err[-4:]) if err else 'sem detalhe'}); "
                         "gravação continua normal")
                continue
            self.st["_falhas_cedo"] = self.st.get("_falhas_cedo", 0) + 1 if early else 0
            if err:
                msg = err[-1]
                low = msg.lower()
                if "401" in low or "unauthorized" in low:
                    self.auth_fails += 1
                    msg = "Câmera recusou usuário/senha do vídeo (RTSP)"
                elif "refused" in low or "timed out" in low or "no route" in low:
                    msg = "A câmera não respondeu nesse IP"
                self.st["erro"] = msg[:200]
                LOG.info(f"[{self.cam['nome']}] gravador: {err[-1][:200]}")
            # login recusado: espera cada vez mais pra não fazer a câmera bloquear o acesso
            wait = 5 if not self.auth_fails else min(600, 30 * 2 ** (self.auth_fails - 1))
            if self.auth_fails:
                self.st["erro"] += f" — nova tentativa em {wait // 60} min" if wait >= 60 else f" — nova tentativa em {wait}s"
            self.stop_ev.wait(wait)

    # ------------------------------------------------- ao vivo + movimento --
    def _live(self):
        prev, hits, last_an = None, 0, 0.0
        live_fails = 0
        while not self.stop_ev.is_set():
            if self.auth_fails:  # vídeo bloqueado: não insiste, deixa o gravador testar
                self.st["ao_vivo"] = "offline"
                self.stop_ev.wait(5)
                continue
            stream = self.cam["stream_leve"]  # mesmo no Alta: IA, grade e fotos usam o leve (barato)
            self.live_restart = False
            cap = open_capture(rtsp_url(self.cam, stream))
            if not cap.isOpened():
                self.st["ao_vivo"] = "offline"
                live_fails += 1
                if live_fails <= 3 or live_fails % 10 == 0:
                    LOG.info(f"[{self.cam['nome']}] ao vivo: não abriu {stream} (tentativa {live_fails})")
                self.stop_ev.wait(min(30, 5 * 2 ** min(live_fails - 1, 3)))
                continue
            live_fails = 0
            LOG.info(f"[{self.cam['nome']}] ao vivo conectado ({stream})")
            self.st["ao_vivo"] = "ok"
            prev, hits = None, 0
            while not self.stop_ev.is_set() and not self.live_restart:
                ok, frame = cap.read()
                if not ok:
                    break
                now = time.time()
                with self.frame_lock:
                    self.frame, self.frame_t = frame, now
                self._new_frame()
                self._grid_motion(frame, now)

                if not self._use_own_detection() or now - last_an < 0.25:
                    continue
                last_an = now
                sens = self.cam["sensibilidade"]
                limiar = 38 - sens * 0.3              # 50% => diferença 23 (antes 30)
                area = max(0.05, 1.2 - sens * 0.012)  # 50% => 0,6% da imagem (antes 1,6%)
                h, w = frame.shape[:2]
                small = cv2.resize(frame, (320, max(1, int(320 * h / w))))
                gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (21, 21), 0)
                if prev is None or prev.shape != gray.shape:
                    prev = gray
                    continue
                diff = cv2.absdiff(prev, gray)
                prev = gray
                _, th = cv2.threshold(diff, limiar, 255, cv2.THRESH_BINARY)
                th = cv2.dilate(th, None, iterations=2)
                mask = ai.grid_mask(self.cam.get("grade"), th.shape[1], th.shape[0])
                if mask is not None:
                    th = cv2.bitwise_and(th, mask)
                    pct = cv2.countNonZero(th) * 100.0 / max(1, cv2.countNonZero(mask))
                else:
                    pct = cv2.countNonZero(th) * 100.0 / th.size
                # mudança gigante de uma vez = virada do modo noturno, ignora
                if pct > 70:
                    hits = 0
                    continue
                if pct >= area:  # onde mexeu (pra IA dar zoom ali se a pessoa estiver pequena/longe)
                    x, y, bw, bh = cv2.boundingRect(th)
                    H, W = th.shape[:2]
                    self.motion_box = ((x / W, y / H, (x + bw) / W, (y + bh) / H), now)
                hits = hits + 1 if pct >= area else 0
                if hits >= 2:
                    if now - getattr(self, "_mov_log", 0) > 30:
                        self._mov_log = now
                        LOG.info(f"[{self.cam['nome']}] movimento detectado ({pct:.1f}% da imagem)")
                    self.trigger("movimento")
            cap.release()
            if self.live_restart:
                continue  # troca de qualidade: reconecta na hora
            if not self.stop_ev.is_set():
                self.live_drops = getattr(self, "live_drops", 0) + 1
                if self.live_drops <= 5 or self.live_drops % 20 == 0:
                    LOG.info(f"[{self.cam['nome']}] imagem ao vivo caiu, reconectando (x{self.live_drops})")
                self.st["ao_vivo"] = "offline"
                self.stop_ev.wait(3)

    def _grid_motion(self, frame, now):
        """Marca quando houve movimento DENTRO dos quadrados azuis (custo quase zero: imagem 160x90)."""
        if now - self._gm_t < 0.25:
            return
        self._gm_t = now
        small = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(frame, (160, 90)), cv2.COLOR_BGR2GRAY), (5, 5), 0)
        prev, self._gm_prev = self._gm_prev, small
        if prev is None:
            return
        diff = cv2.absdiff(prev, small)
        _, th = cv2.threshold(diff, 10, 255, cv2.THRESH_BINARY)
        mask = ai.grid_mask(self.cam.get("grade"), 160, 90)
        if mask is not None:
            mask = cv2.dilate(mask, None, iterations=2)  # folga na borda da grade
            th = cv2.bitwise_and(th, mask)
        if cv2.countNonZero(th) >= 6:  # ~6 pontinhos em 160x90 já é uma pessoa longe
            self.grid_motion_t = now

    def _moved_in_grid(self):
        """Teve movimento na área marcada nos últimos 5 s? (sem grade = imagem toda)"""
        g = self.cam.get("grade")
        if not g or all(g):
            return True
        if time.time() - self.grid_motion_t < 5:
            return True
        # sem imagem leve (ex.: reconectando) não dá pra conferir: deixa passar
        return time.time() - self.frame_t > 3

    def _use_own_detection(self):
        return self.cam["deteccao"] == "ftcam" or self.st["deteccao"] == "fallback"

    # ------------------------------------------------- detecção da Tapo --
    def _onvif(self):
        cam = self.cam
        fails = 0
        while not self.stop_ev.is_set():
            client = onvif.OnvifClient(cam["ip"], cam["porta_onvif"], cam["usuario"], cam["senha"])
            sub = onvif.EventSubscription(client)
            try:
                sub.subscribe()
                if not cam.get("serial"):
                    try:
                        info = client.device_info()
                        if info.get("serial"):
                            self.on_identity(cam["id"], info)
                    except Exception:
                        pass
                self.st["deteccao"] = "tapo"
                self.st["erro"] = ""
                fails = 0
                LOG.info(f"[{cam['nome']}] recebendo eventos da câmera (ONVIF)")
                while not self.stop_ev.is_set():
                    if sub.expired():
                        sub.subscribe()
                    for topic, items in sub.pull(5):
                        sig = topic + "|" + ",".join(sorted(items))
                        if sig not in self.seen_topics:  # registra o que essa câmera manda
                            self.seen_topics.add(sig)
                            LOG.info(f"[{cam['nome']}] evento da câmera: {topic} {items}")
                        for kind, active in onvif.classify_all(topic, items):
                            self.onvif_active[kind] = (active, time.time())
                            if active:
                                self.trigger(kind)
            except onvif.OnvifError as e:
                fails += 1
                self.st["deteccao"] = "fallback"
                self.st["erro"] = f"Detecção da Tapo indisponível ({e}); usando a do FTCam"
                if fails == 1:
                    LOG.info(f"[{cam['nome']}] ONVIF: {e}")
                self.stop_ev.wait(min(60, 10 * fails))
            except Exception as e:  # XML inesperado etc.
                fails += 1
                self.st["deteccao"] = "fallback"
                self.st["erro"] = f"Erro nos eventos da Tapo: {e}"
                self.stop_ev.wait(min(60, 10 * fails))

    # ------------------------------------------------------ eventos/clipes --
    def _events(self):
        event = None
        last_end = 0.0
        while not self.stop_ev.is_set():
            c = self.cfg.get()
            pre, post = c["segundos_antes"], c["segundos_depois"]
            mn, mx = c["clipe_minimo_segundos"], c["clipe_maximo_segundos"]
            seg = c["tamanho_segmento_segundos"]
            now = time.time()

            # enquanto a Tapo disser que ainda há movimento, mantém vivo
            for kind, (active, since) in list(self.onvif_active.items()):
                if not active or now - since > 300:
                    continue
                if event is not None and (self.wants(kind) or not self.needs_ai()):
                    self.last_motion = now
                elif event is None and 0 < self.candidate_until and kind == "movimento":
                    self.candidate_until = max(self.candidate_until, now + 3)

            lm = self.last_motion
            if event is None:
                if now - lm < 1.5 and now - last_end > c["pausa_entre_alertas_segundos"]:
                    when = datetime.fromtimestamp(lm)
                    out = self.clips_dir / when.strftime("%Y-%m-%d") / (when.strftime("%H-%M-%S") + ".mp4")
                    out.parent.mkdir(parents=True, exist_ok=True)
                    self._snapshot(out.with_suffix(".jpg"))
                    event = {"id": lm, "start": lm, "last": lm, "out": out, "tipo": self.last_kind,
                             "info": self.ev_info}
                    with self.pending_lock:
                        self.pending[lm] = lm - pre
                    self.recording_event = True
                    LOG.info(f"[{self.cam['nome']}] alerta: {self.last_kind}")
                    self.on_alert(self, event["tipo"], when, out.with_suffix(".jpg"))
            else:
                event["last"] = max(event["last"], lm)
                if self.last_kind != "movimento":
                    event["tipo"] = self.last_kind
                start = event["start"] - pre
                end = min(max(event["last"] + post, start + mn), start + mx)
                if now >= end:
                    threading.Thread(target=self._finalize, args=(dict(event), start, end), daemon=True).start()
                    self.ev_info = self._new_info()
                    last_end = now
                    event = None
                    self.recording_event = False
                    self.last_kind = "movimento"

            with self.pending_lock:
                oldest = min(self.pending.values(), default=now)
            keep_from = min(now - (pre + 3 * seg), oldest - 2 * seg)
            segs = sorted(self.buf_dir.glob("*.ts"))
            for p in segs[:-1]:
                t = seg_time(p)
                if t is not None and t < keep_from:
                    safe_unlink(p)
            self.stop_ev.wait(0.5)

    # ------------------------------------------------------------------ IA --
    def _ai_loop(self):
        while not self.stop_ev.is_set():
            try:
                self._ai_loop_inner()
            except Exception as e:  # a IA nunca pode derrubar a câmera
                LOG.exception(f"[{self.cam['nome']}] erro na IA: {e}")
                self.stop_ev.wait(5)

    def _ai_loop_inner(self):
        brain = ai.BRAIN
        last_hires = 0.0
        while not self.stop_ev.is_set():
            now = time.time()
            verifying = now < self.candidate_until
            if verifying is False and self.candidate_until and not self.recording_event:
                self.candidate_until = 0.0
                self.ai_state = "descartado"
                vistos = ", ".join(sorted({o["nome"] for o in self.last_objs})) or "nada"
                LOG.info(f"[{self.cam['nome']}] IA não confirmou o alerta (viu: {vistos})")
            live_ai = bool(self.cam.get("ia_ao_vivo"))
            collecting = verifying or self.recording_event
            active = collecting or live_ai
            if not (active and self.cam.get("ia_ativa", True) and brain.available()):
                self.stop_ev.wait(0.3)
                continue
            with self.frame_lock:
                frame = None if self.frame is None else self.frame.copy()
                frame_time = self.frame_t  # hora em que ESSA imagem chegou (não a hora que a IA terminou)
            if frame is None:
                self.stop_ev.wait(0.5)
                continue
            if frame.shape[1] > 1280:
                frame = cv2.resize(frame, (1280, int(frame.shape[0] * 1280 / frame.shape[1])))
            h, w = frame.shape[:2]
            t0 = time.time()
            try:
                objs = brain.detect_objects(frame, conf=0.4)
            except Exception as e:
                LOG.info(f"[{self.cam['nome']}] IA falhou: {e}")
                self.stop_ev.wait(5)
                continue
            # vigia da placa de vídeo: se o OpenCL travar, ele devolve ZERO de tudo sem dar erro.
            # numa cena com movimento quase sempre há algum objeto (carro, pessoa, planta...).
            # muitas análises seguidas vendo NADA de nada = placa travou -> recarrega no processador.
            if collecting and getattr(brain, "last_raw", 1) == 0:
                self._ai_zero = getattr(self, "_ai_zero", 0) + 1
                if self._ai_zero >= 25 and "processador" not in (brain.device or ""):
                    brain.force_cpu()
                    self._ai_zero = 0
            elif getattr(brain, "last_raw", 1) != 0:
                self._ai_zero = 0
            grade = self.cam.get("grade")
            objs = [o for o in objs if o["conf"] >= MIN_CONF_LABEL.get(o["label"], MIN_CONF.get(o["tipo"], 0.5))
                    and ai.in_grid(o["box"], grade, w, h)]
            # nada achado na imagem inteira mas teve movimento: dá zoom na área que mexeu, na imagem HD
            # (pessoa no canto/longe fica pequena demais na imagem leve)
            if (verifying and not any(self.wants(o["tipo"]) for o in objs)
                    and now - self._zoom_t > 1.2 and now - self.motion_box[1] < 4):
                self._zoom_t = now
                try:
                    extra = self._zoom_detect(brain, frame, w, h)
                except Exception as e:
                    extra = []
                    LOG.info(f"[{self.cam['nome']}] zoom da IA falhou: {e}")
                objs += [o for o in extra
                         if o["conf"] >= MIN_CONF_LABEL.get(o["label"], MIN_CONF.get(o["tipo"], 0.5))
                         and ai.in_grid(o["box"], grade, w, h)]
            for o in objs:  # pet cadastrado? troca "cachorro" pelo nome dele
                if o["tipo"] == "animal" and brain.pets:
                    x1, y1, x2, y2 = o["box"]
                    pet = brain.match_pet(frame[y1:y2, x1:x2], o["label"])
                    if pet:
                        o["nome"], o["pet"] = pet, pet
            self.last_objs = objs
            self.last_objs_t = time.time()
            self.last_dim = (w, h)
            if not collecting:  # só mostrando ao vivo: não guarda nada
                self.stop_ev.wait(max(0.2, 0.8 - (time.time() - t0)))
                continue
            info = self.ev_info
            for o in objs:
                if o.get("pet"):
                    info["pets"].add(o["pet"])
            for label in {o["label"] for o in objs}:
                n = sum(1 for o in objs if o["label"] == label)
                info["objetos"][label] = max(info["objetos"].get(label, 0), n)

            # rostos rodam em paralelo (são lentos): os quadrados das pessoas continuam atualizando
            people = [o for o in objs if o["tipo"] == "pessoa"]
            if (people and self.cam.get("reconhecer_rostos", True) and not self._face_busy
                    and now - last_hires > 1.2):
                last_hires = now
                self._face_busy = True
                threading.Thread(target=self._face_job, args=(people, frame, info), daemon=True).start()
            ft, fl = self.last_faces
            faces = fl if time.time() - ft < 2.5 else []

            if len(info["trilha"]) < 400:  # pra desenhar os quadrados no vídeo do alerta
                info["trilha"].append({
                    "t": frame_time, "dim": [w, h],
                    "o": [{"n": o["nome"], "k": o["tipo"], "c": round(o["conf"], 2), "b": list(o["box"])} for o in objs],
                    "f": [{"n": f["nome"] or "desconhecido", "b": list(f["box"])} for f in faces],
                })
            wanted = [o for o in objs if self.wants(o["tipo"]) or not (self.cam.get("disparar_com") or [])]
            score = sum(o["conf"] for o in (wanted or objs)) + 0.5 * len(faces)
            if objs and score > info["best_score"]:
                info["best_score"] = score
                info["best"] = ai.annotate(frame, objs, faces)

            kinds_now = {o["tipo"] for o in wanted}
            confirmed = [o for o in wanted if o["tipo"] in self._prev_kinds or o["conf"] >= 0.8]
            self._prev_kinds = kinds_now
            if verifying and confirmed:
                best = max(confirmed, key=lambda o: o["conf"])
                self.ai_state = f"confirmado: {best['nome']}"
                LOG.info(f"[{self.cam['nome']}] IA confirmou {best['nome']} ({int(best['conf'] * 100)}%)")
                self._fire(best["tipo"])
            elif self.recording_event and wanted:
                self.last_motion = time.time()  # objeto ainda na cena: estende o clipe
            took = time.time() - t0
            self.stop_ev.wait(max(0.1, 0.35 - took))  # o mais rápido que o processador aguentar

    def _zoom_detect(self, brain, frame, w, h):
        """Roda a IA só no pedaço onde teve movimento, recortado da imagem HD e ampliado."""
        (x1, y1, x2, y2), _ = self.motion_box
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        side = max(x2 - x1, (y2 - y1) * h / w, 0.22) * 1.5   # quadrado em volta, no mínimo 1/3 da largura
        bw, bh = min(1, side), min(1, side * w / h)
        nx1, ny1 = min(max(0, cx - bw / 2), 1 - bw), min(max(0, cy - bh / 2), 1 - bh)
        if bw > 0.9:
            return []
        src = self._hires_frame()
        if src is None:
            src = frame
        H, W = src.shape[:2]
        X1, Y1, X2, Y2 = int(nx1 * W), int(ny1 * H), int((nx1 + bw) * W), int((ny1 + bh) * H)
        crop = src[Y1:Y2, X1:X2]
        if crop.size == 0:
            return []
        out = []
        for o in brain.detect_objects(crop, conf=0.35):
            a1, b1, a2, b2 = o["box"]
            o = dict(o, zoom=True)
            o["box"] = (int((X1 + a1) * w / W), int((Y1 + b1) * h / H), int((X1 + a2) * w / W), int((Y1 + b2) * h / H))
            out.append(o)
        if out:
            LOG.info(f"[{self.cam['nome']}] zoom da IA achou: {', '.join(sorted({o['nome'] for o in out}))}")
        return out

    def _face_job(self, people, frame, info):
        """Procura rosto só em volta de cada pessoa, na imagem HD (rosto longe fica grande o bastante)."""
        try:
            brain = ai.BRAIN
            h, w = frame.shape[:2]
            src = self._hires_frame()
            if src is None:
                src = frame
            sx, sy = src.shape[1] / w, src.shape[0] / h
            found = []
            for o in people:
                x1, y1, x2, y2 = o["box"]
                bw, bh = x2 - x1, y2 - y1
                # folga grande: a foto HD pode ser de um instante um pouco diferente
                cx1, cx2 = max(0, x1 - bw * 0.6), min(w, x2 + bw * 0.6)
                cy1, cy2 = max(0, y1 - bh * 0.3), min(h, y1 + bh * 0.75)
                X1, Y1, X2, Y2 = int(cx1 * sx), int(cy1 * sy), int(cx2 * sx), int(cy2 * sy)
                crop = src[Y1:Y2, X1:X2]
                if crop.size == 0:
                    continue
                k = 1.0
                if crop.shape[1] < 320:  # aumenta pedaço pequeno pro detector achar o rosto
                    k = 320 / crop.shape[1]
                    crop = cv2.resize(crop, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
                for f in brain.detect_faces(crop):
                    a1, b1, a2, b2 = f["box"]
                    f["box"] = (int((X1 + a1 / k) / sx), int((Y1 + b1 / k) / sy),
                                int((X1 + a2 / k) / sx), int((Y1 + b2 / k) / sy))
                    found.append(f)
            for f in found:
                if f["nome"]:
                    info["rostos"].add(f["nome"])
                elif not any(float(np.dot(f["emb"], e)) > 0.5 for e in info["faces_emb"]):
                    info["faces_emb"].append(f["emb"])
                    brain.save_unknown(f, self.cam["nome"])
                    info["rostos"].add("desconhecido")
            if found:
                nomes = ", ".join(sorted({f["nome"] or "desconhecido" for f in found}))
                if time.time() - getattr(self, "_face_log", 0) > 20:
                    self._face_log = time.time()
                    LOG.info(f"[{self.cam['nome']}] rosto: {nomes}")
            self.last_faces = (time.time(), found)
        except Exception as e:
            LOG.info(f"[{self.cam['nome']}] rostos falhou: {e}")
        finally:
            self._face_busy = False

    def _hires_frame(self):
        """Pega um quadro em resolução total do último pedaço gravado (melhor pra rostos)."""
        ff = find_ffmpeg()
        segs = sorted(self.buf_dir.glob("*.ts"))
        if not ff or len(segs) < 2:
            return None
        for seg in (segs[-1], segs[-2]):  # o pedaço sendo gravado agora tem a foto mais recente
            try:
                r = subprocess.run(
                    [ff, "-hide_banner", "-loglevel", "error", "-sseof", "-0.6", "-i", str(seg),
                     "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "3", "-"],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=8, creationflags=NO_WINDOW,
                )
                if r.returncode == 0 and r.stdout:
                    img = cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)
                    if img is not None:
                        return img
            except Exception:
                pass
        return None

    def set_grid(self, grade):
        self.cam["grade"] = grade

    def detections(self):
        fresh = time.time() - self.last_objs_t < 3
        return {"objetos": [{"nome": o["nome"], "tipo": o["tipo"], "conf": round(o["conf"], 2), "box": o["box"]}
                            for o in self.last_objs] if fresh else [],
                "dim": self.last_dim, "verificando": time.time() < self.candidate_until}

    def _snapshot(self, path):
        with self.frame_lock:
            f = None if self.frame is None else self.frame.copy()
        if f is not None:
            imwrite((path), f, [cv2.IMWRITE_JPEG_QUALITY, 85])

    def _finalize(self, ev, start, end):
        try:
            seg = self.cfg.get()["tamanho_segmento_segundos"]
            while not self.stop_ev.is_set() and time.time() < end + seg + 2:
                time.sleep(1)
            segs = []
            for p in self.buf_dir.glob("*.ts"):
                t = seg_time(p)
                if t is not None:
                    segs.append((t, p))
            segs.sort()
            segs = segs[:-1]
            chosen_t = [(t, p) for t, p in segs if start - seg <= t <= end]
            chosen = [p for t, p in chosen_t]
            video_t0 = chosen_t[0][0] if chosen_t else start  # hora real do 1º quadro do vídeo
            ff = find_ffmpeg()
            out = ev["out"]
            if not chosen or not ff:
                LOG.info(f"[{self.cam['nome']}] sem vídeo pra montar o clipe")
                return
            lst = out.with_suffix(".txt")
            lst.write_text(
                "".join("file '" + p.resolve().as_posix().replace("'", "'\\''") + "'\n" for p in chosen),
                encoding="utf-8",
            )
            r = subprocess.run(
                [ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(lst), "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
            )
            safe_unlink(lst)
            if r.returncode == 0:
                info = ev.get("info") or self._new_info()
                tipo = ev["tipo"]
                if tipo == "movimento" and info["objetos"]:
                    kinds = [ai.KIND.get(k) for k in info["objetos"]]
                    tipo = next((k for k in AI_KINDS if k in kinds), tipo)
                if info["best"] is not None:  # foto do alerta com as caixas da IA
                    imwrite(out.with_suffix(".jpg"), info["best"], [cv2.IMWRITE_JPEG_QUALITY, 85])
                meta = {
                    "camera_id": self.cam["id"], "camera": self.cam["nome"], "tipo": tipo,
                    "inicio": video_t0, "fim": end, "duracao": round(end - video_t0),
                    "objetos": {ai.LABEL_PT.get(k, k): v for k, v in info["objetos"].items()},
                    "rostos": sorted(info["rostos"]),
                    "pets": sorted(info.get("pets", [])),
                    "deteccoes": [dict(d, t=round(d["t"] - video_t0, 2)) for d in info.get("trilha", []) if d["t"] >= video_t0 - 1],
                }
                out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
                LOG.info(f"[{self.cam['nome']}] clipe salvo {out.name}")
                CLIP_SEQ[0] += 1
        finally:
            with self.pending_lock:
                self.pending.pop(ev["id"], None)


# ------------------------------------------------------------------ gerente --

class Manager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.workers = {}
        self.signatures = {}
        self._booted = set()
        self.lock = threading.RLock()
        self.alerts = deque(maxlen=100)  # notificações recentes pra interface
        self.alert_seq = 0
        kill_orphan_ffmpeg(cfg.recordings_dir().resolve())
        threading.Thread(target=self._housekeeping, daemon=True).start()
        threading.Thread(target=self._ip_watch, daemon=True).start()

    def _on_identity(self, cid, info):
        cams = {c["id"]: c for c in self.cfg.get()["cameras"]}
        cam = cams.get(cid)
        if cam and not cam.get("serial"):
            cam.update({"serial": info["serial"], "modelo": info.get("modelo", "")})
            self.cfg.upsert_camera(cam)
            w = self.workers.get(cid)
            if w:
                w.cam["serial"] = info["serial"]
            self.signatures[cid] = self._sig(cam)
            LOG.info(f"[{cam['nome']}] identificada: {info.get('modelo', '')} nº {info['serial']}")

    def _ip_watch(self):
        """Se uma câmera some por mais de 1 min, procura na rede e corrige o IP sozinho."""
        while True:
            time.sleep(30)
            try:
                lost = [w for w in list(self.workers.values())
                        if time.time() - max(w.frame_t, w.started_at) > 60]
                if not lost:
                    continue
                found = onvif.discover()
                for w in lost:
                    new_ip = self._find_camera(w.cam, found)
                    if new_ip and new_ip != w.cam["ip"]:
                        cam = next((c for c in self.cfg.get()["cameras"] if c["id"] == w.cam["id"]), None)
                        if cam:
                            LOG.info(f"[{cam['nome']}] IP mudou: {cam['ip']} -> {new_ip}; reconectando")
                            cam["ip"] = new_ip
                            self.cfg.upsert_camera(cam)
                            self.sync()
            except Exception as e:
                LOG.info(f"busca de câmera: {e}")

    @staticmethod
    def _find_camera(cam, found):
        candidates = [d for d in found if d["ip"] != cam["ip"] and d.get("onvif")]
        matches = []
        for d in candidates:
            try:
                c = onvif.OnvifClient(d["ip"], d.get("porta_onvif") or cam["porta_onvif"], cam["usuario"], cam["senha"])
                c.sync_time()
                info = c.device_info()
            except Exception:
                continue  # não aceitou o login: não é ela
            if cam.get("serial"):
                if info.get("serial") == cam["serial"]:
                    return d["ip"]
            elif not cam.get("modelo") or info.get("modelo") == cam.get("modelo"):
                matches.append(d["ip"])
        return matches[0] if len(matches) == 1 else None

    # ajustes que mudam na hora, sem reiniciar a câmera (nem a gravação)
    SOFT = {"nome", "disparar_com", "ia_ao_vivo", "sensibilidade", "reconhecer_rostos", "fps_ao_vivo",
            "grade", "ao_vivo_alta", "senha_nuvem_tapo", "ia_ativa", "serial", "modelo"}

    @classmethod
    def _sig(cls, cam):
        return json.dumps({k: v for k, v in (cam or {}).items() if k not in cls.SOFT}, sort_keys=True) if cam else ""

    def sync(self):
        """Liga/desliga/reinicia câmeras conforme a configuração."""
        with self.lock:
            cams = {c["id"]: c for c in self.cfg.get()["cameras"] if c["ativa"] and c["ip"]}
            for cid in list(self.workers):
                if cid not in cams or self._sig(cams[cid]) != self.signatures.get(cid):
                    self.workers.pop(cid).stop()
                    self.signatures.pop(cid, None)
                else:  # só mudou ajuste leve: aplica sem reiniciar
                    w, new = self.workers[cid], cams[cid]
                    if bool(new.get("ao_vivo_alta")) != bool(w.cam.get("ao_vivo_alta")):
                        w.set_live_quality(new.get("ao_vivo_alta"))
                    for k in self.SOFT:
                        if k in new:
                            w.cam[k] = new[k]
            for cid, cam in cams.items():
                if cid not in self.workers:
                    if cam.get("ao_vivo_alta") and cid not in self._booted:
                        # sempre abre no leve (mais estável); o alto liga pelo menu quando quiser
                        cam = self.cfg.upsert_camera(dict(cam, ao_vivo_alta=False))
                    self._booted.add(cid)
                    w = CameraWorker(cam, self.cfg, self._on_alert, self._on_identity)
                    self.workers[cid] = w
                    self.signatures[cid] = self._sig(cam)
                    w.start()

    def stop_all(self):
        with self.lock:
            for w in self.workers.values():
                w.stop()
            self.workers.clear()

    def worker(self, cid):
        return self.workers.get(cid)

    def _on_alert(self, worker, kind, when, snap):
        self.alert_seq += 1
        self.alerts.appendleft({
            "seq": self.alert_seq, "camera": worker.cam["nome"], "camera_id": worker.cam["id"],
            "tipo": kind, "quando": when.timestamp(),
        })
        url = self.cfg.get().get("webhook_alerta")
        if url:
            threading.Thread(target=self._webhook, args=(url, worker.cam["nome"], kind, when, snap), daemon=True).start()

    @staticmethod
    def _webhook(url, cam, kind, when, snap):
        time.sleep(0.5)
        try:
            payload = {"camera": cam, "tipo": kind, "horario": when.isoformat(timespec="seconds"),
                       "mensagem": f"{kind.capitalize()} detectado em {cam}"}
            if Path(snap).exists():
                payload["foto_jpg_base64"] = base64.b64encode(Path(snap).read_bytes()).decode()
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            LOG.info(f"webhook falhou: {e}")

    # ------------------------------------------------------------ alertas --
    def list_alerts(self, camera=None, desde=None, ate=None, tipo=None):
        root = self.cfg.recordings_dir()
        names = {c["id"]: c["nome"] for c in self.cfg.get()["cameras"]}
        out = []
        for mp4 in root.glob("*/*/*.mp4"):
            cid, day = mp4.parent.parent.name, mp4.parent.name
            if camera and cid != camera:
                continue
            meta = {}
            mj = mp4.with_suffix(".json")
            if mj.exists():
                try:
                    meta = json.loads(mj.read_text(encoding="utf-8"))
                except Exception:
                    pass
            try:
                ts = meta.get("inicio") or datetime.strptime(f"{day} {mp4.stem}", "%Y-%m-%d %H-%M-%S").timestamp()
            except ValueError:
                continue
            if desde and ts < desde or ate and ts > ate:
                continue
            t = meta.get("tipo", "movimento")
            if tipo and t not in tipo:
                continue
            out.append({
                "id": f"{cid}/{day}/{mp4.stem}", "camera_id": cid, "camera": names.get(cid, meta.get("camera", cid)),
                "tipo": t, "inicio": ts, "duracao": meta.get("duracao"),
                "objetos": meta.get("objetos") or {}, "rostos": meta.get("rostos") or [],
                "pets": meta.get("pets") or [],
                "mb": round(mp4.stat().st_size / 1024 ** 2, 1),
            })
        out.sort(key=lambda a: a["inicio"], reverse=True)
        return out

    def clip_path(self, alert_id, suffix):
        root = self.cfg.recordings_dir().resolve()
        p = (root / alert_id).with_suffix(suffix).resolve()
        if root not in p.parents or not p.exists():
            return None
        return p

    def delete_alert(self, alert_id):
        p = self.clip_path(alert_id, ".mp4")
        if not p:
            return False
        for s in (".mp4", ".jpg", ".json"):
            safe_unlink(p.with_suffix(s))
        try:
            p.parent.rmdir()
        except OSError:
            pass
        return True

    # ------------------------------------------------------------- limpeza --
    def _housekeeping(self):
        while True:
            try:
                self.cleanup()
            except Exception as e:
                LOG.info(f"limpeza: {e}")
            time.sleep(600)

    def cleanup(self):
        c = self.cfg.get()
        root = self.cfg.recordings_dir()
        limit = time.time() - c["dias_guardar"] * 86400
        clips = sorted(root.glob("*/*/*.mp4"), key=_mtime)
        removed = 0
        for mp4 in list(clips):
            if _mtime(mp4) < limit:
                self._rm(mp4)
                clips.remove(mp4)
                removed += 1
        min_free = c["espaco_livre_minimo_gb"] * 1024 ** 3
        while clips and shutil.disk_usage(root).free < min_free:
            self._rm(clips.pop(0))
            removed += 1
        max_bytes = float(c.get("tamanho_maximo_gb") or 0) * 1024 ** 3
        if max_bytes > 0:  # limite de tamanho das gravações: apaga os mais velhos até caber
            def size(mp4):
                return sum(p.stat().st_size for p in (mp4, mp4.with_suffix(".jpg"), mp4.with_suffix(".json")) if p.exists())
            sizes = [size(m) for m in clips]
            total = sum(sizes)
            while clips and total > max_bytes:
                self._rm(clips.pop(0))
                total -= sizes.pop(0)
                removed += 1
        if removed:
            LOG.info(f"limpeza: {removed} alerta(s) apagado(s)")

    @staticmethod
    def _rm(mp4):
        for s in (".mp4", ".jpg", ".json"):
            safe_unlink(mp4.with_suffix(s))
        try:
            mp4.parent.rmdir()
        except OSError:
            pass
