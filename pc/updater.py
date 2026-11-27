"""Atualização automática: o FTCam baixa a versão nova do GitHub sozinho.

Confere a página de Releases do repositório público. Se tiver versão mais nova, baixa o FTCam.zip,
troca só os arquivos do programa (nunca mexe em dados, gravações, modelos, ffmpeg nem no Python)
e reinicia sozinho, sem ninguém precisar fazer nada.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from core import APP_DIR, DATA_DIR, FROZEN, LOG, NO_WINDOW, VERSAO

REPO = "facincanitech/ftcam"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = "FTCam.zip"
# nunca são tocados pela atualização
KEEP = {"dados", "gravacoes", ".venv", "modelos", "ffmpeg", "webview", "__pycache__"}

STATE = {"ultima": None, "nova": None, "erro": "", "atualizando": False}


def ver(s):
    return tuple(int(x) for x in re.findall(r"\d+", s or "")[:3]) or (0,)


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": f"FTCam/{VERSAO}",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def check():
    """Devolve (versão, url do zip) se tiver versão mais nova; senão None."""
    with _get(API) as r:
        rel = json.load(r)
    tag = rel.get("tag_name", "")
    STATE["ultima"] = time.time()
    if ver(tag) <= ver(VERSAO):
        STATE["nova"] = None
        return None
    STATE["nova"] = tag.lstrip("v")
    for a in rel.get("assets", []):
        if a.get("name") == ASSET:   # versões antigas publicavam um zip separado
            return tag.lstrip("v"), a["browser_download_url"]
    # agora baixa o código da própria versão (o GitHub gera o zip sozinho)
    return tag.lstrip("v"), rel["zipball_url"]


def install(zip_url, dest=APP_DIR):
    """Baixa e troca os arquivos do programa. Devolve True se precisa reiniciar."""
    tmp = Path(tempfile.mkdtemp(prefix="ftcam_upd_"))
    try:
        zpath = tmp / ASSET
        with _get(zip_url, timeout=120) as r, open(zpath, "wb") as f:
            shutil.copyfileobj(r, f)
        with zipfile.ZipFile(zpath) as z:
            if z.testzip() is not None:
                raise RuntimeError("zip corrompido")
            z.extractall(tmp / "x")
        # acha a pasta do programa dentro do zip (FTCam/ no zip antigo, <repo>/pc/ no do GitHub)
        src = next((p.parent for p in sorted((tmp / "x").rglob("app.py"), key=lambda q: len(q.parts))
                    if (p.parent / "core.py").exists()), None)
        if src is None:
            raise RuntimeError("pacote sem os arquivos do FTCam")
        old_req = (dest / "requirements.txt").read_text(encoding="utf-8") if (dest / "requirements.txt").exists() else ""
        for item in src.iterdir():
            if item.name in KEEP:
                continue
            target = dest / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        new_req = (dest / "requirements.txt").read_text(encoding="utf-8") if (dest / "requirements.txt").exists() else ""
        if new_req != old_req:
            _pip_install(dest)
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _python_console():
    """python.exe ao lado do pythonw.exe (pip precisa do de console)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return str(exe)


def _pip_install(dest):
    LOG.info("atualização: instalando bibliotecas novas…")
    subprocess.run([_python_console(), "-m", "pip", "install", "-q", "-r", str(dest / "requirements.txt")],
                   cwd=str(dest), timeout=900, creationflags=NO_WINDOW)


def restart(mgr, extra_args):
    """Abre o FTCam novo e fecha este (o novo espera a porta liberar)."""
    from core import shutdown
    args = [sys.executable, str(APP_DIR / "app.py"), "--esperar"] + list(extra_args)
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(args, cwd=str(APP_DIR), creationflags=flags, close_fds=True)
    LOG.info("atualização instalada; reiniciando o FTCam")
    shutdown(mgr)


def busy(mgr):
    """Não atualiza no meio de um alerta sendo gravado."""
    return any(getattr(w, "recording_event", False) for w in list(mgr.workers.values()))


def run_now(cfg, mgr, restart_args, force=False):
    """Confere e, se tiver versão nova, instala e reinicia. Devolve texto pra mostrar."""
    if FROZEN:
        return "Versão .exe: atualize baixando de novo no site"
    if STATE["atualizando"]:
        return "Já está atualizando"
    try:
        found = check()
    except Exception as e:
        STATE["erro"] = f"sem internet ou GitHub fora ({e.__class__.__name__})"
        return "Não consegui conferir agora"
    STATE["erro"] = ""
    if not found:
        return f"Você já está na versão mais nova ({VERSAO})"
    nova, url = found
    if not force and busy(mgr):
        return f"Versão {nova} disponível; instala quando terminar o alerta"
    STATE["atualizando"] = True
    try:
        LOG.info(f"atualização: baixando a versão {nova} (atual {VERSAO})")
        install(url)
    except Exception as e:
        STATE["atualizando"] = False
        STATE["erro"] = str(e)
        LOG.info(f"atualização falhou: {e}")
        return "A atualização falhou; continua na versão atual"
    threading.Timer(1.5, restart, args=(mgr, ["--atualizado"])).start()  # volta aberto e maximizado
    return f"Atualizando pra {nova}… o FTCam reinicia sozinho"


def start_auto(cfg, mgr, restart_args):
    """Confere 2 min depois de abrir e depois a cada 6 horas."""
    def loop():
        time.sleep(120)
        while True:
            if cfg.get().get("atualizar_sozinho", True):
                msg = run_now(cfg, mgr, restart_args)
                LOG.info(f"atualização: {msg}") if "disponível" in msg or "falhou" in msg else None
                if busy(mgr):  # tinha versão nova mas estava gravando: tenta de novo logo
                    time.sleep(300)
                    continue
            time.sleep(6 * 3600)
    threading.Thread(target=loop, daemon=True).start()
