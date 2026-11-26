"""Caminhos, configuração e utilidades compartilhadas do FTCam."""
import copy
import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

VERSAO = "1.1.11"

FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))  # arquivos empacotados (ui, ícone)
def _writable(d):
    try:
        d.mkdir(parents=True, exist_ok=True)
        t = d / ".teste_escrita"
        t.write_text("ok")
        t.unlink()
        return True
    except OSError:
        return False


# Portátil: guarda tudo junto do programa. Se a pasta for protegida (ex.: Program Files),
# usa a pasta do usuário (%LOCALAPPDATA%\FTCam), que o Windows sempre deixa gravar.
if _writable(APP_DIR / "dados"):
    BASE_DATA = APP_DIR
else:
    BASE_DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "FTCam"
DATA_DIR = BASE_DATA / "dados"
CONFIG_PATH = DATA_DIR / "config.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# no Windows, impede que cada ffmpeg abra uma janela preta
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

DEFAULT_CONFIG = {
    "pasta_gravacoes": "gravacoes",
    "dias_guardar": 7,
    "espaco_livre_minimo_gb": 2,
    "tamanho_maximo_gb": 0,  # 0 = sem limite; passou disso, apaga os alertas mais velhos
    "segundos_antes": 10,
    "segundos_depois": 20,
    "clipe_minimo_segundos": 30,
    "clipe_maximo_segundos": 120,
    "pausa_entre_alertas_segundos": 5,
    "tamanho_segmento_segundos": 5,
    "webhook_alerta": "",
    "painel": {"porta": 8080, "acesso_rede": True, "senha": ""},
    "iniciar_com_windows": False,
    "abrir_ao_ligar": "tela_cheia",  # tela_cheia | janela | bandeja
    "atualizar_sozinho": True,  # baixa a versão nova do GitHub sozinho
    "cameras": [],
}

DEFAULT_CAMERA = {
    "id": "",
    "nome": "Câmera",
    "ip": "",
    "porta_rtsp": 554,
    "porta_onvif": 2020,
    "usuario": "",
    "senha": "",
    "senha_nuvem_tapo": "",
    "deteccao": "tapo",  # "tapo" = eventos ONVIF da câmera | "ftcam" = análise de imagem própria
    "sensibilidade": 50,  # 1 a 100 (só no modo ftcam)
    "disparar_com": [],  # tipos que gravam: [] = qualquer um; ex.: ["pessoa", "animal"]
    "ativa": True,
    "stream_gravacao": "stream1",
    "stream_leve": "stream2",
    "ao_vivo_alta": False,
    "ia_ativa": True,  # IA confirma pessoa/veículo/animal antes de gravar (quando escolhidos)
    "reconhecer_rostos": True,
    "ia_ao_vivo": False,
    "fps_ao_vivo": 0,  # limite de quadros/s no ao vivo (0 = o máximo que a câmera mandar)  # mostra os quadrados da IA na imagem ao vivo (gasta CPU o tempo todo)
    "grade": [],  # 16x9 = 144 células (1 = detecta ali); vazio = imagem toda  # True = ao vivo pelo stream de gravação (máxima qualidade, gasta mais CPU)
}


def _setup_log():
    log = logging.getLogger("ftcam")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        DATA_DIR / "ftcam.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout and not FROZEN:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    return log


LOG = _setup_log()


def find_ffmpeg():
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for d in (APP_DIR, RES_DIR):
        if (d / exe).exists():
            return str(d / exe)
    return shutil.which("ffmpeg")


def new_id():
    return uuid.uuid4().hex[:8]


class Config:
    """Configuração com trava e gravação atômica em dados/config.json."""

    def __init__(self):
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULT_CONFIG)
        if CONFIG_PATH.exists():
            try:
                saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                self._merge(self._data, saved)
            except Exception as e:
                LOG.info(f"config.json inválido, usando padrão: {e}")
        self._data["cameras"] = [self._fix_camera(c) for c in self._data.get("cameras", [])]
        self.save()

    @staticmethod
    def _merge(base, new):
        for k, v in new.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                Config._merge(base[k], v)
            else:
                base[k] = v

    @staticmethod
    def _fix_camera(cam):
        c = copy.deepcopy(DEFAULT_CAMERA)
        c.update(cam or {})
        if not c["id"]:
            c["id"] = new_id()
        c["sensibilidade"] = max(1, min(100, int(c["sensibilidade"])))
        return c

    def get(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def save(self):
        with self._lock:
            tmp = CONFIG_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, CONFIG_PATH)

    def update_global(self, values):
        allowed = set(DEFAULT_CONFIG) - {"cameras"}
        with self._lock:
            for k, v in values.items():
                if k in allowed:
                    if isinstance(v, dict) and isinstance(self._data.get(k), dict):
                        self._data[k].update(v)
                    else:
                        self._data[k] = v
            self.save()
            return self.get()

    def upsert_camera(self, cam):
        with self._lock:
            cam = self._fix_camera(cam)
            cams = self._data["cameras"]
            for i, c in enumerate(cams):
                if c["id"] == cam["id"]:
                    cams[i] = cam
                    break
            else:
                cams.append(cam)
            self.save()
            return copy.deepcopy(cam)

    def remove_camera(self, cam_id):
        with self._lock:
            self._data["cameras"] = [c for c in self._data["cameras"] if c["id"] != cam_id]
            self.save()

    def recordings_dir(self):
        p = Path(self.get()["pasta_gravacoes"])
        p = p if p.is_absolute() else BASE_DATA / p
        p.mkdir(parents=True, exist_ok=True)
        return p


def set_autostart(enabled):
    """Liga/desliga 'iniciar com o Windows' (chave Run do usuário)."""
    if os.name != "nt":
        return False
    if FROZEN:
        cmdline = f'"{sys.executable}" --inicio'
    else:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        cmdline = f'"{pyw if pyw.exists() else sys.executable}" "{APP_DIR / "app.py"}" --inicio'

    import winreg

    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE
    )
    try:
        if enabled:
            winreg.SetValueEx(key, "FTCam", 0, winreg.REG_SZ, cmdline)
        else:
            try:
                winreg.DeleteValue(key, "FTCam")
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)
    return True


def imwrite(path, img, params=None):
    """cv2.imwrite que aceita acento no caminho (o do OpenCV falha calado no Windows)."""
    import cv2
    ext = str(path)[str(path).rfind("."):] or ".jpg"
    ok, buf = cv2.imencode(ext, img, params or [])
    if ok:
        with open(path, "wb") as f:
            f.write(buf.tobytes())
    return ok


def imread(path):
    import cv2
    import numpy as np
    try:
        with open(path, "rb") as f:
            return cv2.imdecode(np.frombuffer(f.read(), np.uint8), cv2.IMREAD_COLOR)
    except OSError:
        return None


def kill_children():
    """Mata todos os processos filhos (ffmpeg etc.). No Windows eles NÃO morrem junto com o programa."""
    try:
        import psutil
        me = psutil.Process()
        kids = me.children(recursive=True)
        for k in kids:
            try:
                k.kill()
            except Exception:
                pass
        psutil.wait_procs(kids, timeout=3)
    except Exception:
        pass


def shutdown(mgr, code=0):
    """Sai de verdade: para as câmeras, mata os ffmpeg e encerra (com trava de segurança de 6 s)."""
    import threading

    def guard():
        time.sleep(6)
        kill_children()
        os._exit(code)
    threading.Thread(target=guard, daemon=True).start()
    t = threading.Thread(target=lambda: mgr and mgr.stop_all(), daemon=True)
    t.start()
    t.join(4)
    kill_children()
    LOG.info("FTCam fechado")
    os._exit(code)
