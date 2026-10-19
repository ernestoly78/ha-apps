"""
App de Home Assistant: movimientos Santander Chile (banca personas).

- Usa el scraper de fintself (Playwright/Chromium headless), endurecido:
  cada paso de tarjetas es tolerante a fallos, intenta expandir "ver más"
  y recorre estados de cuenta facturados anteriores si el sitio los ofrece.
- Acumula todo en /config/santander/historico.csv sin duplicar.
- Captura (opcional) las respuestas JSON internas del sitio en
  /config/santander/raw/<corrida>/ para descubrir su API y pedir más historia.
- Publica en HA por MQTT discovery: estado, últimos movimientos de cuenta
  corriente y de tarjeta, y un botón "Actualizar".
- Si el login falla, deja de reintentar solo (para no bloquear la clave) hasta
  que se apriete el botón o se cambie la configuración.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import threading
import time
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

OPTIONS_FILE = Path(os.environ.get("OPTIONS_FILE", "/data/options.json"))
BASE = Path(os.environ.get("SANTANDER_DIR", "/homeassistant/santander"))
RAW = BASE / "raw"
HIST = BASE / "historico.csv"
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/estado.json"))
TZ = ZoneInfo(os.environ.get("TZ", "America/Santiago"))
KEEP_RAW_RUNS = 5
MAX_PERIODOS = 12
ATTR_ROWS = 25

CAMPOS = ["fecha", "descripcion", "monto", "moneda", "tipo", "cuenta", "tipo_cuenta", "capturado"]
CLAVE_FILA = ["fecha", "descripcion", "monto", "moneda", "cuenta", "tipo_cuenta"]

PREFIX = "santander"
DEVICE = {
    "identifiers": ["santander_movimientos"],
    "name": "Santander",
    "manufacturer": "fintself",
    "model": "Banca personas",
}


def log(msg: str) -> None:
    print(f"{datetime.now(TZ):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def ahora() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Histórico
# --------------------------------------------------------------------------

def a_fila(m) -> dict:
    return {
        "fecha": m.date.strftime("%Y-%m-%d"),
        "descripcion": " ".join((m.description or "").split()),
        "monto": str(int(m.amount)) if m.amount == int(m.amount) else str(m.amount),
        "moneda": m.currency,
        "tipo": m.transaction_type or "",
        "cuenta": m.account_id or "",
        "tipo_cuenta": m.account_type or "",
        "capturado": ahora(),
    }


def leer_historico() -> list[dict]:
    if not HIST.exists():
        return []
    with open(HIST, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def unir_historico(nuevas: list[dict]) -> tuple[list[dict], int]:
    """Agrega solo lo que falta. Usa conteo por clave para no borrar dos
    compras legítimas idénticas el mismo día."""
    hist = leer_historico()
    en_hist = Counter(tuple(r[k] for k in CLAVE_FILA) for r in hist)
    en_lote = Counter(tuple(r[k] for k in CLAVE_FILA) for r in nuevas)
    faltan = {k: n - en_hist[k] for k, n in en_lote.items() if n > en_hist[k]}
    agregadas = 0
    for r in nuevas:
        k = tuple(r[c] for c in CLAVE_FILA)
        if faltan.get(k, 0) > 0:
            faltan[k] -= 1
            hist.append(r)
            agregadas += 1
    hist.sort(key=lambda r: (r["fecha"], r["capturado"]))
    BASE.mkdir(parents=True, exist_ok=True)
    tmp = HIST.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS, delimiter=";")
        w.writeheader()
        w.writerows(hist)
    tmp.replace(HIST)
    return hist, agregadas


# --------------------------------------------------------------------------
# Captura de respuestas internas del sitio
# --------------------------------------------------------------------------

SENSIBLE_URL = re.compile(r"login|logon|auth|oauth|token|session|clave|password|captcha", re.I)
SENSIBLE_KEY = re.compile(r"token|password|clave|pass|secret|authorization|cookie|pin", re.I)
INTERES_URL = re.compile(r"mov|cartola|factur|transac|saldo|cuenta|tarjeta|estado|period|product|resumen", re.I)


def redactar(obj):
    if isinstance(obj, dict):
        return {k: ("***" if SENSIBLE_KEY.search(str(k)) else redactar(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redactar(x) for x in obj]
    return obj


class Capturador:
    def __init__(self, carpeta: Path):
        self.carpeta = carpeta
        self.n = 0
        self.indice: list[dict] = []

    def on_response(self, resp) -> None:
        try:
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            url = resp.url
            if SENSIBLE_URL.search(url) or not INTERES_URL.search(url):
                return
            if "json" not in (resp.headers.get("content-type") or ""):
                return
            cuerpo = resp.json()
            post = None
            if req.post_data:
                try:
                    post = redactar(json.loads(req.post_data))
                except Exception:
                    post = "(no JSON)"
            self.n += 1
            self.carpeta.mkdir(parents=True, exist_ok=True)
            nombre = f"{self.n:03d}.json"
            dato = {
                "url": url,
                "metodo": req.method,
                "status": resp.status,
                "post": post,
                "respuesta": redactar(cuerpo),
            }
            texto = json.dumps(dato, ensure_ascii=False, indent=1)
            if len(texto) > 3_000_000:
                return
            (self.carpeta / nombre).write_text(texto, encoding="utf-8")
            self.indice.append({"archivo": nombre, "metodo": req.method, "url": url.split("?")[0], "bytes": len(texto)})
        except Exception as e:  # nunca botar el scraping por la captura
            log(f"captura: se omitió una respuesta ({type(e).__name__})")

    def cerrar(self) -> None:
        if self.indice:
            (self.carpeta / "indice.json").write_text(json.dumps(self.indice, ensure_ascii=False, indent=1), encoding="utf-8")


def podar_raw() -> None:
    if not RAW.exists():
        return
    corridas = sorted(p for p in RAW.iterdir() if p.is_dir())
    for p in corridas[:-KEEP_RAW_RUNS]:
        shutil.rmtree(p, ignore_errors=True)


# --------------------------------------------------------------------------
# Scraper endurecido
# --------------------------------------------------------------------------

def crear_scraper(incluir_tarjetas: bool, capturador: Capturador | None):
    from fintself.scrapers.cl.santander import SantanderScraper
    from fintself.core.exceptions import DataExtractionError

    VER_MAS = re.compile(r"(ver|cargar|mostrar)\s+m[aá]s", re.I)

    class Santander(SantanderScraper):
        def _login(self) -> None:
            if capturador is not None:
                self._ensure_page().on("response", capturador.on_response)
            super()._login()

        # -- expandir listas paginadas ------------------------------------
        def _expandir(self) -> None:
            page = self._ensure_page()
            for _ in range(40):
                try:
                    page.mouse.wheel(0, 20000)
                    page.wait_for_timeout(800)
                    boton = page.get_by_role("button", name=VER_MAS).first
                    if boton.count() == 0 or not boton.is_visible():
                        boton = page.get_by_text(VER_MAS).first
                        if boton.count() == 0 or not boton.is_visible():
                            return
                    boton.click(timeout=5000)
                    page.wait_for_timeout(1800)
                except Exception:
                    return

        def _extract_debit_card_movements(self, currency):
            self._expandir()
            return super()._extract_debit_card_movements(currency)

        def _extract_credit_card_movements(self, status, currency):
            self._expandir()
            return super()._extract_credit_card_movements(status, currency)

        # -- tarjetas tolerantes a fallos ---------------------------------
        def _seguro(self, etiqueta, fn, *a):
            try:
                return fn(*a) or []
            except Exception as e:
                log(f"  {etiqueta}: omitido ({type(e).__name__}: {str(e)[:120]})")
                return []

        def _moneda(self, status, moneda):
            try:
                self._switch_currency_tab(moneda)
            except DataExtractionError:
                if moneda == "USD":
                    return []  # tarjeta sin cupo en dólares
                # algunas tarjetas no muestran pestañas: se lee lo que haya
            return self._extract_credit_card_movements(status, moneda)

        def _periodos_anteriores(self, idx_tarjeta):
            """Recorre estados de cuenta facturados anteriores si la página
            tiene un selector de período (mat-select o select nativo)."""
            page = self._ensure_page()
            out = []
            selector = page.locator("mat-select").first
            if selector.count() == 0:
                nativo = page.locator("select").first
                if nativo.count() == 0:
                    log("  sin selector de períodos facturados")
                    return out
                opciones = nativo.locator("option").all_inner_texts()
                log(f"  períodos facturados disponibles: {len(opciones)}")
                for i in range(1, min(len(opciones), MAX_PERIODOS)):
                    try:
                        nativo.select_option(index=i)
                        page.wait_for_timeout(2500)
                        for mon in ("CLP", "USD"):
                            out += self._seguro(f"periodo {opciones[i].strip()} {mon}", self._moneda, "facturados", mon)
                    except Exception as e:
                        log(f"  período {i}: {type(e).__name__}")
                return out
            selector.click()
            page.wait_for_timeout(800)
            textos = page.locator("mat-option").all_inner_texts()
            page.keyboard.press("Escape")
            log(f"  períodos facturados disponibles: {len(textos)}")
            for i in range(1, min(len(textos), MAX_PERIODOS)):
                try:
                    self._navigate(self.BILLED_URL, timeout_override=60000)
                    page.wait_for_timeout(2000)
                    if idx_tarjeta > 0:
                        self._navigate_to_card_in_carousel(idx_tarjeta)
                    page.locator("mat-select").first.click()
                    page.wait_for_timeout(800)
                    page.locator("mat-option").nth(i).click()
                    page.wait_for_timeout(2500)
                    for mon in ("CLP", "USD"):
                        out += self._seguro(f"periodo {textos[i].strip()} {mon}", self._moneda, "facturados", mon)
                except Exception as e:
                    log(f"  período {textos[i].strip()}: {type(e).__name__}")
            return out

        def _scrape_movements(self):
            self.account_ids = {"corriente": {}, "credito": {}}
            self._seguro("ids de cuentas", self._extract_and_store_account_ids)
            todos = []
            if incluir_tarjetas:
                todos += self._seguro("tarjetas", self._tarjetas)
            todos += self._seguro("cuenta corriente", self._scrape_debit_card_movements)
            return todos

        def _tarjetas(self):
            page = self._ensure_page()
            out = []
            self._navigate(self.UNBILLED_URL, timeout_override=60000)
            tarjetas = self._get_all_credit_cards_from_carousel() or ["(única)"]
            for i, tid in enumerate(tarjetas):
                log(f"Tarjeta {i + 1}/{len(tarjetas)} {tid}")
                if tid != "(única)":
                    self.account_ids["credito"]["CLP"] = tid
                    self.account_ids["credito"]["USD"] = tid
                # no facturados
                self._navigate(self.UNBILLED_URL, timeout_override=60000)
                page.wait_for_timeout(2000)
                if i > 0 and not self._navigate_to_card_in_carousel(i):
                    continue
                for mon in ("CLP", "USD"):
                    out += self._seguro(f"no facturados {mon}", self._moneda, "no_facturados", mon)
                # facturado actual + anteriores
                self._navigate(self.BILLED_URL, timeout_override=60000)
                page.wait_for_timeout(2000)
                if i > 0 and not self._navigate_to_card_in_carousel(i):
                    continue
                for mon in ("CLP", "USD"):
                    out += self._seguro(f"facturados {mon}", self._moneda, "facturados", mon)
                out += self._seguro("períodos anteriores", self._periodos_anteriores, i)
            return out

    return Santander(headless=True, debug_mode=False)


# --------------------------------------------------------------------------
# MQTT
# --------------------------------------------------------------------------

def credenciales_mqtt() -> dict:
    if os.environ.get("MQTT_HOST"):
        return {"host": os.environ["MQTT_HOST"], "port": int(os.environ.get("MQTT_PORT", 1883)),
                "username": os.environ.get("MQTT_USER"), "password": os.environ.get("MQTT_PASS")}
    import requests

    r = requests.get("http://supervisor/services/mqtt",
                     headers={"Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}"}, timeout=10)
    r.raise_for_status()
    return r.json()["data"]


def payloads_discovery() -> list[tuple[str, dict]]:
    disp = f"{PREFIX}/disponible"
    comun = {"device": DEVICE, "availability_topic": disp}
    out = [
        (f"homeassistant/sensor/{PREFIX}/estado/config", {
            **comun, "name": "Estado", "unique_id": "santander_estado", "icon": "mdi:bank",
            "state_topic": f"{PREFIX}/estado", "value_template": "{{ value_json.estado }}",
            "json_attributes_topic": f"{PREFIX}/estado",
        }),
        (f"homeassistant/sensor/{PREFIX}/nuevos/config", {
            **comun, "name": "Movimientos nuevos", "unique_id": "santander_nuevos", "icon": "mdi:new-box",
            "state_topic": f"{PREFIX}/estado", "value_template": "{{ value_json.nuevos }}",
            "unit_of_measurement": "mov",
        }),
        (f"homeassistant/button/{PREFIX}/actualizar/config", {
            **comun, "name": "Actualizar", "unique_id": "santander_actualizar", "icon": "mdi:refresh",
            "command_topic": f"{PREFIX}/actualizar", "payload_press": "ahora",
        }),
    ]
    for clave, nombre, icono in (("corriente", "Cuenta corriente", "mdi:bank-transfer"),
                                 ("credito", "Tarjeta de crédito", "mdi:credit-card")):
        out.append((f"homeassistant/sensor/{PREFIX}/{clave}/config", {
            **comun, "name": f"{nombre} último movimiento", "unique_id": f"santander_{clave}_ultimo",
            "icon": icono, "state_topic": f"{PREFIX}/{clave}",
            "value_template": "{{ value_json.ultimo_monto }}", "unit_of_measurement": "CLP",
            "json_attributes_topic": f"{PREFIX}/{clave}",
        }))
    return out


def resumen_cuenta(hist: list[dict], tipo: str) -> dict:
    filas = [r for r in hist if r["tipo_cuenta"] == tipo]
    filas.sort(key=lambda r: (r["fecha"], r["capturado"]), reverse=True)
    ult = filas[0] if filas else None
    return {
        "ultimo_monto": int(float(ult["monto"])) if ult and ult["moneda"] == "CLP" else None,
        "ultima_fecha": ult["fecha"] if ult else None,
        "ultima_descripcion": ult["descripcion"] if ult else None,
        "total_historico": len(filas),
        "desde": filas[-1]["fecha"] if filas else None,
        "movimientos": [
            {k: r[k] for k in ("fecha", "descripcion", "monto", "moneda", "cuenta")} for r in filas[:ATTR_ROWS]
        ],
    }


# --------------------------------------------------------------------------
# Ciclo principal
# --------------------------------------------------------------------------

def leer_estado() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def guardar_estado(e: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(e, ensure_ascii=False))


def huella(opts: dict) -> str:
    return hashlib.sha256(f"{opts['rut']}|{opts['clave']}".encode()).hexdigest()


def main() -> None:
    import paho.mqtt.client as mqtt
    from fintself.core.exceptions import LoginError

    opts = json.loads(OPTIONS_FILE.read_text())
    BASE.mkdir(parents=True, exist_ok=True)
    estado = leer_estado()
    if estado.get("huella") != huella(opts):
        estado = {"huella": huella(opts)}  # credenciales nuevas: se levanta el bloqueo
        guardar_estado(estado)

    pedir = threading.Event()
    cred = credenciales_mqtt()
    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="santander_movimientos")
    if cred.get("username"):
        cli.username_pw_set(cred["username"], cred.get("password"))
    cli.will_set(f"{PREFIX}/disponible", "offline", retain=True)

    def on_connect(c, *_):
        for topic, p in payloads_discovery():
            c.publish(topic, json.dumps(p, ensure_ascii=False), retain=True)
        c.publish(f"{PREFIX}/disponible", "online", retain=True)
        c.subscribe(f"{PREFIX}/actualizar")

    def on_message(c, u, msg):
        log("Botón Actualizar presionado")
        pedir.set()

    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.connect(cred["host"], int(cred.get("port", 1883)))
    cli.loop_start()

    def publicar(est: str, extra: dict | None = None) -> None:
        hist = leer_historico()
        estado.update({"estado": est, **(extra or {})})
        guardar_estado(estado)
        pub = {k: v for k, v in estado.items() if k != "huella"}
        pub["total_historico"] = len(hist)
        pub["archivo_historico"] = str(HIST)
        cli.publish(f"{PREFIX}/estado", json.dumps(pub, ensure_ascii=False), retain=True)
        for tipo in ("corriente", "credito"):
            cli.publish(f"{PREFIX}/{tipo}", json.dumps(resumen_cuenta(hist, tipo), ensure_ascii=False), retain=True)

    publicar(estado.get("estado") or "esperando", {"nuevos": estado.get("nuevos", 0)})
    if not opts["rut"] or not opts["clave"]:
        publicar("sin_configurar")
        log("Falta RUT o clave en la configuración de la App.")

    intervalo = opts["intervalo_horas"] * 3600
    proxima = time.time() if opts["ejecutar_al_iniciar"] else time.time() + intervalo

    while True:
        pedir.wait(timeout=max(1, proxima - time.time()))
        manual = pedir.is_set()
        pedir.clear()
        if not manual and time.time() < proxima:
            continue
        proxima = time.time() + intervalo
        if not opts["rut"] or not opts["clave"]:
            continue
        if estado.get("bloqueado_login") and not manual:
            log("Login bloqueado tras un fallo: se espera el botón Actualizar o una clave nueva.")
            continue

        corrida = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
        cap = Capturador(RAW / corrida) if opts["capturar_api"] else None
        publicar("actualizando", {"inicio": ahora()})
        log("Iniciando descarga")
        try:
            scraper = crear_scraper(opts["incluir_tarjetas"], cap)
            movs = scraper.scrape(user=opts["rut"], password=opts["clave"])
            filas = [a_fila(m) for m in movs]
            hist, nuevos = unir_historico(filas)
            por_tipo = Counter(f["tipo_cuenta"] for f in filas)
            log(f"OK: {len(filas)} leídos ({dict(por_tipo)}), {nuevos} nuevos, {len(hist)} en histórico")
            publicar("ok", {
                "ultima_ejecucion": ahora(), "ultimo_ok": ahora(), "error": None,
                "leidos": len(filas), "leidos_por_tipo": dict(por_tipo), "nuevos": nuevos,
                "bloqueado_login": False, "capturas_api": cap.n if cap else 0,
            })
        except LoginError as e:
            log(f"Login fallido: {e.message}")
            publicar("login_fallido", {
                "ultima_ejecucion": ahora(), "nuevos": 0, "bloqueado_login": True,
                "error": "Login fallido. Revisa RUT/clave o si el banco pidió aprobar en Santander Pass. "
                         "No se reintentará solo hasta que aprietes Actualizar o cambies la clave.",
            })
        except Exception as e:
            log("Error:\n" + traceback.format_exc())
            publicar("error", {"ultima_ejecucion": ahora(), "nuevos": 0,
                               "error": f"{type(e).__name__}: {str(e)[:300]}"})
        finally:
            if cap:
                cap.cerrar()
            podar_raw()


if __name__ == "__main__":
    main()
