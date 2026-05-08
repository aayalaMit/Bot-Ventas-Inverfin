import os
import re
import time
import random
import asyncio
import discord
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from anthropic import Anthropic

# =========================
# Config
# =========================
load_dotenv(dotenv_path=".env")

DISCORD_TOKEN = os.getenv("TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

if not DISCORD_TOKEN:
    raise RuntimeError("Falta TOKEN (Bot Token de Discord) en .env")
if not ANTHROPIC_API_KEY:
    raise RuntimeError("Falta ANTHROPIC_API_KEY en .env")

ai = Anthropic(api_key=ANTHROPIC_API_KEY)
CLAUDE_MODEL = "claude-opus-4-6"

CATALOG_URL = "https://inverfin.com.py/collections/all"  # listado masivo de productos [1](https://inverfin.com.py/collections/all)
CATALOG_CACHE_TTL_SEC = 60 * 60  # 1 hora
_catalog_cache = {"ts": 0, "items": [], "source": "unknown"}

sessions = {}

PRICE_RE = re.compile(r"Gs\.\s*[\d\.]+")
BAD_TITLES = {
    "precio de venta", "precio", "contado", "promo", "impuestos incluidos",
    "seleccione forma de pago", "envío", "consultar", "agotado", "añadir al carrito"
}

# =========================
# Helper: banner producto
# =========================
def product_banner(product_a, product_b=None):
    txt = f"🛒 **Producto del escenario:** {product_a['name']} — **{product_a['price']}**"
    if product_b:
        txt += f"\n🆚 **Comparación:** {product_b['name']} — **{product_b['price']}**"
    return txt

# =========================
# Catálogo Inverfin (robusto)
# =========================
def _clean_title(s: str) -> str:
    s2 = " ".join(s.split())
    return s2.strip("•-–—:|")

def _is_bad_title(title: str) -> bool:
    t = title.lower().strip()
    if not t:
        return True
    if t in BAD_TITLES:
        return True
    # Evitar títulos muy cortos o puros precios
    if len(t) < 8:
        return True
    if "gs." in t:
        return True
    return False

def _find_price_near(node) -> str | None:
    """
    Busca un precio 'Gs.' cerca del nodo (en el mismo contenedor visual).
    """
    # 1) Buscar en el mismo contenedor
    text = node.get_text(separator="\n", strip=True)
    m = PRICE_RE.search(text)
    if m:
        return m.group(0)

    # 2) Buscar subiendo un poco
    parent = node.parent
    for _ in range(4):
        if not parent:
            break
        text = parent.get_text(separator="\n", strip=True)
        m = PRICE_RE.search(text)
        if m:
            return m.group(0)
        parent = parent.parent

    return None

def _fetch_catalog_items_sync(limit=60):
    """
    Extrae productos desde /collections/all usando:
    - links /products/... (títulos reales)
    - precio cercano en el card
    Si falla, devuelve fallback basado en ejemplos del sitio.
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(CATALOG_URL, timeout=12, headers=headers)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        items = []
        seen = set()

        # Estrategia: tomar anchors hacia /products/ (hay muchos en Inverfin) [2](https://inverfin.com.py/products/moto-taiga-arizona-tl150-2t-pro-2023)[3](https://inverfin.com.py/products/moto-taiga-arizona-tl200-2t-pro)
        for a in soup.select('a[href*="/products/"]'):
            href = a.get("href", "")
            if not href or "/products/" not in href:
                continue

            title = _clean_title(a.get_text(" ", strip=True))
            # A veces el anchor no contiene texto útil; intentar atributo title/aria-label
            if _is_bad_title(title):
                title = _clean_title(a.get("title", "") or a.get("aria-label", "") or "")

            if _is_bad_title(title):
                continue

            price = _find_price_near(a)
            if not price:
                continue

            key = (title, price)
            if key in seen:
                continue
            seen.add(key)

            items.append({"name": title, "price": price, "url": href})

            if len(items) >= limit:
                break

        if not items:
            raise ValueError("No se detectaron productos con /products/ + precio.")

        return items, "web"

    except Exception:
        # Fallback mínimo (coincide con precios visibles en el listado) [1](https://inverfin.com.py/collections/all)
        return [
            {"name": "AIRE GOODWEATHER 24.000 BTU - INVERTER", "price": "Gs. 5.980.000", "url": ""},
            {"name": "AIRE GOODWEATHER 12.000 BTU GW-12INVS - INVERTER", "price": "Gs. 2.795.000", "url": ""},
            {"name": "AIRE GOODWEATHER 12.000 BTU GW-12FO", "price": "Gs. 2.199.000", "url": ""},
        ], "fallback"

async def get_catalog():
    now = time.time()
    if now - _catalog_cache["ts"] > CATALOG_CACHE_TTL_SEC or not _catalog_cache["items"]:
        # requests bloquea si se corre directo; por eso a thread [4](https://docs.github.com/en/codespaces/setting-up-your-project-for-codespaces/adding-a-dev-container-configuration/introduction-to-dev-containers)
        items, src = await asyncio.to_thread(_fetch_catalog_items_sync)
        _catalog_cache["items"] = items
        _catalog_cache["ts"] = now
        _catalog_cache["source"] = src
    return _catalog_cache["items"], _catalog_cache["source"]

def pick_product_pair(items):
    inverter = [x for x in items if "INVERTER" in x["name"].upper()]
    noninv = [x for x in items if "INVERTER" not in x["name"].upper()]
    if inverter and noninv:
        a = random.choice(inverter)
        tokens = set(a["name"].upper().split())
        candidates = []
        for b in noninv:
            overlap = len(tokens.intersection(set(b["name"].upper().split())))
            if overlap >= 2:
                candidates.append(b)
        b = random.choice(candidates) if candidates else random.choice(noninv)
        return a, b
    return random.choice(items), None

# =========================
# Rúbrica / Prompts Claude
# =========================
RUBRIC_TEXT = """Rúbrica (100 puntos):
1) Apertura y empatía (20): escucha y genera confianza sin discutir.
2) Descubrimiento de necesidades (20): preguntas abiertas antes de ofrecer.
3) Propuesta de valor (20): conecta producto con necesidad (beneficios vs precio).
4) Manejo de objeciones (25): argumentos sólidos (garantía/respaldo/claridad).
5) Cierre y siguiente paso (15): propone acción concreta sin presionar.
"""

def build_customer_prompt(scenario, product_a, product_b, history):
    base = f"""
Eres un CLIENTE realista y exigente en una tienda (Paraguay).
Regla de oro: NO ayudes al vendedor ni le des la razón fácilmente.

Escenario {scenario}/5
Producto: {product_a['name']} — {product_a['price']}
"""
    if product_b:
        base += f"Comparación: {product_b['name']} — {product_b['price']}\n"

    base += """
REGLA OBLIGATORIA:
La PRIMERA línea debe ser exactamente de este estilo:
"Estoy viendo: <NOMBRE_DEL_PRODUCTO> (<PRECIO>)."

Tu tarea:
1) Primera línea con nombre y precio (obligatorio).
2) Objeción fuerte y realista: precio, confianza, costos ocultos, garantía, entrega/instalación.
3) 1-3 párrafos breves.
4) No cierres la venta por iniciativa propia.
"""
    if history:
        base += "\nContexto previo (lo que dijo el vendedor):\n" + "\n".join(history[-6:])
    return base.strip()

def build_coach_prompt(scenario, product_a, product_b, transcript):
    prod = f"{product_a['name']} ({product_a['price']})"
    if product_b:
        prod += f" vs {product_b['name']} ({product_b['price']})"

    return f"""
Eres un COACH de ventas objetivo. Evaluarás a un vendedor tras una simulación.

Producto(s): {prod}
Escenario {scenario}/5

{RUBRIC_TEXT}

Reglas:
- Evalúa de manera objetiva (no personal).
- Si el vendedor pide aclaraciones relevantes (producto, uso, necesidad), eso suma a Descubrimiento.
- Da puntaje total /100 y puntaje por dimensión.
- Incluye una “opción correcta” (argumento ganador).
- Da 2 recomendaciones prácticas.
- Carita por puntaje: 0-59 😞, 60-79 😐, 80-100 🙂.
- Responde en español (México), tono profesional.

Transcripción:
{transcript}

Devuelve EXACTAMENTE en este formato:

CARITA: 😞/😐/🙂
PUNTAJE TOTAL: XX / 100
DETALLE:
- Apertura y empatía: XX/20
- Descubrimiento de necesidades: XX/20
- Propuesta de valor: XX/20
- Manejo de objeciones: XX/25
- Cierre y siguiente paso: XX/15

FEEDBACK:
- Qué hizo bien:
- Qué debe mejorar:

OPCIÓN CORRECTA:
(El argumento ganador)

RECOMENDACIONES:
1)
2)
""".strip()

def claude_text(system, user, max_tokens=600):
    resp = ai.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )  # SDK oficial [6](https://inverfin.com.py/collections/hogar)
    out = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            out += block.text
    return out.strip()

# =========================
# Discord
# =========================
intents = discord.Intents.default()
intents.message_content = True  # prefijos requieren message_content [5](https://www.youtube.com/watch?v=tLvUHWLtTcU)
client = discord.Client(intents=intents)

HELP_TEXT = """
Comandos:
- $entrenar          -> inicia entrenamiento (5 escenarios)
- $producto          -> muestra el producto del escenario actual
- $v <respuesta>     -> respuesta del vendedor
- $cliente           -> el cliente responde (multi-turn)
- $cerrar            -> evalúa escenario y avanza
- $status            -> progreso
- $reset             -> reiniciar
- $ayuda             -> ayuda
"""

@client.event
async def on_ready():
    print(f"We have logged in as {client.user}")

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    content = message.content.strip()
    uid = str(message.author.id)
    sess = sessions.get(uid)

    if content == "$ayuda":
        await message.channel.send(HELP_TEXT)
        return

    if content == "$reset":
        sessions.pop(uid, None)
        await message.channel.send("✅ Sesión reiniciada. Usa $entrenar.")
        return

    if content == "$status":
        if not sess:
            await message.channel.send("No tienes sesión activa. Usa $entrenar.")
        else:
            await message.channel.send(f"Progreso: escenario {sess['scenario']}/5.")
        return

    if content == "$entrenar":
        items, src = await get_catalog()
        a, b = pick_product_pair(items)

        sessions[uid] = {
            "scenario": 1,
            "product_a": a,
            "product_b": b,
            "history": [],
            "transcript": [],
            "catalog_source": src,
        }
        sess = sessions[uid]

        await message.channel.send("🧩 Entrenamiento iniciado (1/5).")
        await message.channel.send(f"📦 Fuente de catálogo: **{src}** (URL: {CATALOG_URL})")
        await message.channel.send(product_banner(a, b))

        customer_msg = claude_text(
            system="Eres un cliente difícil. No ayudes al vendedor.",
            user=build_customer_prompt(1, a, b, []),
            max_tokens=320
        )

        sess["transcript"].append(product_banner(a, b))
        sess["transcript"].append(f"CLIENTE: {customer_msg}")

        await message.channel.send(customer_msg)
        await message.channel.send("👉 Responde como vendedor con: $v <tu respuesta>. Usa $cliente para continuar el rol del cliente. Para cerrar: $cerrar")
        return

    if not sess:
        return

    if content == "$producto":
        await message.channel.send(product_banner(sess["product_a"], sess["product_b"]))
        return

    if content.startswith("$v "):
        seller_text = content[3:].strip()
        sess["transcript"].append(f"VENDEDOR: {seller_text}")
        sess["history"].append(seller_text)
        await message.channel.send("✅ Respuesta registrada. Usa $cliente o $cerrar.")
        return

    if content == "$cliente":
        a = sess["product_a"]
        b = sess["product_b"]
        customer_msg = claude_text(
            system="Eres un cliente difícil. Sé realista y escéptico.",
            user=build_customer_prompt(sess["scenario"], a, b, sess["history"]),
            max_tokens=320
        )
        sess["transcript"].append(f"CLIENTE: {customer_msg}")
        await message.channel.send(customer_msg)
        return

    if content == "$cerrar":
        a = sess["product_a"]
        b = sess["product_b"]
        transcript = "\n".join(sess["transcript"])

        evaluation = claude_text(
            system="Eres un coach de ventas objetivo y constructivo.",
            user=build_coach_prompt(sess["scenario"], a, b, transcript),
            max_tokens=650
        )

        await message.channel.send("📊 Evaluación del escenario:")
        await message.channel.send(evaluation)

        if sess["scenario"] >= 5:
            sessions.pop(uid, None)
            await message.channel.send("🏁 ¡Actividad completada! Finalizaste los 5 escenarios. ✅")
            return

        # siguiente escenario
        items, _src = await get_catalog()
        a2, b2 = pick_product_pair(items)
        sess["scenario"] += 1
        sess["product_a"] = a2
        sess["product_b"] = b2
        sess["history"] = []
        sess["transcript"] = []

        await message.channel.send(f"➡️ Nuevo escenario ({sess['scenario']}/5):")
        await message.channel.send(product_banner(a2, b2))

        customer_msg = claude_text(
            system="Eres un cliente difícil. No ayudes al vendedor.",
            user=build_customer_prompt(sess["scenario"], a2, b2, []),
            max_tokens=320
        )

        sess["transcript"].append(product_banner(a2, b2))
        sess["transcript"].append(f"CLIENTE: {customer_msg}")

        await message.channel.send(customer_msg)
        await message.channel.send("👉 Responde con $v <tu respuesta>. Usa $cliente. Para cerrar: $cerrar")
        return

client.run(DISCORD_TOKEN)