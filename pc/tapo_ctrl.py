"""Controles extras da Tapo (sirene, privacidade, LED, visão noturna) via pytapo.

Experimental: usa a API local da Tapo com usuário 'admin' e a SENHA DA CONTA TAPO
(a do app), não a 'Conta da câmera'. Se der erro, o resto do FTCam segue normal.
"""
import concurrent.futures as cf
import threading

try:
    from pytapo import Tapo
except Exception:  # pytapo não instalado
    Tapo = None

_cache = {}
_lock = threading.Lock()
_pool = cf.ThreadPoolExecutor(max_workers=4)


def available():
    return Tapo is not None


def _client(cam):
    key = (cam["ip"], cam["senha_nuvem_tapo"])
    with _lock:
        if key not in _cache:
            _cache[key] = Tapo(cam["ip"], "admin", cam["senha_nuvem_tapo"], cam["senha_nuvem_tapo"],
                               printWarnInformation=False)
        return _cache[key]


def _run(fn, timeout=15):
    fut = _pool.submit(fn)
    return fut.result(timeout=timeout)


def _reset(cam):
    with _lock:
        _cache.pop((cam["ip"], cam["senha_nuvem_tapo"]), None)


def status(cam):
    if not available():
        return {"ok": False, "erro": "pytapo não está instalado"}
    if not cam.get("senha_nuvem_tapo"):
        return {"ok": False, "erro": "Preencha a senha da conta Tapo nas configurações da câmera"}

    def go():
        t = _client(cam)
        info = t.getBasicInfo().get("device_info", {}).get("basic_info", {})
        out = {"ok": True, "modelo": info.get("device_model", ""), "firmware": info.get("sw_version", "")}
        for name, fn in (("privacidade", t.getPrivacyMode), ("led", t.getLED), ("noturno", t.getDayNightMode)):
            try:
                v = fn()
                if name == "privacidade":
                    v = v.get("enabled") == "on"
                elif name == "led":
                    v = v.get("enabled") == "on"
                out[name] = v
            except Exception:
                out[name] = None
        return out

    try:
        return _run(go)
    except Exception as e:
        _reset(cam)
        return {"ok": False, "erro": f"Não conectou na API da Tapo: {e}"}


def action(cam, acao, valor=None):
    if not available():
        return {"ok": False, "erro": "pytapo não está instalado"}

    def go():
        t = _client(cam)
        if acao == "sirene":
            t.startManualAlarm() if valor else t.stopManualAlarm()
        elif acao == "privacidade":
            t.setPrivacyMode(bool(valor))
        elif acao == "led":
            t.setLEDEnabled(bool(valor))
        elif acao == "noturno":
            t.setDayNightMode(valor)  # "auto" | "on" | "off"
        else:
            raise ValueError("ação desconhecida")
        return {"ok": True}

    try:
        return _run(go)
    except Exception as e:
        _reset(cam)
        return {"ok": False, "erro": str(e)}
