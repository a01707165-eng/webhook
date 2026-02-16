from flask import Flask, request, jsonify
import os
import json
import sqlite3
import requests
from datetime import datetime

app = Flask(__name__)

# ────────────────────────────────────────────────
#  CONFIGURACIÓN (ajusta estos valores)
# ────────────────────────────────────────────────
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "EAAeVR8IGTpgBQt33hL3tzElaxOA9mKxihsGWNUYwCWkmVYGRVxeVqZCw1ZCXuzrxZAx1dX94PBT5gRPT8B4k6tlO3vLr6dIaZCNFdPZAUczqBoY5jkrMHJQpmVyqxYHR4i313IEzuh5ZC5EtgnL1ZA7frqbfJcchcLPOD6KP4anqxvUbs5Rz9PMgBRSvqjOTAZDZD")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "957366660803516")
WA_LANG_CODE = "es_MX"

# Plantilla de bienvenida (con botón)
WELCOME_TEMPLATE_NAME = "bienvenida"

# Texto exacto del botón que indica "sí quiero"
# Cambia según el texto REAL del botón que pusiste en la plantilla aprobada
YES_BUTTON_TEXT = "Sí, quiero alertas"          # ← CAMBIA ESTO al texto exacto del botón
YES_VARIANTS = ["sí quiero", "suscribir", "si quiero", "quiero", "sí", "acepto"]

# Textos para opt-out
STOP_VARIANTS = ["stop", "baja","STOP", "cancelar", "No gracias", "desuscribir", "detener"]

# Ruta de tu base de datos de opt-ins (misma que usas en el cron)
OPTINS_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whatsapp_optins.db")

# Token de verificación para el webhook (lo configuras en Meta)
VERIFY_TOKEN = "AKCOOL_VERIFY_2026"  # ← CAMBIA ESTO por algo seguro

# ────────────────────────────────────────────────
# FUNCIONES DE BASE DE DATOS (opt-ins)
# ────────────────────────────────────────────────

def optins_db_connect():
    conn = sqlite3.connect(OPTINS_DB_PATH, timeout=15)
    conn.execute("PRAGMA busy_timeout=15000;")
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def has_active_optin(phone_clean: str) -> bool:
    conn = optins_db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM whatsapp_optins WHERE phone_number = ?", (phone_clean,))
        row = cur.fetchone()
        return row is not None and row[0] == 'active'
    except:
        return False
    finally:
        conn.close()

def register_optin(phone_clean: str, channel: str = "whatsapp_button"):
    conn = optins_db_connect()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO whatsapp_optins 
            (phone_number, optin_timestamp, channel, status, last_updated)
            VALUES (?, datetime('now'), ?, 'active', datetime('now'))
        """, (phone_clean, channel))
        conn.commit()
        print(f"[OPT-IN] Registrado: +{phone_clean}")
    except Exception as e:
        print(f"Error registrando opt-in: {e}")
    finally:
        conn.close()

def cancel_optin(phone_clean: str):
    conn = optins_db_connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE whatsapp_optins SET status = 'cancelled', last_updated = datetime('now') WHERE phone_number = ?", (phone_clean,))
        if cur.rowcount > 0:
            conn.commit()
            print(f"[OPT-OUT] Cancelado: +{phone_clean}")
    except Exception as e:
        print(f"Error en opt-out: {e}")
    finally:
        conn.close()

# ────────────────────────────────────────────────
# ENVIAR MENSAJE DE PLANTILLA
# ────────────────────────────────────────────────
def send_template(to_number_clean: str, template_name: str, params: list = None):
    if params is None:
        params = []

    url = f"https://graph.facebook.com/v22.0/{WA_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WA_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number_clean,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": WA_LANG_CODE},
            "components": []
        }
    }

    if params:
        payload["template"]["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in params]
        }]

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        if r.status_code == 200:
            print(f"[ENVIADO] {template_name} → {to_number_clean}")
        else:
            print(f"[ERROR] {template_name} → {to_number_clean} | {r.status_code} {r.text}")
    except Exception as e:
        print(f"[REQUEST ERROR] {to_number_clean}: {e}")

# ────────────────────────────────────────────────
# WEBHOOK PRINCIPAL
# ────────────────────────────────────────────────
@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        # Verificación inicial de Meta
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')

        if mode == 'subscribe' and token == VERIFY_TOKEN:
            return challenge, 200
        return "Token inválido", 403

    # Mensaje entrante
    if request.method == 'POST':
        try:
            data = request.get_json()
            if not data or 'entry' not in data:
                return jsonify({"status": "ok"}), 200

            entry = data['entry'][0]
            if 'changes' not in entry or not entry['changes']:
                return jsonify({"status": "ok"}), 200

            change = entry['changes'][0]
            if 'value' not in change or 'messages' not in change['value']:
                return jsonify({"status": "ok"}), 200

            message = change['value']['messages'][0]
            from_phone = message['from']  # 521xxxxxxxxxx
            phone_clean = from_phone.lstrip('+')

            # ─── 1. Si ya tiene opt-in → no enviar bienvenida otra vez ───
            if has_active_optin(phone_clean):
                print(f"[SKIP] {from_phone} ya tiene opt-in activo")
                return jsonify({"status": "ok"}), 200

            # ─── 2. Detectar respuesta al botón / texto de consentimiento ───
            if 'text' in message:
                text = message['text']['body'].strip().lower()

                # Usuario dice "sí quiero" o toca el botón
                if any(variant in text for variant in YES_VARIANTS):
                    register_optin(phone_clean, channel="boton_bienvenida")
                    # Enviar confirmación (puedes crear otra plantilla o usar texto libre si estás dentro de 24h)
                    send_template(phone_clean, "confirmacion_suscripcion", ["¡Listo! Ahora recibirás alertas de temperatura."])
                    return jsonify({"status": "ok"}), 200

                # Usuario dice STOP / baja
                elif any(variant in text for variant in STOP_VARIANTS):
                    cancel_optin(phone_clean)
                    send_template(phone_clean, "confirmacion_baja", ["Has cancelado las alertas. Si cambias de opinión, escribe 'hola'."])
                    return jsonify({"status": "ok"}), 200

            # ─── 3. Cualquier otro mensaje inicial → enviar bienvenida ───
            print(f"[NUEVO CHAT] Enviando bienvenida a {from_phone}")
            send_template(phone_clean, WELCOME_TEMPLATE_NAME)

            return jsonify({"status": "ok"}), 200

        except Exception as e:
            print(f"Error en webhook: {e}")
            return jsonify({"status": "error"}), 500

    return jsonify({"status": "method not allowed"}), 405

if __name__ == '__main__':
    # Para desarrollo local
    app.run(host='0.0.0.0', port=5000, debug=True)

    # Para producción (ej: Render, Railway, Heroku):
    # if __name__ == '__main__':
    #     port = int(os.environ.get('PORT', 5000))
    #     app.run(host='0.0.0.0', port=port)