from flask import Flask, request, jsonify
import os
import json
import sqlite3
import requests
from datetime import datetime

app = Flask(__name__)

# ────────────────────────────────────────────────
# CONFIGURACIÓN (ajusta estos valores)
WA_ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "EAAeVR8IGTpgBQt33hL3tzElaxOA9mKxihsGWNUYwCWkmVYGRVxeVqZCw1ZCXuzrxZAx1dX94PBT5gRPT8B4k6tlO3vLr6dIaZCNFdPZAUczqBoY5jkrMHJQpmVyqxYHR4i313IEzuh5ZC5EtgnL1ZA7frqbfJcchcLPOD6KP4anqxvUbs5Rz9PMgBRSvqjOTAZDZD")
WA_PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "957366660803516")
WA_LANG_CODE = "es_MX"

# Plantilla de bienvenida (con botón)
WELCOME_TEMPLATE_NAME = "bienvenida"

# Texto del botón o variantes de "sí quiero"
YES_VARIANTS = ["sí quiero", "suscribir", "si quiero", "quiero", "sí", "acepto"]  # ← AJUSTA AL TEXTO EXACTO DEL BOTÓN

# Variantes para opt-out
STOP_VARIANTS = ["stop", "baja", "cancelar", "no quiero", "desuscribir", "detener"]

# Ruta de tu base de datos de opt-ins
OPTINS_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whatsapp_optins.db")

# Token de verificación (el mismo que pondrás en Meta)
VERIFY_TOKEN = "AKCOOL_VERIFY_2026"  # ← ESTE ES EL QUE USAS, MANTENLO IGUAL

# ────────────────────────────────────────────────
# FUNCIONES DE BASE DE DATOS
# ────────────────────────────────────────────────

def optins_db_connect():
    conn = sqlite3.connect(OPTINS_DB_PATH, timeout=15)
    conn.execute("PRAGMA busy_timeout=15000;")
    return conn

def has_active_optin(phone_clean: str) -> bool:
    conn = optins_db_connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM whatsapp_optins WHERE phone_number = ?", (phone_clean,))
        row = cur.fetchone()
        return row is not None and row[0] == 'active'
    except Exception as e:
        print(f"Error verificando opt-in: {e}")
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
# ENVIAR PLANTILLA
# ────────────────────────────────────────────────

def send_template(to_number_clean: str, template_name: str, params: list = None):
    if params is None:
        params = []

    url = f"https://graph.facebook.com/v22.0/{WA_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}", "Content-Type": "application/json"}
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
# WEBHOOK EN RAÍZ (/) PARA QUE RENDER Y META LO ENCUENTREN
# ────────────────────────────────────────────────

@app.route('/', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        # Verificación de Meta
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')

        if mode == 'subscribe' and token == VERIFY_TOKEN:
            print(f"[VERIFICACIÓN] Meta verificó correctamente con challenge: {challenge}")
            return challenge, 200
        else:
            print(f"[VERIFICACIÓN FALLIDA] Token recibido: {token} (esperado: {VERIFY_TOKEN})")
            return "Token inválido", 403

    if request.method == 'POST':
        try:
            data = request.get_json()
            if not data or 'entry' not in data:
                print("[POST] No hay entry en el payload")
                return jsonify({"status": "ok"}), 200

            entry = data['entry'][0]
            if 'changes' not in entry or not entry['changes']:
                print("[POST] No hay changes")
                return jsonify({"status": "ok"}), 200

            change = entry['changes'][0]
            if 'value' not in change or 'messages' not in change['value']:
                print("[POST] No hay messages")
                return jsonify({"status": "ok"}), 200

            message = change['value']['messages'][0]
            from_phone = message['from']
            phone_clean = from_phone.lstrip('+')

            print(f"[MENSAJE RECIBIDO] De: +{from_phone}")

            # Si ya tiene opt-in → no enviar bienvenida
            if has_active_optin(phone_clean):
                print(f"[SKIP] +{from_phone} ya tiene opt-in")
                return jsonify({"status": "ok"}), 200

            # Detectar respuesta al botón o texto
            if 'text' in message:
                text = message['text']['body'].strip().lower()
                print(f"[TEXTO RECIBIDO] '{text}'")

                # Consentimiento
                if any(v in text for v in YES_VARIANTS):
                    register_optin(phone_clean)
                    send_template(phone_clean, "confirmacion_suscripcion", ["¡Listo! Ahora recibirás alertas de temperatura."])
                    return jsonify({"status": "ok"}), 200

                # Opt-out
                elif any(v in text for v in STOP_VARIANTS):
                    cancel_optin(phone_clean)
                    send_template(phone_clean, "confirmacion_baja", ["Has cancelado las alertas. Si cambias de opinión escribe 'hola'."])
                    return jsonify({"status": "ok"}), 200

            # Mensaje inicial → enviar bienvenida
            print(f"[NUEVO CHAT] Enviando bienvenida a +{from_phone}")
            send_template(phone_clean, WELCOME_TEMPLATE)

            return jsonify({"status": "ok"}), 200

        except Exception as e:
            print(f"[ERROR EN WEBHOOK] {e}")
            return jsonify({"status": "error"}), 500

    return jsonify({"status": "method not allowed"}), 405

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
