"""ONVIF mínimo, sem dependências: descoberta na rede e eventos de movimento da câmera."""
import base64
import concurrent.futures as cf
import datetime as dt
import hashlib
import ipaddress
import os
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tt": "http://www.onvif.org/ver10/schema",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wsa": "http://www.w3.org/2005/08/addressing",
    "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
}
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"


# ------------------------------------------------------------- descoberta --

PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header><w:MessageID>uuid:{id}</w:MessageID>
<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"""


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # não envia nada, só descobre a interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def ws_discovery(timeout=3.0):
    """Pergunta na rede quem é câmera ONVIF (multicast 239.255.255.250:3702)."""
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip()))
    except OSError:
        pass
    sock.settimeout(0.5)
    msg = PROBE.format(id=uuid.uuid4()).encode()
    try:
        for _ in range(2):
            sock.sendto(msg, ("239.255.255.250", 3702))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, (ip, _) = sock.recvfrom(65535)
            except socket.timeout:
                continue
            text = data.decode("utf-8", "ignore")
            info = {"ip": ip, "onvif": True, "porta_onvif": 80, "nome": "", "modelo": ""}
            m = re.search(r"XAddrs>([^<]+)<", text)
            if m:
                for addr in m.group(1).split():
                    pm = re.match(r"https?://([\d.]+)(?::(\d+))?/", addr)
                    if pm and pm.group(1) == ip:
                        info["porta_onvif"] = int(pm.group(2) or 80)
            sm = re.search(r"Scopes>([^<]+)<", text)
            if sm:
                for sc in sm.group(1).split():
                    if "/name/" in sc:
                        info["nome"] = urllib.request.unquote(sc.rsplit("/", 1)[-1])
                    elif "/hardware/" in sc:
                        info["modelo"] = urllib.request.unquote(sc.rsplit("/", 1)[-1])
            found[ip] = info
    finally:
        sock.close()
    return found


def _port_open(ip, port, timeout=0.4):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def port_scan(ports=(554, 2020)):
    """Varre a rede /24 procurando RTSP (554) e ONVIF da Tapo (2020)."""
    me = local_ip()
    if me.startswith("127."):
        return {}
    net = ipaddress.ip_network(f"{me}/24", strict=False)
    hosts = [str(h) for h in net.hosts() if str(h) != me]
    found = {}

    def check(ip):
        open_ = [p for p in ports if _port_open(ip, p)]
        return ip, open_

    with cf.ThreadPoolExecutor(max_workers=64) as ex:
        for ip, open_ in ex.map(check, hosts):
            if open_:
                found[ip] = {
                    "ip": ip,
                    "onvif": 2020 in open_,
                    "porta_onvif": 2020 if 2020 in open_ else 80,
                    "rtsp": 554 in open_,
                    "nome": "",
                    "modelo": "",
                }
    return found


def discover():
    """Junta as duas buscas. Retorna lista ordenada por IP."""
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(ws_discovery)
        f2 = ex.submit(port_scan)
        a, b = f1.result(), f2.result()
    for ip, info in a.items():
        if ip in b:
            b[ip].update({k: v for k, v in info.items() if v})
        else:
            b[ip] = info
    return sorted(b.values(), key=lambda d: tuple(int(x) for x in d["ip"].split(".")))


# ------------------------------------------------------------------ SOAP --

class OnvifError(Exception):
    pass


class OnvifClient:
    def __init__(self, ip, port, user, password):
        self.ip, self.port, self.user, self.password = ip, port, user, password
        self.device_url = f"http://{ip}:{port}/onvif/device_service"
        self.offset = dt.timedelta(0)  # diferença de relógio câmera x PC

    def _security(self):
        nonce = os.urandom(16)
        created = (dt.datetime.now(dt.timezone.utc) + self.offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()
        ).decode()
        return (
            f'<wsse:Security s:mustUnderstand="1" xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}">'
            f"<wsse:UsernameToken><wsse:Username>{_esc(self.user)}</wsse:Username>"
            f'<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/'
            f'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
            f'<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
            f'oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}'
            f"</wsse:Nonce><wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>"
        )

    def call(self, url, action, body, auth=True, extra_headers="", timeout=10):
        header = (self._security() if auth else "") + extra_headers
        env = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{NS["s"]}" xmlns:wsa="{NS["wsa"]}" '
            f'xmlns:tds="{NS["tds"]}" xmlns:tev="{NS["tev"]}" xmlns:tt="{NS["tt"]}">'
            f"<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
        )
        req = urllib.request.Request(
            url,
            data=env.encode(),
            headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return ET.fromstring(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")
            if "NotAuthorized" in detail or e.code == 401:
                raise OnvifError("usuário ou senha recusados pela câmera") from None
            raise OnvifError(f"HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise OnvifError(f"sem resposta ({e})") from None

    def sync_time(self):
        try:
            root = self.call(
                self.device_url,
                "http://www.onvif.org/ver10/device/wsdl/GetSystemDateAndTime",
                "<tds:GetSystemDateAndTime/>",
                auth=False,
            )
            u = root.find(".//tt:UTCDateTime", NS)
            if u is None:
                return
            g = lambda p: int(u.find(p, NS).text)  # noqa: E731
            cam = dt.datetime(
                g("tt:Date/tt:Year"), g("tt:Date/tt:Month"), g("tt:Date/tt:Day"),
                g("tt:Time/tt:Hour"), g("tt:Time/tt:Minute"), g("tt:Time/tt:Second"),
                tzinfo=dt.timezone.utc,
            )
            self.offset = cam - dt.datetime.now(dt.timezone.utc)
        except Exception:
            pass

    def device_info(self):
        """Fabricante, modelo e número de série (serve pra reconhecer a câmera mesmo se o IP mudar)."""
        root = self.call(self.device_url, "http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation",
                         "<tds:GetDeviceInformation/>")
        g = lambda t: (root.find(f".//tds:{t}", NS).text or "").strip() if root.find(f".//tds:{t}", NS) is not None else ""  # noqa: E731
        return {"fabricante": g("Manufacturer"), "modelo": g("Model"), "serial": g("SerialNumber")}

    def events_url(self):
        root = self.call(
            self.device_url,
            "http://www.onvif.org/ver10/device/wsdl/GetCapabilities",
            "<tds:GetCapabilities><tds:Category>Events</tds:Category></tds:GetCapabilities>",
        )
        x = root.find(".//tt:Events/tt:XAddr", NS)
        if x is None or not x.text:
            raise OnvifError("câmera não informou o serviço de eventos")
        return x.text.strip()


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class EventSubscription:
    """Assinatura PullPoint: a câmera avisa quando ela mesma detecta algo."""

    def __init__(self, client):
        self.c = client
        self.address = None
        self.ref_params = ""
        self.created = 0.0

    def subscribe(self):
        self.c.sync_time()
        url = self.c.events_url()
        root = self.c.call(
            url,
            "http://www.onvif.org/ver10/events/wsdl/EventPortType/CreatePullPointSubscriptionRequest",
            "<tev:CreatePullPointSubscription>"
            "<tev:InitialTerminationTime>PT600S</tev:InitialTerminationTime>"
            "</tev:CreatePullPointSubscription>",
        )
        ref = root.find(".//tev:SubscriptionReference", NS)
        addr = ref.find("wsa:Address", NS) if ref is not None else None
        if addr is None:
            # algumas câmeras usam o namespace antigo de WS-Addressing
            addr = root.find(".//{http://schemas.xmlsoap.org/ws/2004/08/addressing}Address")
        if addr is None or not addr.text:
            raise OnvifError("câmera não criou a assinatura de eventos")
        self.address = addr.text.strip()
        params = ref.find("wsa:ReferenceParameters", NS) if ref is not None else None
        self.ref_params = "".join(ET.tostring(ch, encoding="unicode") for ch in params) if params is not None else ""
        self.created = time.time()

    def pull(self, timeout_s=5):
        """Retorna lista de (tópico, {nome: valor}) recebidos."""
        hdr = (
            "<wsa:Action>http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest"
            f"</wsa:Action><wsa:To>{_esc(self.address)}</wsa:To>{self.ref_params}"
        )
        root = self.c.call(
            self.address,
            "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest",
            f"<tev:PullMessages><tev:Timeout>PT{timeout_s}S</tev:Timeout>"
            "<tev:MessageLimit>32</tev:MessageLimit></tev:PullMessages>",
            extra_headers=hdr,
            timeout=timeout_s + 10,
        )
        out = []
        for nm in root.iter(f"{{{NS['wsnt']}}}NotificationMessage"):
            topic_el = nm.find("wsnt:Topic", NS)
            topic = (topic_el.text or "").strip() if topic_el is not None else ""
            items = {}
            data = nm.find(".//tt:Data", NS)
            for si in (data if data is not None else nm).iter(f"{{{NS['tt']}}}SimpleItem"):
                items[si.get("Name", "")] = si.get("Value", "")
            out.append((topic, items))
        return out

    def expired(self):
        return time.time() - self.created > 480  # renova antes dos 10 min


ITEM_KINDS = (
    (("people", "person", "human"), "pessoa"),
    (("vehicle", "car"), "veículo"),
    (("pet", "animal", "dog", "cat"), "animal"),
    (("linecross", "linedetector", "crossing"), "linha cruzada"),
    (("tamper",), "câmera obstruída"),
    (("motion",), "movimento"),
)


def _is_true(v):
    return str(v).strip().lower() in ("true", "1", "on")


def classify_all(topic, items):
    """Lista de (tipo, ativo) de um evento ONVIF.

    Olha primeiro os nomes dos campos (ex.: IsPeople, IsVehicle, IsPet, IsMotion), que é como a
    Tapo manda os eventos inteligentes, e depois o tópico (ex.: .../PeopleDetector/People)."""
    out = []
    for name, val in items.items():
        n = name.lower()
        for keys, kind in ITEM_KINDS:
            if any(k in n for k in keys):
                out.append((kind, _is_true(val)))
                break
    if out:
        return out
    t = topic.lower()
    active = any(_is_true(v) for v in items.values())
    for keys, kind in ITEM_KINDS:
        if any(k in t for k in keys):
            return [(kind, active)]
    return []


def classify(topic, items):
    r = classify_all(topic, items)
    return r[0] if r else (None, False)
