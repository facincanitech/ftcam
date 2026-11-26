"""FTCam — DVR para câmeras IP (Tapo e qualquer câmera RTSP/ONVIF).

Uso:
  FTCam.exe               abre a janela
  FTCam.exe --minimizado  começa escondido na bandeja (usado no 'iniciar com o Windows')
  python app.py --sem-janela   só o servidor (Linux/note sem tela); acesse pelo navegador
"""
import argparse
import os
import socket
import sys
import threading
import time

from core import DATA_DIR, LOG, RES_DIR, VERSAO, Config, shutdown


def port_free(port, host):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minimizado", action="store_true")  # nome antigo do --inicio (mantido pra quem já instalou)
    ap.add_argument("--inicio", action="store_true", help="aberto pelo 'iniciar com o Windows'")
    ap.add_argument("--sem-janela", action="store_true")
    ap.add_argument("--esperar", action="store_true", help="reinício da atualização: espera o FTCam antigo fechar")
    ap.add_argument("--bandeja", action="store_true", help="abre escondido na bandeja")
    args = ap.parse_args()

    cfg = Config()
    c = cfg.get()
    port = int(c["painel"]["porta"])
    host = "0.0.0.0" if c["painel"].get("acesso_rede", True) else "127.0.0.1"
    url = f"http://127.0.0.1:{port}/"

    if args.esperar:  # veio da atualização automática: espera o antigo liberar a porta
        for _ in range(40):
            if port_free(port, host):
                break
            time.sleep(1)

    if not port_free(port, host):
        # já tem um FTCam aberto: só mostra a janela dele
        LOG.info("FTCam já está rodando; abrindo a interface")
        if not args.sem_janela:
            _open_window_only(url)
        return

    import engine
    import web

    mgr = engine.Manager(cfg)
    mgr.sync()
    app = web.create_app(cfg, mgr)
    threading.Thread(
        target=lambda: app.run(host=host, port=port, threaded=True, use_reloader=False),
        daemon=True,
    ).start()
    LOG.info(f"FTCam {VERSAO} rodando em http://{host}:{port}")
    vis = {"oculta": args.bandeja}
    import updater
    restart_args = lambda: ["--bandeja"] if vis["oculta"] else []
    web.UPD["restart_args"] = restart_args
    updater.start_auto(cfg, mgr, restart_args)

    if args.sem_janela:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        mgr.stop_all()
        return

    try:
        import webview
    except Exception:
        LOG.info("pywebview indisponível; abrindo no navegador")
        import webbrowser
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        mgr.stop_all()
        return

    state = {"sair": False}
    # ligou o PC: abre do jeito escolhido nos ajustes (padrão: câmeras em tela cheia)
    modo = c.get("abrir_ao_ligar", "tela_cheia") if (args.inicio or args.minimizado) else "janela"
    if args.bandeja:
        modo = "bandeja"
    cheia = modo == "tela_cheia"
    window = webview.create_window(
        "FTCam", url + ("?cheia=1" if cheia else ""), width=1400, height=860, min_size=(960, 600),
        background_color="#0b1118", hidden=modo == "bandeja", fullscreen=cheia,
    )
    web.WIN.update(w=window, cheia=cheia)

    def on_closing():
        if state["sair"] or tray is None:
            return True
        window.hide()  # fechar = minimizar pra bandeja; gravação continua
        vis["oculta"] = True
        try:
            window.evaluate_js("window.__ftHidden=true")  # tela para de pedir imagem (economiza CPU)
        except Exception:
            pass
        return False

    window.events.closing += on_closing

    tray = _make_tray(window, state, mgr, vis)

    webview.start(private_mode=False, storage_path=str(DATA_DIR / "webview"))
    state["sair"] = True
    if tray:
        tray.stop()
    shutdown(mgr)


def _make_tray(window, state, mgr, vis):
    try:
        import pystray
        from PIL import Image
    except Exception:
        return None
    icon_path = RES_DIR / "icon.png"
    img = Image.open(icon_path) if icon_path.exists() else Image.new("RGB", (64, 64), "#3fa9f5")

    def show(icon=None, item=None):
        vis["oculta"] = False
        window.show()
        window.restore()
        try:
            window.evaluate_js("window.__ftHidden=false")
        except Exception:
            pass

    def quit_(icon, item):
        state["sair"] = True
        icon.stop()
        # sai de vez mesmo se a janela não fechar direito (e leva os ffmpeg junto)
        threading.Thread(target=shutdown, args=(mgr,), daemon=True).start()
        try:
            window.destroy()
        except Exception:
            pass

    tray = pystray.Icon(
        "FTCam", img, "FTCam — gravando",
        menu=pystray.Menu(
            pystray.MenuItem("Abrir FTCam", show, default=True),
            pystray.MenuItem("Sair (para de gravar)", quit_),
        ),
    )
    threading.Thread(target=tray.run, daemon=True).start()
    return tray


def _open_window_only(url):
    try:
        import webview
        webview.create_window("FTCam", url, width=1400, height=860, background_color="#0b1118")
        webview.start()
    except Exception:
        import webbrowser
        webbrowser.open(url)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG.exception("erro fatal")
        raise
    sys.exit(0)
