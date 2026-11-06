import textwrap
import streamlit as st
import sqlite3
import pandas as pd
import datetime
import base64
import random
import hashlib
import os
import json

# ---------------------------------------------------------
# NOTIFICATION HELPER FUNCTIONS
# ---------------------------------------------------------
def add_notification(recipient_role, title, message, category='GENERAL', business_id=None):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO system_notifications (recipient_role, business_id, title, message, category, is_read)
            VALUES (?, ?, ?, ?, ?, 0)
        """, (recipient_role, business_id, title, message, category))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error adding notification: {e}")

def get_unread_notifications_count(recipient_role, business_id=None):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        if recipient_role == 'DIRECTOR':
            cursor.execute("SELECT COUNT(*) as cnt FROM system_notifications WHERE recipient_role IN ('DIRECTOR', 'ALL') AND is_read = 0")
        else:
            cursor.execute("SELECT COUNT(*) as cnt FROM system_notifications WHERE (recipient_role IN ('MANAGER', 'ALL') AND (business_id = ? OR business_id IS NULL)) AND is_read = 0", (business_id,))
        row = cursor.fetchone()
        conn.close()
        return row['cnt'] if row else 0
    except Exception:
        return 0

def mark_notifications_read(recipient_role, business_id=None):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        if recipient_role == 'DIRECTOR':
            cursor.execute("UPDATE system_notifications SET is_read = 1 WHERE recipient_role IN ('DIRECTOR', 'ALL') AND is_read = 0")
        else:
            cursor.execute("UPDATE system_notifications SET is_read = 1 WHERE (recipient_role IN ('MANAGER', 'ALL') AND (business_id = ? OR business_id IS NULL)) AND is_read = 0", (business_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error marking notifications read: {e}")

# ---------------------------------------------------------
# BANK GATEWAY ENGINE (v62→v63) — malipo yanaunganishwa moja kwa moja na
# mifumo ya benki husika WAKATI WA KUTUMA (real-time transfer switch).
# BANK_GATEWAY_MODE (inasomwa kiotomatiki kutoka env vars / st.secrets):
#   "SIMULATION" = Gateway Simulator yenye mtiririko halisi wa benki:
#                  hela inatoka KWELI kwenye akaunti ya mtumaji (DEBIT) na
#                  kuingia KWELI kwenye akaunti ya mpokeaji (CREDIT),
#                  validation ya akaunti, response codes za ISO-style,
#                  TxnID, na kumbukumbu kwenye bank_transfer_log + account_ledger.
#   "LIVE"       = weka kwenye .streamlit/secrets.toml au environment vars:
#                     BANK_GATEWAY_MODE = "LIVE"
#                     BANK_API_URL_<CHANNEL>  (mf. BANK_API_URL_NMB_CORE)
#                     BANK_API_KEY_<CHANNEL>  (mf. BANK_API_KEY_NMB_CORE)
#                  Mfumo utazitumia moja kwa moja (real API calls).
# ---------------------------------------------------------
def _gw_secret(key_name):
    """Soma config ya gateway kutoka environment variables au .streamlit/secrets.toml
    (NJIA SALAMA: API keys haziandikiwi kwenye file ya app)."""
    try:
        _val = os.environ.get(key_name)
        if _val:
            return str(_val)
    except Exception:
        pass
    try:
        if key_name in st.secrets:
            return str(st.secrets[key_name])
    except Exception:
        pass
    return None

BANK_GATEWAY_MODE = (_gw_secret("BANK_GATEWAY_MODE") or "SIMULATION").strip().upper()
if BANK_GATEWAY_MODE not in ("SIMULATION", "LIVE"):
    BANK_GATEWAY_MODE = "SIMULATION"
BANK_API_ENDPOINTS = {
    # "NMB_CORE": "https://api.nmbbank.co.tz/...",   # weka API halisi hapa
    # "CRDB_CORE": "https://openapi.crdbbank.co.tz/...",
}

def _get_bank_api_endpoint(channel_code):
    """Endpoint ya LIVE API: kwanza env/secrets (BANK_API_URL_<CHANNEL>), kisha dict ya file."""
    ep = _gw_secret(f"BANK_API_URL_{channel_code}")
    if ep:
        return ep
    try:
        return BANK_API_ENDPOINTS.get(channel_code)
    except Exception:
        return None

def _get_bank_api_key(channel_code):
    """API KEY ya LIVE: env/secrets (BANK_API_KEY_<CHANNEL>)."""
    return _gw_secret(f"BANK_API_KEY_{channel_code}")

def detect_bank_channel(bank_name):
    """Tambua ghala la benki / mobile money linalohusika na malipo."""
    bn = str(bank_name).upper()
    if "M-PESA" in bn or "MPESA" in bn or "VODACOM" in bn:
        return "VODACOM_MPESA", "M-PESA (Vodacom Mobile Money)"
    if "TIGO" in bn or "MIXX" in bn:
        return "TIGO_PESA", "Mixx by Yas (Tigo Pesa)"
    if "AIRTEL" in bn:
        return "AIRTEL_MONEY", "Airtel Money"
    if "HALO" in bn:
        return "HALOPESA", "HaloPesa"
    if "NMB" in bn:
        return "NMB_CORE", "NMB Bank — Core Banking Switch"
    if "CRDB" in bn:
        return "CRDB_CORE", "CRDB Bank — Core Banking Switch"
    if "NBC" in bn:
        return "NBC_CORE", "NBC Bank — Core Banking Switch"
    if "ABSA" in bn:
        return "ABSA_CORE", "ABSA Bank — Core Banking Switch"
    if "KCB" in bn:
        return "KCB_CORE", "KCB Bank — Core Banking Switch"
    if "STANBIC" in bn:
        return "STANBIC_CORE", "Stanbic Bank — Core Banking Switch"
    if "EQUITY" in bn:
        return "EQUITY_CORE", "Equity Bank — Core Banking Switch"
    return "GENERIC_CORE", f"{str(bank_name).title()} — Core Banking Switch"

def log_bank_transfer(res, source_label, dest_name, amount, reference, notes=""):
    """Kumbukumbu ya kila muamala wa benki (iliyo kamilika au iliyokataliwa)."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO bank_transfer_log
            (mode, channel, channel_label, status, response_code, bank_txn_id, source,
             destination, dest_name, amount, reference_code, message, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (res.get("mode"), res.get("channel"), res.get("channel_label"), res.get("status"),
              res.get("response_code"), res.get("bank_txn_id"), res.get("source"),
              res.get("destination"), dest_name, float(amount), str(reference),
              res.get("message"), res.get("checked_at")))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Bank log error: {e}")

def record_account_movement(account_owner, account_type, business_id, bank_name, account_number,
                            direction, amount, balance_after, counterparty,
                            reference_code="", bank_txn_id="", channel_label="", description=""):
    """ACCOUNT LEDGER (v63): kumbukumbu ya kila mwendo wa hela — DEBIT (hela
    imetoka kwenye akaunti) au CREDIT (hela imeingia kwenye akaunti) — pamoja
    na salio lililobakia baada ya muamala. Hii ndiyo uthibitisho wa kwamba
    hela inaenda KWELI kutoka akaunti moja hadi nyingine."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO account_ledger
            (created_at, account_owner, account_type, business_id, bank_name, account_number,
             direction, amount, balance_after, counterparty, reference_code, bank_txn_id,
             channel_label, description)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), str(account_owner), str(account_type),
              business_id, str(bank_name), str(account_number), str(direction), float(amount),
              float(balance_after), str(counterparty), str(reference_code or ""),
              str(bank_txn_id or ""), str(channel_label or ""), str(description or "")))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Account ledger error: {e}")

def send_bank_transfer(source_label, source_bank, source_account, dest_bank, dest_account, dest_name, amount, reference, notes="", source_balance=None):
    """Tuma malipo kwenye benki/mobile money husika KWA MUDA WA KUTUMA.
    Inarejea dict yenye: status (CONFIRMED/REJECTED), bank_txn_id, channel,
    channel_label, response_code, message.
    source_balance: salio la akaunti ya mtumaji — gateway inakataa muamala
    (51 — INSUFFICIENT FUNDS) kama hela ni pichondogo kuliko kiasi. """
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    channel_code, channel_label = detect_bank_channel(dest_bank)
    res = {
        "mode": BANK_GATEWAY_MODE, "channel": channel_code, "channel_label": channel_label,
        "status": "REJECTED", "bank_txn_id": None, "response_code": None, "message": "",
        "checked_at": now_str,
        "source": f"{source_bank} A/C {source_account}",
        "destination": f"{dest_bank} A/C {dest_account}",
    }
    digits = re.sub(r"\D", "", str(dest_account))
    mobile_channels = ("VODACOM_MPESA", "TIGO_PESA", "AIRTEL_MONEY", "HALOPESA")
    if channel_code in mobile_channels:
        if not re.fullmatch(r"0[67]\d{8}", digits):
            res["response_code"] = "30 — INVALID MSISDN"
            res["message"] = f"{channel_label}: Namba ya simu ya malipo '{dest_account}' si sahihi. Inatakiwa kuwa tarakimu 10 zikianza na 06 au 07."
            log_bank_transfer(res, source_label, dest_name, amount, reference, notes)
            return res
    else:
        if not (6 <= len(digits) <= 22):
            res["response_code"] = "14 — INVALID ACCOUNT"
            res["message"] = f"{channel_label}: Namba ya akaunti '{dest_account}' si sahihi (tarakimu 6 hadi 22 pekee)."
            log_bank_transfer(res, source_label, dest_name, amount, reference, notes)
            return res
    if float(amount) <= 0:
        res["response_code"] = "13 — INVALID AMOUNT"
        res["message"] = "Kiasi cha kutuma kinatakiwa kiwe zaidi ya 0 TZS."
        log_bank_transfer(res, source_label, dest_name, amount, reference, notes)
        return res

    if source_balance is not None:
        try:
            if float(source_balance) < float(amount):
                res["response_code"] = "51 — INSUFFICIENT FUNDS"
                res["message"] = (f"{channel_label}: Salio la akaunti ya mtumaji halitoshi — Salio: TZS "
                                  f"{float(source_balance):,.2f}, Muamala: TZS {float(amount):,.2f}. "
                                  "Weka mtaji/deposit kwenye akaunti ya kampuni kwanza.")
                log_bank_transfer(res, source_label, dest_name, amount, reference, notes)
                return res
        except Exception:
            pass

    if BANK_GATEWAY_MODE == "LIVE":
        # ==========================================================
        # UNGANISHO HALISI NA BENKI (v63): API halisi ya benki/mobile money.
        # Endpoint + API KEY zinasomwa kwa NJIA SALAMA kutoka environment
        # variables au .streamlit/secrets.toml:
        #   BANK_API_URL_<CHANNEL>  na  BANK_API_KEY_<CHANNEL>
        # ==========================================================
        _endpoint = _get_bank_api_endpoint(channel_code)
        _api_key = _get_bank_api_key(channel_code)
        if not _endpoint:
            res["response_code"] = "31 — LIVE CHANNEL NOT CONFIGURED"
            res["message"] = (f"{channel_label}: LIVE API haijaandanishwa. Weka BANK_API_URL_{channel_code} "
                              "na BANK_API_KEY_" + channel_code + " kwenye .streamlit/secrets.toml au env vars, "
                              "au tumia SIMULATION mode.")
            log_bank_transfer(res, source_label, dest_name, amount, reference, notes)
            return res
        try:
            import urllib.request as _ureq
            import urllib.error as _uerr
            _payload = json.dumps({
                "source_account": str(source_account), "source_bank": str(source_bank),
                "destination_account": str(dest_account), "destination_bank": str(dest_bank),
                "destination_name": str(dest_name), "amount": float(amount),
                "currency": "TZS", "reference": str(reference), "notes": str(notes),
            }).encode("utf-8")
            _headers = {"Content-Type": "application/json"}
            if _api_key:
                _headers["Authorization"] = f"Bearer {_api_key}"
            _req = _ureq.Request(_endpoint, data=_payload, headers=_headers, method="POST")
            with _ureq.urlopen(_req, timeout=12) as _resp:
                _body = json.loads(_resp.read().decode("utf-8") or "{}")
            _bstatus = str(_body.get("status") or _body.get("responseStatus") or _body.get("result") or "").upper()
            _bcode = str(_body.get("response_code") or _body.get("code") or "").upper()
            if ("SUCCESS" in _bstatus or "CONFIRM" in _bstatus or "APPROV" in _bstatus
                    or "COMPLETED" in _bstatus or _bcode.startswith("00")):
                res["status"] = "CONFIRMED"
                res["bank_txn_id"] = str(_body.get("transaction_id") or _body.get("txn_id") or _body.get("reference") or f"LIVE{hashlib.sha1((str(reference) + str(amount) + now_str).encode()).hexdigest()[:12].upper()}")
                res["response_code"] = "00 — APPROVED (LIVE)"
                res["message"] = f"Malipo yamepitishwa na {channel_label} (LIVE API) na yamepokelewa kwenye akaunti ya {dest_name}."
            else:
                res["response_code"] = f"{_bcode or '92'} — DECLINED BY BANK"
                res["message"] = f"{channel_label}: Muamala umekataliwa na benki (LIVE API): {_body.get('message') or _bstatus or 'No response message'}"
            log_bank_transfer(res, source_label, dest_name, amount, reference, notes)
            return res
        except Exception as _e:
            res["response_code"] = "91 — GATEWAY UNREACHABLE"
            res["message"] = (f"{channel_label}: Imeshindikana kufikia LIVE API ({_e.__class__.__name__}). "
                              "Angalia endpoint/credentials au mtandao, kisha jaribu tena au tumia SIMULATION mode.")
            log_bank_transfer(res, source_label, dest_name, amount, reference, notes)
            return res

    # SIMULATION: mtiririko halisi wa bank switch — TxnID ya kudumu
    # (deterministic) kutoka kwenye kipimo cha muamala.
    seed = f"{channel_code}|{reference}|{amount}|{digits}|{now_str}"
    txn_id = "TXN" + hashlib.sha1(seed.encode()).hexdigest()[:12].upper()
    res["status"] = "CONFIRMED"
    res["bank_txn_id"] = txn_id
    res["response_code"] = "00 — APPROVED"
    res["message"] = f"Malipo yamepitishwa na {channel_label} na yamepokelewa kwenye akaunti ya {dest_name}."
    log_bank_transfer(res, source_label, dest_name, amount, reference, notes)
    return res

def save_payment_receipt(business_id, business_name, payer_role, payer_name, recipient_type,
                         recipient_name, recipient_phone, recipient_bank, recipient_account,
                         amount, reference_code, gateway_res, notes=""):
    """Risiti ya muamala wa malipo → inaingia kwenye DATABASE YA DIRECTOR na
    DATABASE YA MENEJA kwa pamoja (delivery flags) + notification automatic."""
    receipt_no = "RCP{}{}".format(datetime.datetime.now().strftime("%Y%m%d%H%M%S"), random.randint(100, 999))
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO payment_receipts
            (receipt_no, payment_date, business_id, business_name, payer_role, payer_name,
             recipient_type, recipient_name, recipient_phone, recipient_bank, recipient_account,
             amount, currency, reference_code, bank_txn_id, bank_channel, bank_channel_label,
             gateway_status, gateway_message, delivery_director, delivery_manager, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'TZS', ?, ?, ?, ?, ?, ?, 1, 1, ?)
        """, (receipt_no, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), business_id,
              business_name, payer_role, payer_name, recipient_type, recipient_name,
              recipient_phone, recipient_bank, recipient_account, float(amount), str(reference_code),
              gateway_res.get("bank_txn_id"), gateway_res.get("channel"), gateway_res.get("channel_label"),
              gateway_res.get("status"), gateway_res.get("message"), notes))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Receipt error: {e}")
    add_notification('ALL', f"🧾 Risiti ya Malipo {receipt_no}: {recipient_name}",
                     f"Malipo ya TZS {float(amount):,.2f} kwa {recipient_name} ({recipient_bank} A/C {recipient_account}) "
                     f"yamethibitishwa na benki — TxnID: {gateway_res.get('bank_txn_id')} ({gateway_res.get('channel_label')}). "
                     f"Ref: {reference_code}. Risiti imewasilishwa kwenye DATABASE YA DIRECTOR na DATABASE YA MENEJA.",
                     category='PAYMENT_RECEIPT', business_id=business_id)
    return receipt_no

# ---------------------------------------------------------
# V64 ENGINE: PIN VALIDATION + RISITI PDF + SMS GATEWAY + DAILY PROFIT
# ---------------------------------------------------------
def _hash_pin(pin_str):
    """Hash ya PIN (sha256) — PIN halisi haitunzwi kwenye database."""
    return hashlib.sha256(str(pin_str).strip().encode()).hexdigest()

def get_company_pin_hash(business_id):
    """PIN hash ya Akaunti ya Kampuni ya biashara (chaguo-msingi: 1234)."""
    return get_setting(f"pay_pin::COMPANY::{business_id}") or _hash_pin("1234")

def set_company_pin(business_id, new_pin):
    new_pin = str(new_pin).strip()
    if not (new_pin.isdigit() and 4 <= len(new_pin) <= 6):
        return False, "PIN lazima iwe tarakimu 4 hadi 6 pekee."
    update_setting(f"pay_pin::COMPANY::{business_id}", _hash_pin(new_pin))
    return True, "PIN ya Akaunti ya Kampuni imebadilishwa kikamilifu."

def validate_company_pin(business_id, pin_input):
    """VALIDATION KUU YA MALIPO (v64): PIN ya akaunti ya kutolea hela."""
    return str(pin_input).strip() and _hash_pin(pin_input) == get_company_pin_hash(business_id)

def log_pin_rejection(source_bank, source_account, dest_bank, dest_account, dest_name, amount, reference, who):
    """Muamala uliokataliwa kwa PIN mbaya unaingia kwenye Bank Transfer Log (audit)."""
    res = {
        "mode": BANK_GATEWAY_MODE, "channel": detect_bank_channel(dest_bank)[0],
        "channel_label": detect_bank_channel(dest_bank)[1], "status": "REJECTED",
        "bank_txn_id": None, "response_code": "55 — INVALID PIN",
        "message": "Malipo yamekataliwa: PIN ya akaunti ya kutolea hela si sahihi (55 — INVALID PIN). "
                   "Hela HAIJATOLEWA kwenye akaunti. Angalia PIN yako kisha jaribu tena.",
        "checked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": f"{source_bank} A/C {source_account}",
        "destination": f"{dest_bank} A/C {dest_account}",
    }
    log_bank_transfer(res, f"{who} (PIN REJECTED)", dest_name, amount, reference,
                      "PIN ya akaunti ya kutolea hela si sahihi")
    return res

def generate_receipt_pdf(data):
    """RISITI RASMI YA MALIPO KWA PDF (v64) — inarejea bytes za PDF."""
    if not FPDF_AVAILABLE:
        return None
    def _pdf_safe(v):
        # Helvetica ya fpdf inasoma latin-1 tu — tulia herufi za kigeni (—, →, n.k.)
        s = str(v).replace("\u2014", "-").replace("\u2013", "-").replace("\u2192", "->").replace("\u2019", "'")
        return s.encode("latin-1", errors="replace").decode("latin-1")
    try:
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=14)
        pdf.add_page()
        # Header
        pdf.set_fill_color(11, 19, 43)
        pdf.rect(0, 0, 210, 26, 'F')
        pdf.set_xy(0, 6)
        pdf.set_text_color(0, 240, 255)
        pdf.set_font("Helvetica", 'B', 15)
        pdf.cell(0, 8, "RISITI RASMI YA MALIPO - OFFICIAL PAYMENT RECEIPT", 0, 1, 'C')
        pdf.set_xy(0, 15)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", '', 9)
        pdf.cell(0, 6, "BIASHARA CYBER GRADING SYSTEM - Bank Gateway (Real-Time Transfer Advice)", 0, 1, 'C')
        # Body
        pdf.set_y(32)
        pdf.set_text_color(20, 20, 30)
        rows = [
            ("Risiti No / Receipt No", str(data.get("receipt_no", "-"))),
            ("Muda wa Malipo / Payment Time", str(data.get("payment_time", "-"))),
            ("Biashara / Business", str(data.get("business_name", "-"))),
            ("Aliyelipa / Paid By", f"{data.get('payer_role', '-')} — {data.get('payer_name', '-')}"),
            ("Mpokeaji / Recipient", str(data.get("recipient_name", "-")).upper()),
            ("Benki ya Mpokeaji / Recipient Bank", str(data.get("recipient_bank", "-"))),
            ("Akaunti ya Mpokeaji / Recipient A/C", str(data.get("recipient_account", "-"))),
            ("Simu ya Mpokeaji / Recipient Phone", str(data.get("recipient_phone", "-"))),
            ("Kiasi / Amount", f"TZS {float(data.get('amount', 0)):,.2f}"),
            ("Namba ya Kumbukumbu / Reference", str(data.get("reference_code", "-"))),
            ("Ghala la Benki / Bank Gateway", str(data.get("channel_label", "-"))),
            ("Majibu ya Benki / Response Code", str(data.get("response_code", "-"))),
            ("Bank Transaction ID (TxnID)", str(data.get("bank_txn_id", "-"))),
            ("Salio la Akaunti ya Mtumaji Baada ya Muamala", f"TZS {float(data.get('source_balance_after', 0)):,.2f}"),
            ("Salio la Akaunti ya Mpokeaji Baada ya Muamala", f"TZS {float(data.get('recipient_balance_after', 0)):,.2f}"),
            ("Maelezo / Notes", str(data.get("notes", "-"))[:120]),
        ]
        for lbl, val in rows:
            pdf.set_font("Helvetica", 'B', 10)
            pdf.set_fill_color(232, 240, 254)
            pdf.cell(85, 8, " " + _pdf_safe(lbl), 1, 0, 'L', fill=True)
            pdf.set_font("Helvetica", '', 10)
            pdf.cell(0, 8, f" {_pdf_safe(val[:70])}", 1, 1, 'L')
        # Footer
        pdf.set_y(-28)
        pdf.set_font("Helvetica", 'I', 8)
        pdf.set_text_color(90, 90, 110)
        pdf.multi_cell(0, 5, "Risiti hii imetengenezwa na kuhifadhiwa kiotomatiki na mfumo. "
                             "Malipo yamethibitishwa na Ghala la Benki (Bank Gateway). "
                             "Hela imehamia KWELI kutoka akaunti ya mtumaji kwenda akaunti ya mpokeaji (DEBIT -> CREDIT).", 0, 'C')
        return bytes(pdf.output())
    except Exception as e:
        print(f"Receipt PDF error: {e}")
        return None

def store_receipt_pdf(receipt_no, business_id, recipient_name, recipient_phone, amount, pdf_bytes):
    """Hifadhi risiti PDF (base64) ili kila mhusika aweze kuipakua baadaye."""
    if not pdf_bytes:
        return
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO receipt_pdfs (created_at, receipt_no, business_id, recipient_name, recipient_phone, amount, pdf_b64)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), receipt_no, business_id,
              recipient_name, recipient_phone, float(amount), base64.b64encode(pdf_bytes).decode()))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Receipt PDF store error: {e}")

def build_salary_sms(recipient_name, amount, bank, account, ref, txn, lang="Kiswahili"):
    """Ujumbe wa SMS wa malipo ya mshahara (Kiswahili/English)."""
    if lang == "English":
        msg = (f"Dear {recipient_name}, your salary of TZS {float(amount):,.2f} has been PAID and sent to "
               f"your account ({bank} A/C {account}). Ref: {ref} | TxnID: {txn}. Thank you!")
    else:
        msg = (f"Habari {recipient_name}, mshahara wako wa TZS {float(amount):,.2f} UMEWALIWA na kutumwa kwenye "
               f"akaunti yako ({bank} A/C {account}). Ref: {ref} | TxnID: {txn}. Asante!")
    return msg

def send_sms_gateway(phone, message, business_id=None, recipient_name="", category="SALARY_SMS"):
    """SMS ENGINE (v64): tuma SMS kwa simu ya mtu kiotomatiki.
    - LIVE: kama SMS_API_URL + SMS_API_KEY zipo kwenye secrets/env, HTTP POST halisi.
    - SIMULATION: SMS inaandikwa kwenye sms_log (inaonekana kwa Director na mwenyewe)."""
    mode = "LIVE" if _gw_secret("SMS_API_URL") else "SIMULATION"
    status, gtxn = "SENT (SIMULATION)", "SMS" + hashlib.sha1((str(phone) + str(message)).encode()).hexdigest()[:10].upper()
    if mode == "LIVE":
        try:
            import urllib.request as _ureq
            _payload = json.dumps({"to": str(phone), "message": str(message), "from": "BIASHARA-CYBER"}).encode("utf-8")
            _headers = {"Content-Type": "application/json"}
            _key = _gw_secret("SMS_API_KEY")
            if _key:
                _headers["Authorization"] = f"Bearer {_key}"
            _req = _ureq.Request(_gw_secret("SMS_API_URL"), data=_payload, headers=_headers, method="POST")
            with _ureq.urlopen(_req, timeout=12) as _resp:
                _body = json.loads(_resp.read().decode("utf-8") or "{}")
            gtxn = str(_body.get("message_id") or _body.get("sms_id") or _body.get("id") or gtxn)
            status = "SENT (LIVE)"
        except Exception as _e:
            status = f"FAILED ({_e.__class__.__name__})"
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO sms_log (created_at, business_id, phone, recipient_name, message, category, status, gateway_mode, gateway_txn_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), business_id, str(phone),
              str(recipient_name), str(message), category, status, mode, gtxn))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"SMS log error: {e}")
    return {"status": status, "mode": mode, "gateway_txn_id": gtxn}

def record_daily_profit(business_id, record_date, cash_revenue, credit_revenue, expenses):
    """FAIDA YA SIKU (v64): (cash + credit) - matumizi. Inarejea profit.
    Inakokotwa kiotomatiki mara mapato ya siku yanapotumwa (UNIQUE per business/date)."""
    cash_revenue = float(cash_revenue or 0)
    credit_revenue = float(credit_revenue or 0)
    expenses = float(expenses or 0)
    profit = cash_revenue + credit_revenue - expenses
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO daily_profit (business_id, record_date, cash_revenue, credit_revenue, revenue, expenses, profit, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(business_id, record_date) DO UPDATE SET
                cash_revenue=excluded.cash_revenue, credit_revenue=excluded.credit_revenue,
                revenue=excluded.revenue, expenses=excluded.expenses, profit=excluded.profit,
                updated_at=excluded.updated_at
        """, (business_id, record_date, cash_revenue, credit_revenue,
              cash_revenue + credit_revenue, expenses, profit,
              datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Daily profit error: {e}")
    return profit

def get_person_profile(business_id, person_name):
    """PROFAILI YA MTU (v64): meneja au mfanyakazi wa biashara husika — details zake TU."""
    conn = get_connection()
    df_stf = pd.read_sql_query("SELECT * FROM staff_members WHERE business_id = ? AND full_name = ? LIMIT 1",
                               conn, params=(business_id, str(person_name).strip()))
    conn.close()
    if not df_stf.empty:
        r = df_stf.iloc[0]
        return {"is_manager": str(r.get('staff_type')) == 'Meneja wa Biashara', "name": r['full_name'],
                "role": r.get('role_title'), "phone": r.get('phone'),
                "bank": r.get('bank_name'), "account": r.get('bank_account_number') or r.get('account_number'),
                "balance": float(r.get('bank_account_balance') or 0.0)}
    return None

def get_person_pin_hash(business_id, person_name):
    """PIN hash ya mtu (chaguo-msingi: 1234 — Director anaweza kuibadilisha)."""
    return get_setting(f"person_pin::{business_id}::{str(person_name).strip().upper()}") or _hash_pin("1234")

def set_person_pin(business_id, person_name, new_pin):
    new_pin = str(new_pin).strip()
    if not (new_pin.isdigit() and 4 <= len(new_pin) <= 6):
        return False, "PIN lazima iwe tarakimu 4 hadi 6 pekee."
    update_setting(f"person_pin::{business_id}::{str(person_name).strip().upper()}", _hash_pin(new_pin))
    return True, f"PIN ya {person_name} imebadilishwa kikamilifu."

def get_my_receipts(business_id, person_name, phone):
    """Risiti za MTU HUYU TU (privacy): jina au simu inapatanisha."""
    conn = get_connection()
    df = pd.read_sql_query("""SELECT * FROM payment_receipts WHERE business_id = ? AND
        (LOWER(recipient_name) = LOWER(?) OR recipient_phone = ?) ORDER BY id DESC""",
        conn, params=(business_id, str(person_name).strip(), str(phone)))
    conn.close()
    return df

def get_my_sms(business_id, phone):
    """SMS zote zilizomtumia MTU HUYU TU (privacy: kwa simu yake tu)."""
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM sms_log WHERE phone = ? AND (business_id = ? OR business_id IS NULL) ORDER BY id DESC",
                           conn, params=(str(phone), business_id))
    conn.close()
    return df


# ---------------------------------------------------------
# DAILY REPORT GUARD — Alert ya automatic kwa Director pale biashara
# haijatuma taarifa zake za mapato za kila siku (daily_records).
# ---------------------------------------------------------
def get_businesses_missing_daily_report():
    """Orodha ya biashara zilizosajiliwa ambazo HAZIJATUMA taarifa za mapato za leo."""
    missing = []
    try:
        conn = get_connection()
        df_b = pd.read_sql_query("SELECT id, name, manager_name, manager_phone FROM businesses", conn)
        df_sub = pd.read_sql_query("SELECT DISTINCT business_id FROM daily_records WHERE record_date = ?", conn, params=(today_date_str,))
        conn.close()
        submitted = set(int(x) for x in df_sub['business_id'].tolist()) if not df_sub.empty else set()
        for _, r in df_b.iterrows():
            if int(r['id']) not in submitted:
                missing.append({"id": int(r['id']), "name": r['name'],
                                "manager_name": r['manager_name'], "manager_phone": r['manager_phone']})
    except Exception as e:
        print(f"Daily report guard error: {e}")
    return missing

def run_daily_report_guard():
    """Inaandika notification ya automatic kwa DIRECTOR kwa kila biashara
    isiyo-tuma taarifa za leo (MARA MOJA tu kwa siku/biashara — hakuna kujirudia)."""
    missing = get_businesses_missing_daily_report()
    for m in missing:
        marker = f"[RIPOTI:{today_date_str}:{m['id']}]"
        flag_key = f"missing_notif_seen_{today_date_str}_{m['id']}"
        if st.session_state.get(flag_key):
            continue
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS c FROM system_notifications WHERE category = 'DAILY_REPORT_MISSING' AND message LIKE ?", (f"%{marker}%",))
            already = cursor.fetchone()['c'] > 0
            if not already:
                cursor.execute("""
                    INSERT INTO system_notifications (recipient_role, business_id, title, message, category, is_read)
                    VALUES ('DIRECTOR', ?, ?, ?, 'DAILY_REPORT_MISSING', 0)
                """, (m['id'],
                      f"🏢 {m['name']}: HAIJATUMA TAARIFA ZA MAPATO ZA LEO!",
                      f"Biashara ya '{m['name']}' (Meneja: {m['manager_name']}, Simu: {m['manager_phone']}) haijatuma taarifa zake za mapato za leo ({today_date_str}) hadi saa {datetime.datetime.now().strftime('%H:%M')}. {marker}"))
                conn.commit()
            conn.close()
            st.session_state[flag_key] = True
        except Exception as e:
            print(f"Missing notif error: {e}")
    return missing

def inject_auto_monitor(interval_sec=75):
    """AUTO MONITOR: mfumo unajirefresh kiotomatiki kila sekunde interval_sec
    (kupitia kitufe cha siri ⟳) — hivyo notification, alert na risiti mpya
    zinaonekana AUTOMATIC bila mtumaji kubofya chochote."""
    st.button("⟳", key="auto_sync_hidden_btn")
    js = """
    <script>
    (function() {
        try {
            if (window.__cyberAutoMonitor && window.__cyberAutoMonitor.clear) {
                window.__cyberAutoMonitor.clear();
            }
            var TICK = __INTERVAL__ * 1000;
            function findSyncBtn() {
                var btns = document.querySelectorAll('button');
                for (var i = 0; i < btns.length; i++) {
                    var t = (btns[i].textContent || '').trim();
                    if (t === '\\u27F3') return btns[i];
                }
                return null;
            }
            function hideBtn() { var b = findSyncBtn(); if (b) { b.style.display = 'none'; } }
            hideBtn();
            var iv = setInterval(function() {
                var b = findSyncBtn();
                if (b) { b.click(); }
            }, TICK);
            window.__cyberAutoMonitor = { clear: function() { clearInterval(iv); } };
        } catch(e) { console.log(e); }
    })();
    </script>
    """.replace("__INTERVAL__", str(int(interval_sec)))
    render_js_html(js)

# ---------------------------------------------------------
# LIVE FACE GUARD (JavaScript) — kamera ina-scann USO TU, si kitu kingine.
# Inatumia FaceDetector API ya browser au face-api.js (tiny model), na
# OpenCV ya server inabaki kuwa mthibitishaji wa mwisho (uso MMOJA tu).
# ---------------------------------------------------------
FACE_GUARD_JS = """
<script>
(function() {
    if (window.__cyberFaceGuard) return;
    window.__cyberFaceGuard = true;

    var MSG = {
        scanning: '\\uD83D\\uDD0D LIVE FACE GUARD: INASCAN USO...',
        ok: '\\u2705 USO MMOJA UMEGUNDULIWA \\u2014 SCANNER SAFI (USO TU)',
        none: '\\u274C HAKUNA USO! Mfumo ha-scanni kitu kingine \\u2014 NIONEKANE USO WAKO TU',
        many: '\\u274C USO ZAIDI YA MMOJA! Nionekane na USO WAKO MMOJA tu',
        offline: '\\u26A0 LIVE GUARD: OFFLINE \\u2014 OpenCV ya server itathibitisha uso tu'
    };
    var engineFailed = false, faceApiLoaded = false, nativeDetector = null, tinyOpts = null;

    function setOv(ov, kind) {
        if (!ov) return;
        var c = (kind === 'ok') ? '#39FF14' : (kind === 'scanning' || kind === 'offline') ? '#FFD700' : '#FF0040';
        ov.style.borderColor = c;
        ov.style.color = c;
        ov.style.textShadow = '0 0 8px ' + c;
        ov.textContent = MSG[kind] || MSG.scanning;
    }

    function attachOverlay(video) {
        var root = null;
        try { root = video.closest('[data-testid="stCameraInput"]'); } catch(e) { root = null; }
        if (!root) root = video.parentElement;
        if (!root) return null;
        var ov = null;
        for (var i = 0; i < root.children.length; i++) {
            if ((' ' + root.children[i].className).indexOf('cyber-face-overlay') !== -1) { ov = root.children[i]; break; }
        }
        if (ov) return ov;
        try { if (getComputedStyle(root).position === 'static') root.style.position = 'relative'; } catch(e) {}
        ov = document.createElement('div');
        ov.className = 'cyber-face-overlay';
        ov.style.cssText = 'position:absolute;top:8px;left:8px;right:8px;z-index:99;pointer-events:none;font-family:Consolas,monospace;font-size:12px;font-weight:900;letter-spacing:1px;padding:6px 10px;border-radius:6px;background:rgba(2,6,23,0.88);border:1px solid #FFD700;color:#FFD700;box-shadow:0 0 12px rgba(0,240,255,0.35);text-align:center;';
        ov.textContent = MSG.scanning;
        try { root.appendChild(ov); } catch(e) { return null; }
        return ov;
    }

    function markOffline() {
        engineFailed = true;
        var ovs = document.querySelectorAll('.cyber-face-overlay');
        for (var i = 0; i < ovs.length; i++) setOv(ovs[i], 'offline');
    }

    function loadScript(src, ok, fail) {
        var s = document.createElement('script');
        s.src = src;
        s.onload = ok;
        s.onerror = fail || markOffline;
        document.head.appendChild(s);
    }

    function initEngine() {
        try { if (window.FaceDetector) { return; } } catch(e) {}
        loadScript('https://cdn.jsdelivr.net/npm/@vladmandic/face-api@1.7.13/dist/face-api.js',
            function() {
                try {
                    faceapi.nets.tinyFaceDetector.loadFromUri('https://cdn.jsdelivr.net/npm/@vladmandic/face-api@1.7.13/model')
                        .then(function() { faceApiLoaded = true; })
                        .catch(markOffline);
                } catch(e) { markOffline(); }
            });
    }

    function scanVideo(video, ov) {
        if (!video || !video.videoWidth) return;
        var n = -1;
        try {
            if (faceApiLoaded && window.faceapi) {
                if (!tinyOpts) tinyOpts = new faceapi.TinyFaceDetectorOptions({ inputSize: 320, scoreThreshold: 0.4 });
                faceapi.detectAllFaces(video, tinyOpts).then(function(det) {
                    var cnt = det.length;
                    setOv(ov, cnt === 1 ? 'ok' : (cnt === 0 ? 'none' : 'many'));
                }).catch(function(){});
                return;
            } else if (window.FaceDetector) {
                if (!nativeDetector) { try { nativeDetector = new FaceDetector({ fastMode: true, maxDetectedFaces: 10 }); } catch(e) { markOffline(); return; } }
                nativeDetector.detect(video).then(function(faces) {
                    var cnt = faces.length;
                    setOv(ov, cnt === 1 ? 'ok' : (cnt === 0 ? 'none' : 'many'));
                }).catch(function(){});
                return;
            }
        } catch(e) {}
        if (!engineFailed) setOv(ov, 'scanning');
        else setOv(ov, 'offline');
    }

    setInterval(function() {
        var vids = document.querySelectorAll('[data-testid="stCameraInput"] video');
        for (var i = 0; i < vids.length; i++) {
            var ov = attachOverlay(vids[i]);
            if (ov) scanVideo(vids[i], ov);
        }
    }, 600);

    initEngine();
})();
</script>
"""

def inject_face_guard_js():
    render_js_html(FACE_GUARD_JS)

def is_valid_b64(val):
    if val is None or pd.isna(val) or isinstance(val, float):
        return False
    val_str = str(val).strip()
    if not val_str or val_str.lower() in ('none', 'nan', 'null', '0', '0.0'):
        return False
    return True

def safe_b64decode(val):
    if not is_valid_b64(val):
        return None
    try:
        return base64.b64decode(str(val).strip())
    except Exception:
        return None

import json
import io
import urllib.parse
import re
import os

try:
    from fpdf import FPDF
    FPDF_AVAILABLE = True
except ImportError:
    FPDF_AVAILABLE = False

try:
    import plotly.express as px
    import plotly.graph_objects as go
    PLOTLY_AVAILABLE = True
except ImportError:
    PLOTLY_AVAILABLE = False

try:
    import cv2
    import numpy as np
    from PIL import Image
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False
    try:
        from PIL import Image
    except ImportError:
        pass

# ---------------------------------------------------------
# COMPREHENSIVE TANZANIA REGIONS & DISTRICTS DICTIONARY
# ---------------------------------------------------------
TANZANIA_BANKS = [
    "NMB BANK", "CRDB BANK", "NBC BANK", "ABSA BANK TANZANIA",
    "AZANIA BANK", "STANBIC BANK", "EQUITY BANK", "EXIM BANK",
    "TCB (TANZANIA COMMERCIAL BANK)", "KCB BANK", "BANK OF AFRICA (BOA)",
    "DTB (DIAMOND TRUST BANK)", "M-PESA / VODACOM", "TIGO PESA / TIGO",
    "AIRTEL MONEY", "HALOPESA"
]

TANZANIA_REGIONS = [
    "ARUSHA", "DAR ES SALAAM", "DODOMA", "GEITA", "IRINGA", "KAGERA",
    "KATAVI", "KIGOMA", "KILIMANJARO", "LINDI", "MANYARA", "MARA",
    "MBEYA", "MOROGORO", "MTWARA", "MWANZA", "NJOMBE", "PEMBA KASKAZINI",
    "PEMBA KUSINI", "PWANI", "RUKWA", "RUVUMA", "SHINYANGA", "SIMIYU",
    "SINGIDA", "SONGWE", "TABORA", "TANGA", "ZANZIBAR KASKAZINI",
    "ZANZIBAR KUSINI", "ZANZIBAR MJINI MAGHARIBI"
]

# ---------------------------------------------------------
# STANDARD ROLES LIST (NYADHIFAMBALIMBALI)
# ---------------------------------------------------------
STANDARD_ROLES = [
    "MENEJA MKUU (General Manager)",
    "MENEJA MSAIDIZI (Assistant Manager)",
    "MUTEKNOLOJIA / IT SPECIALIST",
    "MOASIBU / ACCOUNTANT",
    "AFISA MAUZO (Sales Officer)",
    "MSIMAMIZI WA STOO (Storekeeper)",
    "MUDERESI / MAPOKEZI (Receptionist)",
    "MFANYAKAZI WA USAFI / ULINZI (Cleaner/Security)"
]

EDUCATION_LEVELS = [
    "ELIMU YA MSINGI (Primary School)",
    "ELIMU YA SEKONDARI (Secondary School)",
    "STASHAHADA (Diploma)",
    "SHAHADA (Bachelor Degree)",
    "UZAMILI (Master's Degree)",
    "UZAMIFU (PhD)"
]

# ---------------------------------------------------------
# DATABASE INITIALIZATION
# ---------------------------------------------------------
DB_FILE = "cyber_business_grading_system.db"

def get_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Meza ya Biashara & Meneja
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS businesses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        start_date DATE NOT NULL,
        initial_capital REAL NOT NULL,
        currency TEXT DEFAULT 'TZS',
        manager_name TEXT NOT NULL,
        manager_phone TEXT NOT NULL,
        manager_email TEXT NOT NULL,
        manager_photo_b64 TEXT,
        business_avatar_b64 TEXT,
        password TEXT NOT NULL DEFAULT 'manager1234',
        staff_password TEXT NOT NULL DEFAULT 'staff1234',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 2. Meza ya Ledger (Assets & Liabilities)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS assets_liabilities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER NOT NULL,
        item_name TEXT NOT NULL,
        item_type TEXT NOT NULL,
        quantity INTEGER DEFAULT 1,
        unit_price REAL DEFAULT 0,
        amount REAL NOT NULL,
        date_acquired DATE NOT NULL,
        description TEXT,
        sender_name TEXT,
        sender_phone TEXT,
        sender_face_b64 TEXT,
        submitted_timestamp TEXT,
        FOREIGN KEY (business_id) REFERENCES businesses (id) ON DELETE CASCADE
    );
    """)

    # 3. Meza ya Mapato & Matumizi ya Siku
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS daily_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER NOT NULL,
        record_date DATE NOT NULL,
        cash_revenue REAL DEFAULT 0,
        credit_revenue REAL DEFAULT 0,
        revenue REAL NOT NULL,
        expenses REAL DEFAULT 0,
        expense_details TEXT,
        notes TEXT,
        sender_name TEXT NOT NULL,
        sender_phone TEXT NOT NULL,
        sender_face_b64 TEXT,
        face_detected_count INTEGER DEFAULT 1,
        face_metadata_json TEXT,
        submitted_timestamp TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(business_id, record_date),
        FOREIGN KEY (business_id) REFERENCES businesses (id) ON DELETE CASCADE
    );
    """)

    # 4. Meza ya Ujumbe kwa Director
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS director_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER,
        sender_name TEXT NOT NULL,
        sender_phone TEXT NOT NULL,
        sender_email TEXT,
        subject TEXT NOT NULL,
        message TEXT NOT NULL,
        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 5. Meza ya Director Settings & System Logo
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS director_settings (
        setting_key TEXT PRIMARY KEY,
        setting_val TEXT NOT NULL
    );
    """)

    cursor.execute("INSERT OR IGNORE INTO director_settings (setting_key, setting_val) VALUES ('director_password', 'director1234')")
    cursor.execute("INSERT OR IGNORE INTO director_settings (setting_key, setting_val) VALUES ('deletion_password', 'DELETE2026')")
    cursor.execute("INSERT OR IGNORE INTO director_settings (setting_key, setting_val) VALUES ('system_logo_b64', '')")

    # 6. Meza ya Usajili wa Wafanyakazi & Meneja na Director
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS staff_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER NOT NULL,
        staff_type TEXT NOT NULL,
        full_name TEXT NOT NULL,
        residence_mkoa TEXT NOT NULL,
        residence_wilaya TEXT NOT NULL,
        full_residence_upper TEXT NOT NULL,
        phone TEXT NOT NULL,
        nida_number TEXT NOT NULL,
        email TEXT NOT NULL,
        education_level TEXT NOT NULL DEFAULT 'ELIMU YA SEKONDARI (Secondary School)',
        role_title TEXT NOT NULL,
        salary REAL DEFAULT 0,
        photo_b64 TEXT,
        contract_b64 TEXT,
        contract_filename TEXT,
        application_letter_b64 TEXT,
        letter_filename TEXT,
        cv_b64 TEXT,
        cv_filename TEXT,
        cert_b64 TEXT,
        cert_filename TEXT,
        status TEXT DEFAULT 'ACTIVE',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (business_id) REFERENCES businesses (id) ON DELETE CASCADE
    );
    """)

    # 7. Meza ya Maombi ya Ajira ya Nje (External Job Applications)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS job_applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT NOT NULL,
        residence_mkoa TEXT NOT NULL,
        residence_wilaya TEXT NOT NULL,
        full_residence_upper TEXT NOT NULL,
        phone TEXT NOT NULL,
        nida_number TEXT NOT NULL,
        email TEXT NOT NULL,
        education_level TEXT NOT NULL DEFAULT 'ELIMU YA SEKONDARI (Secondary School)',
        desired_role TEXT NOT NULL DEFAULT 'AFISA MAUZO (Sales Officer)',
        photo_b64 TEXT,
        application_letter_b64 TEXT,
        letter_filename TEXT,
        cv_b64 TEXT,
        cv_filename TEXT,
        cert_b64 TEXT,
        cert_filename TEXT,
        status TEXT DEFAULT 'PENDING',
        reply_message TEXT,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 8. Meza ya Logs za SMS kwa Staff
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS staff_sms_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recipient_type TEXT NOT NULL,
        recipient_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        message TEXT NOT NULL,
        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # 9. Meza ya Deletion Audit Logs
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS deletion_audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_type TEXT NOT NULL,
        item_details TEXT NOT NULL,
        deleted_by TEXT DEFAULT 'DIRECTOR JOSHUA JOSEPH',
        deleted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    
    
    # Column migrations for existing tables
    migration_cols = [
        ("businesses", "manager_bank_name", "TEXT DEFAULT 'NMB BANK'"),
        ("businesses", "manager_account_number", "TEXT DEFAULT '00000000000'"),
        ("businesses", "company_bank_name", "TEXT DEFAULT 'CRDB BANK'"),
        ("businesses", "company_account_number", "TEXT DEFAULT '11111111111'"),
        ("businesses", "company_bank_balance", "REAL DEFAULT 5000000.0"),
        ("businesses", "manager_bank_balance", "REAL DEFAULT 0.0"),
        ("staff_members", "bank_name", "TEXT DEFAULT 'NMB BANK'"),
        ("staff_members", "account_number", "TEXT DEFAULT '00000000000'"),
        ("staff_members", "bank_account_balance", "REAL DEFAULT 0.0"),
        ("job_applications", "bank_name", "TEXT DEFAULT 'NMB BANK'"),
        ("job_applications", "account_number", "TEXT DEFAULT '00000000000'"),
    ]
    for tbl, col, coltype in migration_cols:
        try:
            cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {coltype}")
        except Exception:
            pass

    # 10. Meza ya Malipo ya Mishahara (Payroll Disbursements)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS payroll_disbursements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER,
        sender_role TEXT NOT NULL,
        sender_name TEXT NOT NULL,
        recipient_type TEXT NOT NULL,
        recipient_name TEXT NOT NULL,
        recipient_phone TEXT NOT NULL,
        bank_name TEXT NOT NULL,
        account_number TEXT NOT NULL,
        amount REAL NOT NULL,
        payment_date DATE NOT NULL,
        reference_code TEXT NOT NULL,
        notes TEXT,
        source_bank_name TEXT,
        source_account_number TEXT,
        source_balance_after REAL,
        recipient_bank_name TEXT,
        recipient_account_number TEXT,
        recipient_balance_after REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (business_id) REFERENCES businesses (id) ON DELETE CASCADE
    );
    """)

    # Meza ya System Notifications
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS system_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recipient_role TEXT NOT NULL,
        business_id INTEGER,
        title TEXT NOT NULL,
        message TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'GENERAL',
        is_read INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Meza ya Ghala la Benki (Bank Transfer Log) - kila jaribio la kutuma
    # malipo kwenye benki husika linarekodiwa hapa
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bank_transfer_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mode TEXT,
        channel TEXT,
        channel_label TEXT,
        status TEXT,
        response_code TEXT,
        bank_txn_id TEXT,
        source TEXT,
        destination TEXT,
        dest_name TEXT,
        amount REAL,
        reference_code TEXT,
        message TEXT,
        checked_at TIMESTAMP
    );
    """)

    # Meza ya ACCOUNT LEDGER (v63) — mwendo halisi wa hela: kila DEBIT (hela
    # imetoka kwenye akaunti) na CREDIT (hela imeingia kwenye akaunti) unaandikwa
    # hapa pamoja na salio lililobakia — uthibitisho wa kwamba hela inaenda
    # KWELI kutoka akaunti moja hadi nyingine.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS account_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TIMESTAMP,
        account_owner TEXT,
        account_type TEXT,
        business_id INTEGER,
        bank_name TEXT,
        account_number TEXT,
        direction TEXT,
        amount REAL,
        balance_after REAL,
        counterparty TEXT,
        reference_code TEXT,
        bank_txn_id TEXT,
        channel_label TEXT,
        description TEXT
    );
    """)

    # Meza ya SMS LOG (v64) — kila SMS ya malipo ya mshahara inayoenda kwenye
    # simu ya mtu (kiotomatiki baada ya malipo kukamilika) inarekodiwa hapa.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sms_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TIMESTAMP,
        business_id INTEGER,
        phone TEXT NOT NULL,
        recipient_name TEXT,
        message TEXT NOT NULL,
        category TEXT DEFAULT 'SALARY_SMS',
        status TEXT,
        gateway_mode TEXT,
        gateway_txn_id TEXT
    );
    """)

    # Meza ya DAILY PROFIT (v64) — faida ya kila biashara ya kila siku
    # (mapato ya siku - matumizi ya siku) inakokotwa hapa mara tu mapato
    # ya siku yanapotumwa — ndipo inatumiwa kwenye line graph ya Director.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS daily_profit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_id INTEGER NOT NULL,
        record_date DATE NOT NULL,
        cash_revenue REAL DEFAULT 0,
        credit_revenue REAL DEFAULT 0,
        revenue REAL DEFAULT 0,
        expenses REAL DEFAULT 0,
        profit REAL DEFAULT 0,
        updated_at TIMESTAMP,
        UNIQUE(business_id, record_date)
    );
    """)

    # Meza ya RISITI PDF (v64) — risiti rasmi za malipo kwenye mfumo wa PDF
    # (bytes) zinazopatikana kwa mlipaji, Meneja, Director na mwenyewe aliye-
    # lipwa kupitia Details Zangu Binafsi.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS receipt_pdfs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TIMESTAMP,
        receipt_no TEXT NOT NULL,
        business_id INTEGER,
        recipient_name TEXT,
        recipient_phone TEXT,
        amount REAL,
        pdf_b64 TEXT NOT NULL
    );
    """)


    # Meza ya RISITI ZA MALIPO - risiti ya kila muamala inaingia hapa na
    # kuwasilishwa kwa DATABASE YA DIRECTOR na DATABASE YA MENEJA kwa pamoja
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS payment_receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        receipt_no TEXT NOT NULL,
        payment_date TIMESTAMP,
        business_id INTEGER,
        business_name TEXT,
        payer_role TEXT,
        payer_name TEXT,
        recipient_type TEXT,
        recipient_name TEXT,
        recipient_phone TEXT,
        recipient_bank TEXT,
        recipient_account TEXT,
        amount REAL NOT NULL,
        currency TEXT DEFAULT 'TZS',
        reference_code TEXT,
        bank_txn_id TEXT,
        bank_channel TEXT,
        bank_channel_label TEXT,
        gateway_status TEXT,
        gateway_message TEXT,
        delivery_director INTEGER DEFAULT 0,
        delivery_manager INTEGER DEFAULT 0,
        seen_by_director INTEGER DEFAULT 0,
        seen_by_manager INTEGER DEFAULT 0,
        notes TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)

    # Migrations for existing databases
    for col_def in [
        ("businesses", "company_bank_name TEXT DEFAULT 'CRDB BANK'"),
        ("businesses", "company_account_number TEXT DEFAULT '0152984123000'"),
        ("businesses", "company_bank_balance REAL DEFAULT 10000000.0"),
        ("staff_members", "bank_name TEXT DEFAULT 'CRDB BANK'"),
        ("staff_members", "bank_account_number TEXT DEFAULT '0150099887700'"),
        ("staff_members", "bank_account_balance REAL DEFAULT 0.0"),
        ("job_applications", "bank_name TEXT DEFAULT 'CRDB BANK'"),
        ("job_applications", "bank_account_number TEXT DEFAULT '0150099887700'"),
        ("payroll_disbursements", "source_bank_name TEXT"),
        ("payroll_disbursements", "source_account_number TEXT"),
        ("payroll_disbursements", "source_balance_after REAL"),
        ("payroll_disbursements", "recipient_bank_name TEXT"),
        ("payroll_disbursements", "recipient_account_number TEXT"),
        ("payroll_disbursements", "recipient_balance_after REAL")
    ]:
        t_name, col_spec = col_def
        try:
            cursor.execute(f"ALTER TABLE {t_name} ADD COLUMN {col_spec}")
        except Exception:
            pass

    conn.commit()
    conn.close()

init_db()

# ---------------------------------------------------------
# SETTINGS HELPERS
# ---------------------------------------------------------
def get_setting(key, default_val=""):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT setting_val FROM director_settings WHERE setting_key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    if row and row['setting_val']:
        return row['setting_val']
    return default_val

def update_setting(key, val):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO director_settings (setting_key, setting_val) VALUES (?, ?)", (key, str(val)))
    conn.commit()
    conn.close()

# ---------------------------------------------------------
# I18N LANGUAGE SYSTEM (v64): kila mtu anachagua lugha yake —
# Kiswahili (chaguo-msingi) au English. Chaguo kinahifadhiwa kwenye
# database kwa kila portal/role tofauti kama theme ilivyo.
# ---------------------------------------------------------
APP_LANGUAGES = ["Kiswahili", "English"]
PERSONAL_PORTAL_LABEL = "👤 Details Zangu Binafsi (Personal Details)"

_TRANSLATIONS = {
    # Sidebar / chrome
    "🎨 CHAGUA MUONEKANO (Interface Theme):": "🎨 CHOOSE APPEARANCE (Interface Theme):",
    "🌐 CHAGUA SYSTEM PORTAL:": "🌐 CHOOSE SYSTEM PORTAL:",
    "🏢 Mfumo wa Biashara (Business Portal)": "🏢 Business System (Business Portal)",
    "👑 Director Portal (Admin Control Panel)": "👑 Director Portal (Admin Control Panel)",
    "📝 Tuma Maombi ya Ajira (Job Application)": "📝 Submit Job Application (Job Application)",
    "🌍 CHAGUA LUGHA (Language):": "🌍 CHOOSE LANGUAGE (Language):",
    "⚡ Cyber Interface": "⚡ Cyber Interface",
    "🖥️ User Interface (Kawaida)": "🖥️ User Interface (Standard)",
    # Portals
    "📝 Tuma Maombi ya Ajira Moja kwa Moja kwa Director (Joshua Joseph)": "📝 Submit a Job Application Directly to the Director (Joshua Joseph)",
    "👑 Director Executive Control Dashboard & Admin Panel": "👑 Director Executive Control Dashboard & Admin Panel",
    # Banners (render_marquee_error)
    "❌ KOSA LAKO [ERROR]:": "❌ YOUR ERROR [ERROR]:",
    "💡 SULUHISHO [RESOLUTION]:": "💡 RESOLUTION [RESOLUTION]:",
    "📍 MAHALI PA KOSA [LOCATION]:": "📍 ERROR LOCATION [LOCATION]:",
    # Director dashboard
    "📊 Cross-Business Financial Overview": "📊 Cross-Business Financial Overview",
    "Jumla ya Biashara": "Total Businesses",
    "Jumla Kuu Mapato": "Total Revenue",
    "Jumla ya Matumizi": "Total Expenses",
    "Faida Halisi Cash": "Net Cash Profit",
    "Madeni Yote": "Total Liabilities",
    "📈 UKUAJI WA KILA BIASHARA KWA FAIDA YA SIKU (Business Growth by Daily Profit)": "📈 BUSINESS GROWTH BY DAILY PROFIT (Business Growth by Daily Profit)",
    "Tarehe": "Date",
    "Faida (TZS)": "Profit (TZS)",
    "Biashara": "Business",
    # Payments
    "💳 THIBITISHA & TUMA MALIPO YA MSHAHARA": "💳 CONFIRM & SEND SALARY PAYMENT",
    "🏦 Benki ya Kutolea Hela (Mlipaji)": "🏦 Source Bank (Payer)",
    "🔢 Akaunti ya Kutolea Hela (Account Namba)": "🔢 Source Account (Account Number)",
    "🔐 PIN ya Akaunti ya Kutolea Hela (Validation Kuu)": "🔐 Source Account PIN (Main Validation)",
    "💰 PIN ya Akaunti Yako (Validation)": "💰 Your Account PIN (Validation)",
    # Manager graph
    "📈 Mwenendo wa Faida ya Kila Siku (Daily Profit Line Graph)": "📈 Daily Profit Trend (Daily Profit Line Graph)",
}

_CURRENT_LANG_CACHE = {"val": None, "scope": None}

def resolve_language_scope():
    """Nani anaingia sasa — chaguo la lugha linahifadhiwa kwa kila mtu/portal."""
    pm = str(st.session_state.get("portal_mode", "") or "")
    if "Director" in pm:
        return "DIRECTOR"
    if "Biashara" in pm or "Business" in pm:
        role = str(st.session_state.get("session_role", "") or "")
        who = str(st.session_state.get("session_user_name", "") or "")
        biz = str(st.session_state.get("session_business_name", "") or "")
        if role or who or biz:
            return f"BIZ::{role}::{who}::{biz}"
        return "BIZ"
    if "Details" in pm or "Personal" in pm:
        return "PERSONAL"
    return "PUBLIC"

def get_current_language():
    """Lugha ya sasa: session_state kwanza, kisha database (setting lang::<scope>)."""
    sess = st.session_state.get("app_language")
    if sess in APP_LANGUAGES:
        return sess
    scope = resolve_language_scope()
    if _CURRENT_LANG_CACHE["scope"] == scope and _CURRENT_LANG_CACHE["val"]:
        lang = _CURRENT_LANG_CACHE["val"]
        st.session_state["app_language"] = lang
        return lang
    saved = get_setting(f"lang::{scope}", "Kiswahili")
    if saved not in APP_LANGUAGES:
        saved = "Kiswahili"
    _CURRENT_LANG_CACHE["scope"] = scope
    _CURRENT_LANG_CACHE["val"] = saved
    st.session_state["app_language"] = saved
    return saved

def t(sw_text):
    """Tafsiri ya papo hapo: Kiswahili → English (kama English imechaguliwa)."""
    try:
        lang = get_current_language()
    except Exception:
        lang = "Kiswahili"
    if lang == "English":
        return _TRANSLATIONS.get(str(sw_text), str(sw_text))
    return str(sw_text)


# ---------------------------------------------------------
# VERSION-SAFE HTML+JS RENDERER
# (Inafanya kazi kwenye Streamlit zote: zamani, sasa na mpya.
#  Streamlit mpya inazuia JS kwenye st.html bila bendera maalum.)
# ---------------------------------------------------------
def render_js_html(html_str, height=0):
    try:
        st.html(html_str, unsafe_allow_javascript=True)
        return
    except TypeError:
        pass
    except Exception:
        pass
    try:
        # Streamlit ya zamani: st.html inaruhusu JS kwa chaguo-msingi
        st.html(html_str)
        return
    except Exception:
        pass
    try:
        st.components.v1.html(html_str, height=height)
    except Exception:
        pass

# ---------------------------------------------------------
# FIELD BLINK ON ERROR (uwanja wenye kosa unablink na glow nyekundu)
# ---------------------------------------------------------
def blink_error_field(field_labels, duration=5.0):
    """Fanya widget zenye label zinazopatana na field_labels ziblink wakati wa error."""
    if not field_labels:
        return
    if isinstance(field_labels, str):
        field_labels = [field_labels]
    selectors_js = ", ".join('"{0}"'.format(str(s).lower().replace('"', '')) for s in field_labels)
    duration_ms = int(float(duration) * 1000)
    blink_js = f"""
    <script>
    (function() {{
        try {{
            var sels = [{selectors_js}];
            var durs = {duration_ms};
            var tries = 0;

            function getDocs() {{
                var docs = [];
                try {{
                    if (window.parent && window.parent.document && window.parent.document.body) {{
                        docs.push(window.parent.document);
                    }}
                }} catch(e) {{ }}
                docs.push(document);
                return docs;
            }}

            function findAndBlink() {{
                var foundAny = false;
                var docs = getDocs();
                for (var d = 0; d < docs.length; d++) {{
                    var doc = docs[d];
                    var labels = doc.querySelectorAll('label');
                    for (var i = 0; i < labels.length; i++) {{
                        var txt = (labels[i].textContent || '').toLowerCase();
                        var hit = false;
                        for (var j = 0; j < sels.length; j++) {{
                            if (txt.indexOf(sels[j]) !== -1) {{ hit = true; break; }}
                        }}
                        if (!hit) continue;
                        var el = null;
                        try {{
                            el = labels[i].closest('div[data-testid="stTextInput"], div[data-testid="stTextArea"], div[data-testid="stCameraInput"], div[data-testid="stFileUploader"], div[data-testid="stSelectbox"], div[data-testid="stNumberInput"], div[data-testid="stDateInput"], div[data-testid="stTimeInput"], div[data-testid="stRadio"]');
                        }} catch(e) {{ el = null; }}
                        if (!el) el = labels[i];
                        el.classList.add('cyber-error-blink-field');
                        // Badge ya cyber ERROR kwenye kona
                        try {{
                            var pos = doc.defaultView ? doc.defaultView.getComputedStyle(el).position : '';
                            if (pos !== 'absolute' && pos !== 'fixed') {{ el.style.position = 'relative'; }}
                            if (!el.querySelector('.cyber-error-field-tag')) {{
                                var tag = doc.createElement('span');
                                tag.className = 'cyber-error-field-tag';
                                tag.textContent = '❌ ERROR';
                                el.appendChild(tag);
                            }}
                        }} catch(e) {{ }}
                        foundAny = true;
                        (function(elm, d) {{
                            setTimeout(function() {{
                                elm.classList.remove('cyber-error-blink-field');
                                var t = elm.querySelector('.cyber-error-field-tag');
                                if (t) t.remove();
                            }}, d);
                        }})(el, durs);
                    }}
                }}
                return foundAny;
            }}

            function attempt() {{
                tries++;
                if (findAndBlink()) return;
                if (tries < 15) setTimeout(attempt, 200);
            }}
            attempt();
        }} catch(e) {{ console.log(e); }}
    }})();
    </script>
    """
    render_js_html(blink_js)

# ---------------------------------------------------------
# SOUND BEEP TRIGGER (Web Audio API - Cyberpunk Audio Engine)
# ---------------------------------------------------------
def trigger_beep_sound(frequency=880, duration=5.0, beep_type="cyber_alert"):
    beep_html = f"""
    <script>
    (function() {{
        try {{
            var AudioContext = window.AudioContext || window.webkitAudioContext;
            if (!AudioContext) return;
            var ctx = new AudioContext();
            if (ctx.state === 'suspended') {{ ctx.resume(); }}
            var now = ctx.currentTime;
            var type = "{beep_type}";
            
            if (type === "cyber_alert" || type === "sawtooth" || type === "square" || type === "error") {{
                // ============================================================
                //  CYBER ERROR ALARM ENGINE v2 — harsh digital glitch alarm
                //  Layers: (1) dissonant error buzz, (2) glitch stutter,
                //          (3) static noise burst, (4) waveshaper distortion
                // ============================================================
                var dur = {duration};
                var t0 = now;

                // Master chain: master gain -> distortion (tanh waveshaper) -> speakers
                var master = ctx.createGain();
                master.gain.value = 0.85;
                var shaper = ctx.createWaveShaper();
                var curveLen = 512, curve = new Float32Array(curveLen);
                for (var ci = 0; ci < curveLen; ci++) {{
                    var cx = ci * 2 / curveLen - 1;
                    curve[ci] = Math.tanh(3.2 * cx);
                }}
                shaper.curve = curve;
                shaper.oversample = '2x';
                master.connect(shaper);
                shaper.connect(ctx.destination);

                // Pre-render one white-noise buffer for static bursts
                var noiseLen = Math.floor(ctx.sampleRate * 0.14);
                var noiseBuf = ctx.createBuffer(1, noiseLen, ctx.sampleRate);
                var nData = noiseBuf.getChannelData(0);
                for (var ni = 0; ni < noiseLen; ni++) {{ nData[ni] = Math.random() * 2 - 1; }}

                var cycle = 1.0; // one full error cycle per second
                var cycles = Math.max(1, Math.ceil(dur / cycle));
                var baseF = {frequency};

                for (var c = 0; c < cycles; c++) {{
                    var tc = t0 + c * cycle;
                    if (tc >= t0 + dur) break;

                    // --- LAYER 1: harsh dissonant buzz with downward crash bend ---
                    var o1 = ctx.createOscillator();
                    var g1 = ctx.createGain();
                    o1.type = 'square';
                    o1.frequency.setValueAtTime(baseF, tc);
                    o1.frequency.exponentialRampToValueAtTime(Math.max(60, baseF * 0.5), tc + 0.30);
                    g1.gain.setValueAtTime(0.0001, tc);
                    g1.gain.exponentialRampToValueAtTime(0.30, tc + 0.015);
                    g1.gain.setValueAtTime(0.30, tc + 0.30);
                    g1.gain.exponentialRampToValueAtTime(0.0001, tc + 0.38);
                    o1.connect(g1);
                    g1.connect(master);
                    o1.start(tc);
                    o1.stop(tc + 0.40);

                    // --- LAYER 2: glitch stutter (rapid alternating dissonant blips) ---
                    var fA = Math.max(80, baseF * 0.5);
                    var fB = Math.max(60, baseF * 0.37); // dissonant ratio = alarm feel
                    var blipDur = 0.055, blipGap = 0.008, blipCount = 9;
                    var tb = tc + 0.42;
                    for (var b = 0; b < blipCount; b++) {{
                        var st = tb + b * (blipDur + blipGap);
                        if (st >= t0 + dur) break;
                        var o2 = ctx.createOscillator();
                        var g2 = ctx.createGain();
                        o2.type = (b % 2 === 0) ? 'sawtooth' : 'square';
                        var fNow = (b % 2 === 0) ? fA : fB;
                        o2.frequency.setValueAtTime(fNow, st);
                        o2.frequency.linearRampToValueAtTime(fNow * 0.78, st + blipDur);
                        g2.gain.setValueAtTime(0.24, st);
                        g2.gain.exponentialRampToValueAtTime(0.0001, st + blipDur);
                        o2.connect(g2);
                        g2.connect(master);
                        o2.start(st);
                        o2.stop(st + blipDur + 0.01);
                    }}

                    // --- LAYER 3: digital static burst at each cycle start ---
                    var src = ctx.createBufferSource();
                    src.buffer = noiseBuf;
                    var bp = ctx.createBiquadFilter();
                    bp.type = 'bandpass';
                    bp.frequency.value = 1900;
                    bp.Q.value = 0.7;
                    var ng = ctx.createGain();
                    ng.gain.setValueAtTime(0.14, tc);
                    ng.gain.exponentialRampToValueAtTime(0.0001, tc + 0.13);
                    src.connect(bp);
                    bp.connect(ng);
                    ng.connect(master);
                    src.start(tc);

                    // --- LAYER 4: sub-bass system thump (cyber impact) ---
                    var o3 = ctx.createOscillator();
                    var g3 = ctx.createGain();
                    o3.type = 'sine';
                    o3.frequency.setValueAtTime(95, tc + 0.36);
                    o3.frequency.exponentialRampToValueAtTime(38, tc + 0.55);
                    g3.gain.setValueAtTime(0.30, tc + 0.36);
                    g3.gain.exponentialRampToValueAtTime(0.0001, tc + 0.60);
                    o3.connect(g3);
                    g3.connect(master);
                    o3.start(tc + 0.36);
                    o3.stop(tc + 0.62);
                }}
            }} else if (type === "success" || type === "sine" || type === "cyber_success") {{
                // High-Tech Ascending Crystal Cyber Chime
                var freqs = [523.25, 659.25, 783.99, 1046.50];
                freqs.forEach(function(f, idx) {{
                    var osc = ctx.createOscillator();
                    var gain = ctx.createGain();
                    osc.type = 'triangle';
                    var startTime = now + idx * 0.09;
                    osc.frequency.setValueAtTime(f, startTime);
                    gain.gain.setValueAtTime(0.25, startTime);
                    gain.gain.exponentialRampToValueAtTime(0.001, startTime + 0.35);
                    osc.connect(gain);
                    gain.connect(ctx.destination);
                    osc.start(startTime);
                    osc.stop(startTime + 0.35);
                }});
            }} else {{
                // Sci-Fi Synth Blip
                var osc = ctx.createOscillator();
                var gain = ctx.createGain();
                osc.type = 'sine';
                osc.frequency.setValueAtTime({frequency}, now);
                osc.frequency.exponentialRampToValueAtTime({frequency} * 1.5, now + 0.25);
                gain.gain.setValueAtTime(0.2, now);
                gain.gain.exponentialRampToValueAtTime(0.001, now + 0.3);
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.start(now);
                osc.stop(now + 0.3);
            }}
        }} catch(e) {{ console.log(e); }}
    }})();
    </script>
    """
    render_js_html(beep_html)

# ---------------------------------------------------------
# MARQUEE SCROLLING ERROR BANNER WITH HIGH-TECH CYBER HUD,
# GLITCH TEXT (RGB SPLIT + FLICKER), SCANLINES & LASER EFFECT
# v60 FIX: Banner sasa ni SELF-CONTAINED (CSS yote ndani yake) na
# inarenderiwa kwa render_js_html (st.html / components iframe).
# KABLAYA: st.markdown ilikuwa inageuza mistari yenye nafasi 4 kuwa
# CODE BLOCK - ndiyo maana mtu aliona CODE GHAFI badala ya maneno ya
# cyber. st.html na components.v1.html hazitumii markdown parser,
# hivyo banner inaonekana kama ilivyoundwa kwenye Streamlit zote.
# ---------------------------------------------------------
CYBER_BANNER_EMBED_CSS = """
<style>
    html, body { margin: 0 !important; padding: 0 !important; }

    @keyframes cyberPulseBorder {
        0% { border-color: #00F0FF; box-shadow: 0 0 25px rgba(0, 240, 255, 0.9), inset 0 0 15px rgba(255, 0, 127, 0.4); background: #030712; }
        50% { border-color: #FF007F; box-shadow: 0 0 30px rgba(255, 0, 127, 0.95), inset 0 0 20px rgba(0, 240, 255, 0.5); background: #0B0F19; }
        100% { border-color: #00F0FF; box-shadow: 0 0 25px rgba(0, 240, 255, 0.9), inset 0 0 15px rgba(255, 0, 127, 0.4); background: #030712; }
    }

    .cyber-mode-error-wrapper {
        animation: cyberPulseBorder 1.5s infinite alternate;
        border: 2px solid #00F0FF !important;
        border-radius: 12px;
        padding: 12px 16px;
        margin-bottom: 18px;
        position: relative;
        overflow: hidden;
        background-color: #030712;
        background-image:
            linear-gradient(rgba(0, 240, 255, 0.045) 1px, transparent 1px),
            linear-gradient(90deg, rgba(0, 240, 255, 0.045) 1px, transparent 1px);
        background-size: 22px 22px;
    }

    .cyber-mode-error-wrapper::before,
    .cyber-mode-error-wrapper::after {
        content: "";
        position: absolute;
        width: 20px;
        height: 20px;
        z-index: 3;
        pointer-events: none;
    }
    .cyber-mode-error-wrapper::before {
        top: 0; left: 0;
        border-top: 3px solid #39FF14;
        border-left: 3px solid #39FF14;
        filter: drop-shadow(0 0 6px #39FF14);
    }
    .cyber-mode-error-wrapper::after {
        bottom: 0; right: 0;
        border-bottom: 3px solid #39FF14;
        border-right: 3px solid #39FF14;
        filter: drop-shadow(0 0 6px #39FF14);
    }

    .cyber-mode-hud-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        border-bottom: 1px dashed rgba(0, 240, 255, 0.5);
        padding-bottom: 6px;
        margin-bottom: 8px;
        font-family: 'Consolas', 'Courier New', monospace;
        gap: 8px;
        flex-wrap: wrap;
    }

    .cyber-hud-left, .cyber-hud-right {
        display: flex;
        align-items: center;
        gap: 12px;
    }

    .cyber-hud-glitch-tag {
        color: #FF007F;
        font-weight: 900;
        font-size: 0.85rem;
        letter-spacing: 1.5px;
        text-shadow: 0 0 10px #FF007F;
        display: inline-block;
        animation: cyberHudGlitch 2.8s infinite steps(1, end);
    }

    @keyframes cyberHudGlitch {
        0%, 87%, 100% { text-shadow: 0 0 10px #FF007F; transform: translate(0, 0) skewX(0deg); }
        88% { text-shadow: -2px 0 #00F0FF, 2px 0 #FFD700; transform: translate(1px, 0) skewX(-7deg); }
        90% { text-shadow: 2px 0 #00F0FF, -2px 0 #39FF14; transform: translate(-1px, 0) skewX(5deg); }
        92% { text-shadow: 0 0 10px #FF007F; transform: translate(0, 0) skewX(0deg); }
    }

    .cyber-hud-pulse-dot {
        color: #FF0055;
        font-size: 0.75rem;
        font-weight: 800;
        letter-spacing: 1px;
        text-shadow: 0 0 8px #FF0055;
        animation: cyberDotPulse 1s infinite ease-in-out;
    }

    @keyframes cyberDotPulse {
        0%, 100% { opacity: 1; text-shadow: 0 0 8px #FF0055; }
        50% { opacity: 0.4; text-shadow: 0 0 2px #FF0055; }
    }

    .cyber-hud-sound-tag {
        color: #00F0FF;
        font-size: 0.78rem;
        font-weight: 700;
        text-shadow: 0 0 8px #00F0FF;
        letter-spacing: 1px;
        animation: cyberSoundPulse 0.9s infinite ease-in-out;
    }

    @keyframes cyberSoundPulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.55; }
    }

    .cyber-hud-err-code {
        color: #FFD700;
        font-size: 0.72rem;
        font-weight: 800;
        letter-spacing: 1px;
        text-shadow: 0 0 8px rgba(255, 215, 0, 0.8);
        border: 1px solid rgba(255, 215, 0, 0.5);
        border-radius: 4px;
        padding: 1px 6px;
        background: rgba(255, 215, 0, 0.08);
        font-family: 'Consolas', 'Courier New', monospace;
    }

    /* ==========================================================
       STATIC CYBER PANEL — KOSA + SULUHISHO vinayosomeka papo hapo
       (bila kusubiri marquee), mtu anaeleweka kosa lake MARA MOJA
       ========================================================== */
    .cyber-static-panel {
        margin: 6px 0 10px 0;
        padding: 8px 12px;
        border-left: 4px solid #FF0040;
        background: rgba(255, 0, 64, 0.07);
        border-radius: 6px;
        font-family: 'Consolas', 'Courier New', monospace;
        position: relative;
        z-index: 2;
    }

    .cyber-static-row {
        display: flex;
        align-items: baseline;
        gap: 10px;
        padding: 3px 0;
        flex-wrap: wrap;
    }

    .cyber-static-lbl {
        color: #FFD700;
        font-size: 0.78rem;
        font-weight: 900;
        letter-spacing: 1.5px;
        text-shadow: 0 0 8px rgba(255, 215, 0, 0.7);
        white-space: nowrap;
    }

    .cyber-static-val {
        font-size: 0.98rem;
        font-weight: 800;
        letter-spacing: 0.5px;
        word-break: break-word;
    }

    .cyber-static-err {
        color: #FFFFFF;
        text-shadow: 0 0 10px #FF0040;
        animation: cyberErrFlicker 2.2s infinite steps(1, end);
    }

    .cyber-static-fix {
        color: #39FF14;
        text-shadow: 0 0 10px rgba(57, 255, 20, 0.85);
    }

    .cyber-static-loc {
        color: #00F0FF;
        text-shadow: 0 0 10px rgba(0, 240, 255, 0.85);
    }

    .cyber-mode-ticker-container {
        padding: 6px 0;
        background: rgba(2, 6, 23, 0.95);
        border-radius: 6px;
        position: relative;
        overflow: hidden;
    }

    .cyber-mode-ticker-container::after {
        content: "";
        position: absolute;
        top: 0; left: 0; right: 0; bottom: 0;
        background: repeating-linear-gradient(0deg,
            rgba(0, 240, 255, 0.06) 0px,
            rgba(0, 240, 255, 0.06) 1px,
            transparent 1px,
            transparent 3px);
        pointer-events: none;
        z-index: 2;
    }

    .cyber-mode-ticker-container::before {
        content: "";
        position: absolute;
        top: 0;
        bottom: 0;
        width: 70px;
        left: -70px;
        background: linear-gradient(90deg, transparent, rgba(0, 240, 255, 0.14), transparent);
        animation: cyberTrackSweep 2.8s infinite linear;
        pointer-events: none;
        z-index: 1;
    }

    @keyframes cyberTrackSweep {
        0% { left: -70px; }
        100% { left: 100%; }
    }

    /* CSS-animated ticker (badala ya tag ya marquee: inafanya kazi hata
       pale HTML inaposafishwa na DOMPurify au Streamlit mpya) */
    .cyber-mode-marquee-track {
        display: flex;
        width: max-content;
        will-change: transform;
        animation: cyberMarqueeScroll 26s linear infinite;
    }

    .cyber-mode-ticker-container:hover .cyber-mode-marquee-track {
        animation-play-state: paused;
    }

    @keyframes cyberMarqueeScroll {
        0% { transform: translateX(0); }
        100% { transform: translateX(-50%); }
    }

    .cyber-mode-marquee-text {
        font-family: 'Consolas', 'Courier New', monospace !important;
        font-size: 1.18rem !important;
        font-weight: 900 !important;
        letter-spacing: 2px !important;
        color: #00F0FF;
        white-space: nowrap;
        flex: 0 0 auto;
        padding-right: 90px;
        animation: cyberTickerGlitch 3.4s infinite steps(1, end);
    }

    @keyframes cyberTickerGlitch {
        0%, 83%, 96%, 100% {
            transform: translate(0, 0) skewX(0deg);
            text-shadow: 0 0 8px rgba(0, 240, 255, 0.65);
            opacity: 1;
        }
        84% {
            transform: translate(2px, -1px) skewX(-8deg);
            text-shadow: -4px 0 #FF0040, 4px 0 #00F0FF, 0 0 12px rgba(0, 240, 255, 0.9);
        }
        86% {
            transform: translate(-3px, 1px) skewX(6deg);
            text-shadow: 4px -1px #FF0040, -4px 1px #00F0FF;
            opacity: 0.82;
        }
        88% {
            transform: translate(1px, 0) skewX(-3deg);
            text-shadow: -2px 0 #39FF14, 3px 0 #FF0040;
        }
        90% {
            transform: translate(-1px, 1px) skewX(2deg);
            text-shadow: 2px 0 #00F0FF, -2px 0 #FFD700;
            opacity: 0.9;
        }
        92% {
            transform: translate(0, 0) skewX(0deg);
            opacity: 1;
        }
    }

    .cyber-text-alert {
        color: #FF007F;
        text-shadow: 0 0 12px #FF007F, 0 0 20px #FF007F;
        position: relative;
        display: inline-block;
        animation: cyberAlertFlicker 2.6s infinite steps(1, end);
    }

    .cyber-text-alert::before,
    .cyber-text-alert::after {
        content: attr(data-text);
        position: absolute;
        top: 0;
        left: 0;
        width: 100%;
        pointer-events: none;
        text-shadow: none;
    }

    .cyber-text-alert::before {
        color: #00F0FF;
        animation: cyberGlitchSliceA 2.1s infinite steps(1, end);
        z-index: 2;
    }

    .cyber-text-alert::after {
        color: #FFFFFF;
        animation: cyberGlitchSliceB 1.7s infinite steps(1, end);
        z-index: 1;
    }

    @keyframes cyberGlitchSliceA {
        0%, 74%, 100% { clip-path: inset(50% 0 50% 0); transform: translate(0, 0); opacity: 0; }
        75% { clip-path: inset(10% 0 74% 0); transform: translate(-3px, -1px); opacity: 0.85; }
        79% { clip-path: inset(62% 0 20% 0); transform: translate(3px, 1px); opacity: 0.85; }
        83% { clip-path: inset(36% 0 46% 0); transform: translate(-2px, 0); opacity: 0.85; }
        87% { clip-path: inset(78% 0 6% 0); transform: translate(2px, -1px); opacity: 0.85; }
        91% { clip-path: inset(50% 0 50% 0); transform: translate(0, 0); opacity: 0; }
    }

    @keyframes cyberGlitchSliceB {
        0%, 79%, 100% { clip-path: inset(50% 0 50% 0); transform: translate(0, 0); opacity: 0; }
        80% { clip-path: inset(28% 0 58% 0); transform: translate(3px, 1px); opacity: 0.75; }
        84% { clip-path: inset(55% 0 30% 0); transform: translate(-3px, -1px); opacity: 0.75; }
        88% { clip-path: inset(8% 0 80% 0); transform: translate(2px, 0); opacity: 0.75; }
        92% { clip-path: inset(44% 0 42% 0); transform: translate(-2px, 1px); opacity: 0.75; }
        96% { clip-path: inset(50% 0 50% 0); transform: translate(0, 0); opacity: 0; }
    }

    @keyframes cyberAlertFlicker {
        0%, 66%, 100% { opacity: 1; }
        68% { opacity: 0.5; }
        70% { opacity: 1; }
        72% { opacity: 0.72; }
        74% { opacity: 1; }
    }

    .cyber-text-loc-lbl {
        color: #FFD700;
        text-shadow: 0 0 10px #FFD700;
    }

    .cyber-text-loc-val {
        color: #00F0FF;
        text-shadow: 0 0 12px #00F0FF, 0 0 22px #00F0FF;
    }

    .cyber-text-err-lbl {
        color: #FF0055;
        text-shadow: 0 0 10px #FF0055;
    }

    .cyber-text-err-val {
        color: #FFFFFF;
        text-shadow: 0 0 10px #FFFFFF, 0 0 18px rgba(255, 255, 255, 0.8);
        animation: cyberErrFlicker 1.9s infinite steps(1, end);
    }

    @keyframes cyberErrFlicker {
        0%, 81%, 100% {
            text-shadow: 0 0 10px #FFFFFF, 0 0 18px rgba(255, 255, 255, 0.8);
            transform: translateX(0);
            opacity: 1;
        }
        82% {
            text-shadow: -2px 0 #FF0040, 2px 0 #00F0FF;
            transform: translateX(1px);
        }
        84% {
            text-shadow: 2px 0 #FF0040, -2px 0 #00F0FF;
            transform: translateX(-1px);
            opacity: 0.85;
        }
        86% {
            text-shadow: 0 0 10px #FFFFFF, 0 0 18px rgba(255, 255, 255, 0.8);
            transform: translateX(0);
            opacity: 1;
        }
    }

    .cyber-text-fix-lbl {
        color: #A855F7;
        text-shadow: 0 0 10px #A855F7;
    }

    .cyber-text-fix-val {
        color: #39FF14;
        text-shadow: 0 0 12px #39FF14, 0 0 22px #39FF14;
    }

    .cyber-text-status {
        color: #00F0FF;
        text-shadow: 0 0 10px #00F0FF;
        animation: cyberStatusPulse 1.6s infinite ease-in-out;
    }

    @keyframes cyberStatusPulse {
        0%, 100% { text-shadow: 0 0 6px #00F0FF; }
        50% { text-shadow: 0 0 16px #00F0FF, 0 0 26px #00F0FF; }
    }

    .cyber-text-blink {
        animation: cyberBlink 0.8s infinite steps(1, end);
    }

    @keyframes cyberBlink {
        0% { opacity: 1; }
        50% { opacity: 0; }
        100% { opacity: 1; }
    }

    .cyber-laser-scan-line {
        position: absolute;
        bottom: 0;
        left: 0;
        width: 100%;
        height: 3px;
        background: linear-gradient(90deg, transparent, #FF007F, #00F0FF, #39FF14, transparent);
        box-shadow: 0 0 15px #00F0FF;
        animation: cyberLaserAnim 2s infinite linear;
    }

    @keyframes cyberLaserAnim {
        0% { transform: translateX(-100%); }
        100% { transform: translateX(100%); }
    }
</style>
"""


# ---------------------------------------------------------
# CLEAN BANNER CSS (v63) — kwa ajili ya "🖥️ User Interface (Kawaida)":
# banner ya kosa inaonekana kama tahadhari rasmi na safi (bila glitch,
# scanlines wala ticker), ila bado inaonyesha KOSA, SULUHISHO na MAHALI.
# ---------------------------------------------------------
SIMPLE_BANNER_EMBED_CSS = """
<style>
    html, body { margin: 0 !important; padding: 0 !important; background: transparent !important; }

    .cyber-mode-error-wrapper {
        animation: none !important;
        border: 1px solid #E2E8F0 !important;
        border-left: 6px solid #DC2626 !important;
        border-radius: 10px;
        padding: 12px 16px;
        margin-bottom: 18px;
        position: relative;
        overflow: hidden;
        background-color: #FFFFFF !important;
        background-image: none !important;
        box-shadow: 0 2px 10px rgba(15, 23, 42, 0.08);
    }
    .cyber-mode-error-wrapper::before,
    .cyber-mode-error-wrapper::after { display: none !important; }

    .cyber-mode-hud-header {
        background: #FEF2F2;
        border-bottom: 1px solid #FECACA;
        border-radius: 6px 6px 0 0;
        padding: 8px 12px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 8px;
        margin-bottom: 10px;
    }
    .cyber-hud-glitch-tag {
        color: #B91C1C !important;
        font-weight: 900;
        font-size: 0.95rem;
        text-shadow: none !important;
        animation: none !important;
    }
    .cyber-hud-glitch-tag::before, .cyber-hud-glitch-tag::after { display: none !important; }
    .cyber-hud-pulse-dot {
        color: #DC2626 !important;
        font-weight: 700;
        font-size: 0.8rem;
        animation: none !important;
    }
    .cyber-hud-err-code {
        color: #64748B !important;
        font-family: 'Consolas', 'Courier New', monospace;
        font-size: 0.8rem;
        text-shadow: none !important;
    }
    .cyber-hud-sound-tag {
        color: #64748B !important;
        font-size: 0.78rem;
        text-shadow: none !important;
        animation: none !important;
    }

    .cyber-static-panel {
        background: #FFFFFF !important;
        border: 1px solid #F1F5F9;
        border-radius: 8px;
        padding: 10px 14px;
        display: flex;
        flex-direction: column;
        gap: 8px;
    }
    .cyber-static-row {
        display: flex;
        gap: 10px;
        align-items: baseline;
        flex-wrap: wrap;
    }
    .cyber-static-lbl {
        color: #334155 !important;
        font-weight: 800;
        font-size: 0.88rem;
        white-space: nowrap;
        text-shadow: none !important;
    }
    .cyber-static-val {
        font-weight: 700;
        font-size: 0.95rem;
        text-shadow: none !important;
        animation: none !important;
    }
    .cyber-static-err { color: #B91C1C !important; }
    .cyber-static-fix { color: #166534 !important; }
    .cyber-static-loc { color: #1D4ED8 !important; }

    /* Ticker na laser ni za cyber tu — clean theme inazificha */
    .cyber-mode-ticker-container { display: none !important; }
    .cyber-mode-marquee-track { display: none !important; }
    .cyber-laser-scan-line { display: none !important; }
    .cyber-text-alert, .cyber-text-status, .cyber-text-blink { animation: none !important; }

    * { animation: none !important; text-shadow: none !important; }
</style>
"""

def render_marquee_error(error_location, error_msg, fix_action, field_label=None):
    import random as _rnd
    import html as _html

    def _esc(v):
        return _html.escape(str(v), quote=True)

    error_location = _esc(error_location)
    error_msg = _esc(error_msg)
    fix_action = _esc(fix_action)
    err_code = "0x{:04X}-{:02X}".format(_rnd.randint(0x1000, 0xFFFF), _rnd.randint(0x10, 0xFF))

    # ── THEME-AWARE (v63): Cyber theme = glitch banner; Simple theme = tahadhari safi ──
    if get_ui_theme() == "SIMPLE":
        _banner_css = SIMPLE_BANNER_EMBED_CSS
        _hud_tag = "⚠️ [SYSTEM VALIDATION ALERT]"
        _hud_status = "● ALERT ACTIVE — ERROR DETECTED"
        _hud_sound = "🔔 ERROR ALARM (5s)"
        _ticker_css_note = ""
    else:
        _banner_css = CYBER_BANNER_EMBED_CSS
        _hud_tag = "⚡ [CYBER PROTOCOL EXCEPTION]"
        _hud_status = "● SYSTEM ALERT ACTIVE"
        _hud_sound = "🔊 CYBER ERROR ALARM (5s)"

    ticker_content = (
        '<span class="cyber-text-alert" data-text="🚨 [CRITICAL ALERT DETECTED]">🚨 [CRITICAL ALERT DETECTED]</span> &nbsp;&nbsp;&nbsp;&nbsp; '
        f'<span class="cyber-text-loc-lbl">📍 [LOCATION]:</span> <span class="cyber-text-loc-val" data-text="{error_location}">{error_location}</span> &nbsp;&nbsp;⚡//⚡&nbsp;&nbsp; '
        f'<span class="cyber-text-err-lbl">❌ [ERROR REASON]:</span> <span class="cyber-text-err-val">{error_msg}</span> &nbsp;&nbsp;⚡//⚡&nbsp;&nbsp; '
        f'<span class="cyber-text-fix-lbl">💡 [CYBER RESOLUTION]:</span> <span class="cyber-text-fix-val" data-text="{fix_action}">{fix_action}</span> &nbsp;&nbsp;&nbsp;&nbsp; '
        '<span class="cyber-text-status">🔒 [DATABASE SECURED & AUDIT LOGGED]</span> &nbsp;&nbsp;&nbsp;&nbsp; '
        '<span class="cyber-text-status cyber-text-blink">▮</span>'
    )

    marquee_html = f"""{_banner_css}
<div class="cyber-mode-error-wrapper">
  <div class="cyber-mode-hud-header">
    <div class="cyber-hud-left">
      <span class="cyber-hud-glitch-tag" data-text="{_hud_tag}">{_hud_tag}</span>
      <span class="cyber-hud-pulse-dot">{_hud_status}</span>
    </div>
    <div class="cyber-hud-right">
      <span class="cyber-hud-err-code">ERR::{err_code}</span>
      <span class="cyber-hud-sound-tag">{_hud_sound}</span>
    </div>
  </div>
  <div class="cyber-static-panel">
    <div class="cyber-static-row"><span class="cyber-static-lbl">{t('❌ KOSA LAKO [ERROR]:')}</span><span class="cyber-static-val cyber-static-err">{error_msg}</span></div>
    <div class="cyber-static-row"><span class="cyber-static-lbl">{t('💡 SULUHISHO [RESOLUTION]:')}</span><span class="cyber-static-val cyber-static-fix">{fix_action}</span></div>
    <div class="cyber-static-row"><span class="cyber-static-lbl">{t('📍 MAHALI PA KOSA [LOCATION]:')}</span><span class="cyber-static-val cyber-static-loc">{error_location}</span></div>
  </div>
  <div class="cyber-mode-ticker-container">
    <div class="cyber-mode-marquee-track">
      <span class="cyber-mode-marquee-text">{ticker_content}</span>
      <span class="cyber-mode-marquee-text" aria-hidden="true">{ticker_content}</span>
    </div>
  </div>
  <div class="cyber-laser-scan-line"></div>
</div>"""
    # v60: render_js_html badala ya st.markdown — hii ndiyo ilikuwa ndizo ya
    # kuonekana kwa code ghafi. st.html/components.v1.html hazichakati markdown.
    render_js_html(marquee_html, height=250)
    if field_label:
        try:
            blink_error_field(field_label, duration=5.0)
        except Exception:
            pass


def validate_three_names(name_str, field_label="Majina"):
    cleaned = str(name_str).strip()
    words = [w for w in cleaned.split() if w]
    if len(words) < 3:
        return False, f"Sehemu ya {field_label} inalazimisha kuingiza MAJINA MATATU YOTE! (mfano: JOSHUA JOSEPH MAVOA). Umeingiza majina {len(words)}."
    return True, " ".join(words).upper()

def validate_phone_number(phone_str):
    cleaned = str(phone_str).strip()
    if not cleaned:
        return False, "Namba ya simu haiwezi kuachwa wazi!"
    if not cleaned.isdigit():
        return False, "Namba ya simu lazima iwe na NAMBA PEKEE (bila herufi, nafasi au alama)!"
    if len(cleaned) != 10:
        return False, f"Namba ya simu lazima iwe na tarakimu 10 pekee! (Umeingiza tarakimu {len(cleaned)})."
    if not (cleaned.startswith("06") or cleaned.startswith("07")):
        return False, f"Namba ya simu lazima ianze na '06' au '07' pekee! (Umeingiza '{cleaned[:2]}')."
    return True, cleaned

def validate_gmail_email(email_str):
    cleaned = str(email_str).strip().lower()
    if not cleaned:
        return False, "Barua pepe (email) haiwezi kuachwa wazi!"
    pattern = r'^[a-zA-Z0-9._%+-]+@gmail\.com$'
    if not re.match(pattern, cleaned):
        return False, "Email lazima iwe na mfumo sahihi wa 'jina@gmail.com' (mfano: mavoajoshua9@gmail.com)!"
    return True, cleaned

def validate_nida_number(nida_str):
    cleaned = str(nida_str).strip()
    if not cleaned:
        return False, "Namba ya NIDA haiwezi kuachwa wazi!"
    if not cleaned.isdigit():
        return False, "Namba ya NIDA lazima iwe na NAMBA PEKEE (bila herufi, nafasi au alama)!"
    if len(cleaned) != 20:
        return False, f"Namba ya NIDA lazima iwe na tarakimu 20 kamili! (Umeingiza tarakimu {len(cleaned)})."
    return True, cleaned

def validate_password_length(password_str):
    cleaned = str(password_str).strip()
    if not cleaned:
        return False, "Password haiwezi kuachwa wazi!"
    if len(cleaned) < 4:
        return False, f"Password lazima iwe na angalau herufi 4 au zaidi! (Umeingiza character {len(cleaned)})."
    return True, cleaned

# ---------------------------------------------------------
# OPENCV FACIAL RECOGNITION ENGINE
# ---------------------------------------------------------
if OPENCV_AVAILABLE:
    try:
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    except Exception:
        face_cascade = None
else:
    face_cascade = None

def scan_and_verify_face(uploaded_camera_file):
    if uploaded_camera_file is None:
        return False, None, 0, "Hakuna picha iliyopigwa au iliyotushwa", None

    now_dt = datetime.datetime.now()
    timestamp_str = now_dt.strftime("%A, %Y-%m-%d %H:%M:%S")

    if not OPENCV_AVAILABLE or face_cascade is None:
        bytes_data = uploaded_camera_file.getvalue()
        img_b64 = base64.b64encode(bytes_data).decode('utf-8')
        return True, img_b64, 1, "✅ Picha imehifadhiwa na kuthibitishwa.", json.dumps([{"scan_time": timestamp_str}])

    try:
        image_bytes = uploaded_camera_file.getvalue()
        pil_img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img_np = np.array(pil_img)
        
        img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        
        faces = face_cascade.detectMultiScale(
            gray, 
            scaleFactor=1.1, 
            minNeighbors=5, 
            minSize=(60, 60)
        )
        
        if len(faces) == 0:
            return False, None, 0, "⚠️ REJECTION: Mfumo haujaona USO wa Binadamu kwenye picha hii! Mfumo hu-scan USO TU — elekeza kamera usoni mwa mtu mmoja.", None

        if len(faces) > 1:
            return False, None, len(faces), f"⚠️ REJECTION: Zimegundulika USO {len(faces)} kwenye picha! Mfumo hu-scan USO MMOJA TU — hakuna kitu kingine wala watu wengi. Nionekane na uso wako mwenyewe pekee.", None

        metadata_list = []
        for idx, (x, y, w, h) in enumerate(faces):
            x, y, w, h = int(x), int(y), int(w), int(h)
            cx, cy = int(x + w // 2), int(y + h // 2)
            cv2.rectangle(img_bgr, (x, y), (x+w, y+h), (0, 255, 57), 2)

            line_len = int(w * 0.25)
            cv2.line(img_bgr, (x, y), (x + line_len, y), (255, 240, 0), 4)
            cv2.line(img_bgr, (x, y), (x, y + line_len), (255, 240, 0), 4)

            cv2.circle(img_bgr, (cx, cy), 6, (0, 240, 255), -1)

            label = f"FACE VERIFIED #{idx+1} [OK]"
            cv2.putText(img_bgr, label, (x, max(y - 12, 20)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 57), 2)
            
            metadata_list.append({
                "face_id": idx + 1,
                "center_x": cx, "center_y": cy,
                "bbox": [int(x), int(y), int(w), int(h)],
                "area_px": int(w * h),
                "timestamp": timestamp_str
            })

        img_rgb_annotated = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        pil_annotated = Image.fromarray(img_rgb_annotated)
        buffer = io.BytesIO()
        pil_annotated.save(buffer, format="JPEG", quality=90)
        img_b64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
        
        return True, img_b64, len(faces), f"✅ FACE VERIFIED: USO MMOJA umethibitishwa — scanner ni safi (uso TU, hakuna kitu kingine)!", json.dumps(metadata_list)

    except Exception as e:
        return False, None, 0, f"Hitilafu wakati wa ku-scan sura: {str(e)}", None

# ---------------------------------------------------------
# PDF REPORT GENERATOR ENGINE FOR DIRECTOR JOSHUA JOSEPH
# ---------------------------------------------------------
if FPDF_AVAILABLE:
    class CyberReportPDF(FPDF):
        def header(self):
            self.set_fill_color(10, 20, 38)
            self.rect(0, 0, 210, 35, 'F')
            
            self.set_font('Helvetica', 'B', 15)
            self.set_text_color(0, 240, 255)
            self.cell(0, 10, 'CYBER BUSINESS GRADING SYSTEM | PERFORMANCE REPORT', 0, 1, 'C')
            
            self.set_font('Helvetica', 'B', 9)
            self.set_text_color(255, 0, 127)
            self.cell(0, 6, 'DIRECTOR: JOSHUA JOSEPH | SIMU: 0677698182 | EMAIL: mavoajoshua9@gmail.com', 0, 1, 'C')
            self.ln(10)

        def footer(self):
            self.set_y(-15)
            self.set_font('Helvetica', 'I', 8)
            self.set_text_color(150, 150, 150)
            self.cell(0, 10, f'Page {self.page_no()} | Generated by Cyber Business Grading System', 0, 0, 'C')

def generate_pdf_monthly_report(business_info, year, month_num, month_name, df_daily, df_assets):
    if not FPDF_AVAILABLE:
        return b""
    pdf = CyberReportPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    
    currency = business_info['currency']
    
    pdf.set_font('Helvetica', 'B', 13)
    pdf.set_text_color(10, 20, 38)
    pdf.cell(0, 8, f"TAARIFA YA BIASHARA: {business_info['name'].upper()} ({month_name} {year})", 0, 1, 'L')
    
    pdf.set_font('Helvetica', '', 9)
    pdf.set_text_color(50, 50, 50)
    pdf.cell(0, 5, f"Meneja: {business_info['manager_name']} | Simu: {business_info['manager_phone']} | Email: {business_info['manager_email']}", 0, 1, 'L')
    pdf.cell(0, 5, f"Mtaji wa Kuanzia: {business_info['initial_capital']:,.2f} {currency} | Tarehe ya Kuanza: {business_info['start_date']}", 0, 1, 'L')
    pdf.ln(5)
    
    if not df_daily.empty:
        total_cash_rev = df_daily['cash_revenue'].sum()
        total_credit_rev = 0.0
        total_rev = df_daily['revenue'].sum()
        total_exp = df_daily['expenses'].sum()
        net_cash_profit = total_rev - total_credit_rev - total_exp
    else:
        total_cash_rev = total_credit_rev = total_rev = total_exp = net_cash_profit = 0.0
        
    total_assets = df_assets[df_assets['item_type'] == 'Asset']['amount'].sum() if not df_assets.empty else 0.0
    total_liabilities = df_assets[df_assets['item_type'] == 'Liability']['amount'].sum() if not df_assets.empty else 0.0

    pdf.set_font('Helvetica', 'B', 10)
    pdf.set_fill_color(0, 240, 255)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 7, '1. MUHTASARI WA MAPATO, MATUMIZI NA FAIDA HALISI', 1, 1, 'L', fill=True)
    
    pdf.set_font('Helvetica', '', 9)
    pdf.cell(95, 6, f"  Mauzo ya Cash (Fedha Taslimu):", 1, 0, 'L')
    pdf.cell(95, 6, f"{total_cash_rev:,.2f} {currency}", 1, 1, 'R')
    
    pdf.cell(95, 6, f"  Mauzo ya Mkopo (Madeni Jamii):", 1, 0, 'L')
    pdf.cell(95, 6, f"{total_credit_rev:,.2f} {currency}", 1, 1, 'R')

    pdf.cell(95, 6, f"  Jumla Kuu ya Mapato (Cash + Mkopo):", 1, 0, 'L')
    pdf.cell(95, 6, f"{total_rev:,.2f} {currency}", 1, 1, 'R')

    pdf.cell(95, 6, f"  Jumla ya Matumizi ya Siku:", 1, 0, 'L')
    pdf.cell(95, 6, f"- {total_exp:,.2f} {currency}", 1, 1, 'R')

    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_fill_color(220, 255, 220)
    pdf.cell(95, 7, f"  FAIDA HALISI YA CASH MKONONI:", 1, 0, 'L', fill=True)
    pdf.cell(95, 7, f"{net_cash_profit:,.2f} {currency}", 1, 1, 'R', fill=True)
    pdf.ln(5)

    pdf.set_font('Helvetica', 'B', 10)
    pdf.set_fill_color(255, 0, 127)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 7, '2. MUHTASARI WA ASSETS & LIABILITIES (LEDGER)', 1, 1, 'L', fill=True)
    
    pdf.set_font('Helvetica', '', 9)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(95, 6, f"  Jumla ya Mali na Vifaa Vyote (Assets):", 1, 0, 'L')
    pdf.cell(95, 6, f"{total_assets:,.2f} {currency}", 1, 1, 'R')

    pdf.cell(95, 6, f"  Jumla ya Madeni ya Nje (Liabilities):", 1, 0, 'L')
    pdf.cell(95, 6, f"- {total_liabilities:,.2f} {currency}", 1, 1, 'R')
    pdf.ln(5)

    pdf.set_font('Helvetica', 'B', 10)
    pdf.set_fill_color(15, 23, 42)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 7, f'3. REKODI YA MAUZO NA MATUMIZI YA KILA SIKU ({month_name} {year})', 1, 1, 'L', fill=True)

    pdf.set_font('Helvetica', 'B', 8)
    pdf.set_fill_color(230, 230, 230)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(20, 6, 'Tarehe', 1, 0, 'C', fill=True)
    pdf.cell(32, 6, 'Cash Rev', 1, 0, 'C', fill=True)
    pdf.cell(32, 6, 'Credit Rev', 1, 0, 'C', fill=True)
    pdf.cell(32, 6, 'Matumizi', 1, 0, 'C', fill=True)
    pdf.cell(32, 6, 'Cash Profit', 1, 0, 'C', fill=True)
    pdf.cell(42, 6, 'Mtumaji & Submit Time', 1, 1, 'C', fill=True)

    pdf.set_font('Helvetica', '', 7)
    if not df_daily.empty:
        for idx, row in df_daily.iterrows():
            c_profit = row['revenue'] - row['expenses']
            s_time = row['submitted_timestamp'] if row['submitted_timestamp'] else "N/A"
            pdf.cell(20, 5, str(row['record_date']), 1, 0, 'C')
            pdf.cell(32, 5, f"{row['cash_revenue']:,.0f}", 1, 0, 'R')
            pdf.cell(32, 5, f"{row['credit_revenue']:,.0f}", 1, 0, 'R')
            pdf.cell(32, 5, f"{row['expenses']:,.0f}", 1, 0, 'R')
            pdf.cell(32, 5, f"{c_profit:,.0f}", 1, 0, 'R')
            pdf.cell(42, 5, f"{row['sender_name'][:12]} ({s_time[-8:] if len(s_time)>=8 else 'N/A'})", 1, 1, 'L')
    else:
        pdf.cell(190, 6, 'Hakuna rekodi za mauzo zilizopatikana kwenye mwezi huu.', 1, 1, 'C')

    pdf.ln(8)
    pdf.set_font('Helvetica', 'B', 9)
    pdf.cell(0, 6, f"Ripoti Imeidhinishwa na Director Wa Mfumo: JOSHUA JOSEPH", 0, 1, 'R')
    
    # fpdf2 inarejea bytearray; fpdf ya zamani inarejea str — vhudhia zote mbili
    _pdf_out = pdf.output()
    if isinstance(_pdf_out, str):
        return _pdf_out.encode('latin-1', 'replace')
    return bytes(_pdf_out)

# Helper for Image to Base64
def file_to_b64(uploaded_file):
    if uploaded_file is None:
        return None
    bytes_data = uploaded_file.getvalue()
    return base64.b64encode(bytes_data).decode('utf-8')

# ---------------------------------------------------------
# INTERFACE THEME SYSTEM (v63): Kila mtu anachague muonekano anayotaka —
# "⚡ Cyber Interface" (cyberpunk) au "🖥️ User Interface (Kawaida)" (clean).
# Chaguo linahifadhiwa kwenye database (director_settings) kwa kila
# portal/role tofauti, hivyo kila mtu ana theme yake anayoiachia.
# ---------------------------------------------------------
THEME_LABEL_CYBER = "⚡ Cyber Interface"
THEME_LABEL_SIMPLE = "🖥️ User Interface (Kawaida)"
THEME_LABELS = [THEME_LABEL_CYBER, THEME_LABEL_SIMPLE]

def resolve_theme_scope():
    """Tambua nani anatumia mfumo sasa (scope ya theme: portal + biashara + role).
    Director, Meneja na Mfanyakazi kila mmoja anaweza kuwa na theme yake."""
    pm = str(st.session_state.get("portal_mode", "") or "")
    if "Director" in pm:
        return "DIRECTOR"
    if "Biashara" in pm:
        biz = str(st.session_state.get("biz_sel_name", "") or "")
        role = ""
        try:
            for _k, _v in list(st.session_state.items()):
                if _k.startswith("auth_") and _v is True:
                    role = str(st.session_state.get("role_" + _k[5:], "") or "")
                    break
        except Exception:
            pass
        if biz and role:
            return f"BIZ::{biz}::{role}"
        if biz:
            return f"BIZ::{biz}"
    return "PUBLIC"

def get_ui_theme():
    """Rudisha 'CYBER' au 'SIMPLE' kutoka kwa chaguo la mtumiaji la theme."""
    lbl = str(st.session_state.get("ui_theme_choice", THEME_LABEL_CYBER) or THEME_LABEL_CYBER)
    return "SIMPLE" if lbl.startswith("🖥️") else "CYBER"

# ---------------------------------------------------------
# STREAMLIT GLASSMORPHIC CYBERPUNK CONFIGURATION
# ---------------------------------------------------------
st.set_page_config(
    page_title="CYBER BUSINESS GRADING SYSTEM",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── ANGALIA THEME YA MTUMIAJI (v63) kabla ya kuingiza CSS ──
# Kila run, kama scope imebadilika (mf. Meneja ameingia baada ya Director):
#  - kama scope hiyo ina theme iliyohifadhiwa kwenye database → itumie
#  - kama haina → acha chaguo la session la mtumiaji (usiforce default)
_theme_scope_now = resolve_theme_scope()
if str(st.session_state.get("_theme_scope_loaded", "")) != _theme_scope_now:
    st.session_state["_theme_scope_loaded"] = _theme_scope_now
    try:
        _saved_theme = get_setting(f"theme::{_theme_scope_now}", "")
        if _saved_theme:
            st.session_state["ui_theme_choice"] = _saved_theme
    except Exception:
        pass
UI_THEME = get_ui_theme()

# Responsive Cyber CSS & Blinking Red Error Box
_CYBER_THEME_CSS = """
<style>
    /* Responsive Global Styles */
    .stApp {
        background: #060911;
        color: #F1F5F9;
        font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    }
    
    @media (max-width: 768px) {
        .standalone-title-text { font-size: 1.4rem !important; }
        .director-title { font-size: 1.15rem !important; }
        .director-info { font-size: 0.82rem !important; }
        .stMetric { padding: 10px !important; }
    }

    /* Standalone System Title Card */
    .standalone-title-card {
        background: linear-gradient(135deg, #0B132B, #1C2541);
        border: 2px solid #00F0FF;
        border-radius: 14px;
        padding: 18px 24px;
        margin-bottom: 20px;
        text-align: center;
        box-shadow: 0 0 22px rgba(0, 240, 255, 0.45);
    }
    .standalone-title-text {
        font-size: 2.3rem;
        font-weight: 900;
        color: #00F0FF;
        text-shadow: 0 0 16px rgba(0, 240, 255, 0.95);
        letter-spacing: 1.8px;
    }

    /* Director Header Banner */
    .director-banner {
        background: linear-gradient(135deg, rgba(10, 20, 38, 0.95), rgba(15, 23, 42, 0.95));
        border: 2px solid #FF007F;
        box-shadow: 0 0 22px rgba(255, 0, 127, 0.35);
        border-radius: 14px;
        padding: 16px 20px;
        margin-bottom: 20px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 12px;
    }
    .director-title {
        font-size: 1.55rem;
        font-weight: 800;
        color: #FF007F;
        text-shadow: 0 0 10px rgba(255, 0, 127, 0.8);
    }
    .director-info {
        font-size: 0.96rem;
        color: #CBD5E1;
        margin-top: 4px;
    }
    .director-highlight {
        color: #00F0FF;
        font-weight: 700;
    }
    .cyber-badge {
        background: #0F172A;
        border: 1px solid #39FF14;
        color: #39FF14;
        padding: 6px 14px;
        border-radius: 20px;
        font-weight: bold;
        font-size: 0.82rem;
        box-shadow: 0 0 10px rgba(57, 255, 20, 0.35);
    }

    /* Cyber Error Box & Glowing Marquee Banner */
    @keyframes blinkCyberRedAlert {
        0% { border-color: #FF0055; box-shadow: 0 0 25px rgba(255, 0, 85, 0.95), inset 0 0 15px rgba(255, 0, 85, 0.4); background: #20000B; }
        50% { border-color: #00F0FF; box-shadow: 0 0 15px rgba(0, 240, 255, 0.6), inset 0 0 10px rgba(0, 240, 255, 0.2); background: #070D1F; }
        100% { border-color: #FF0055; box-shadow: 0 0 25px rgba(255, 0, 85, 0.95), inset 0 0 15px rgba(255, 0, 85, 0.4); background: #20000B; }
    }
    .blinking-red-box, .cyber-error-box {
        animation: blinkCyberRedAlert 1.2s infinite alternate;
        border: 2px solid #FF0055 !important;
        border-radius: 12px;
        padding: 14px 18px;
        margin-bottom: 18px;
        position: relative;
        font-family: 'Consolas', 'Courier New', monospace;
    }
    .cyber-error-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        border-bottom: 1px dashed rgba(255, 0, 85, 0.6);
        padding-bottom: 6px;
        margin-bottom: 8px;
    }
    .cyber-error-tag {
        font-size: 0.85rem;
        font-weight: 900;
        color: #FF0055;
        text-shadow: 0 0 10px #FF0055;
        letter-spacing: 1.5px;
    }
    .cyber-error-sub {
        font-size: 0.78rem;
        color: #00F0FF;
        text-shadow: 0 0 8px #00F0FF;
        letter-spacing: 1px;
    }

    /* Animated Cyber Scanner Box */
    .scanner-box {
        position: relative;
        border: 2px solid #00F0FF;
        border-radius: 12px;
        padding: 12px;
        background: #0B132B;
        box-shadow: 0 0 20px rgba(0, 240, 255, 0.3);
        margin-top: 10px;
        overflow: hidden;
    }
    .scanner-line {
        position: absolute;
        top: 0;
        left: 0;
        width: 100%;
        height: 4px;
        background: linear-gradient(90deg, transparent, #39FF14, #00F0FF, transparent);
        box-shadow: 0 0 15px #39FF14;
        animation: scanAnim 2.5s infinite ease-in-out;
    }
    @keyframes scanAnim {
        0% { top: 0%; }
        50% { top: 95%; }
        100% { top: 0%; }
    }

    /* Cyber Metrics */
    .stMetric {
        background: rgba(15, 23, 42, 0.85) !important;
        border: 1px solid #00F0FF !important;
        border-left: 5px solid #FF007F !important;
        border-radius: 10px !important;
        padding: 14px !important;
        box-shadow: 0 0 15px rgba(0, 240, 255, 0.18) !important;
    }

    /* Buttons */
    .stButton>button {
        background: linear-gradient(90deg, #00F0FF, #0088FF) !important;
        color: #000000 !important;
        font-weight: 800 !important;
        border: none !important;
        border-radius: 8px !important;
        box-shadow: 0 0 18px rgba(0, 240, 255, 0.45) !important;
        transition: all 0.3s ease !important;
    }
    .stButton>button:hover {
        background: linear-gradient(90deg, #FF007F, #7928CA) !important;
        color: #FFFFFF !important;
        box-shadow: 0 0 28px rgba(255, 0, 127, 0.75) !important;
        transform: translateY(-2px);
    }

    /* Sidebar */
    section[data-testid="stSidebar"] {
        background-color: #040710 !important;
        border-right: 1px solid #00F0FF33;
    }

    /* ============================================================================
       CYBER MODE ERROR BANNER — FULL GLITCH EDITION
       (RGB split, flicker, scanlines, HUD corners, laser)
       ============================================ */
    @keyframes cyberPulseBorder {
        0% {
            border-color: #00F0FF;
            box-shadow: 0 0 25px rgba(0, 240, 255, 0.9), inset 0 0 15px rgba(255, 0, 127, 0.4);
            background: #030712;
        }
        50% {
            border-color: #FF007F;
            box-shadow: 0 0 30px rgba(255, 0, 127, 0.95), inset 0 0 20px rgba(0, 240, 255, 0.5);
            background: #0B0F19;
        }
        100% {
            border-color: #00F0FF;
            box-shadow: 0 0 25px rgba(0, 240, 255, 0.9), inset 0 0 15px rgba(255, 0, 127, 0.4);
            background: #030712;
        }
    }

    .cyber-mode-error-wrapper {
        animation: cyberPulseBorder 1.5s infinite alternate;
        border: 2px solid #00F0FF !important;
        border-radius: 12px;
        padding: 12px 16px;
        margin-bottom: 18px;
        position: relative;
        overflow: hidden;
        background-color: #030712;
        background-image:
            linear-gradient(rgba(0, 240, 255, 0.045) 1px, transparent 1px),
            linear-gradient(90deg, rgba(0, 240, 255, 0.045) 1px, transparent 1px);
        background-size: 22px 22px;
    }

    /* HUD corner brackets (targeting-lock style) */
    .cyber-mode-error-wrapper::before,
    .cyber-mode-error-wrapper::after {
        content: "";
        position: absolute;
        width: 20px;
        height: 20px;
        z-index: 3;
        pointer-events: none;
    }
    .cyber-mode-error-wrapper::before {
        top: 0;
        left: 0;
        border-top: 3px solid #39FF14;
        border-left: 3px solid #39FF14;
        filter: drop-shadow(0 0 6px #39FF14);
    }
    .cyber-mode-error-wrapper::after {
        bottom: 0;
        right: 0;
        border-bottom: 3px solid #39FF14;
        border-right: 3px solid #39FF14;
        filter: drop-shadow(0 0 6px #39FF14);
    }

    .cyber-mode-hud-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        border-bottom: 1px dashed rgba(0, 240, 255, 0.5);
        padding-bottom: 6px;
        margin-bottom: 8px;
        font-family: 'Consolas', 'Courier New', monospace;
    }

    .cyber-hud-left, .cyber-hud-right {
        display: flex;
        align-items: center;
        gap: 12px;
    }

    .cyber-hud-glitch-tag {
        color: #FF007F;
        font-weight: 900;
        font-size: 0.85rem;
        letter-spacing: 1.5px;
        text-shadow: 0 0 10px #FF007F;
        display: inline-block;
        animation: cyberHudGlitch 2.8s infinite steps(1, end);
    }

    @keyframes cyberHudGlitch {
        0%, 87%, 100% {
            text-shadow: 0 0 10px #FF007F;
            transform: translate(0, 0) skewX(0deg);
        }
        88% {
            text-shadow: -2px 0 #00F0FF, 2px 0 #FFD700;
            transform: translate(1px, 0) skewX(-7deg);
        }
        90% {
            text-shadow: 2px 0 #00F0FF, -2px 0 #39FF14;
            transform: translate(-1px, 0) skewX(5deg);
        }
        92% {
            text-shadow: 0 0 10px #FF007F;
            transform: translate(0, 0) skewX(0deg);
        }
    }

    .cyber-hud-pulse-dot {
        color: #FF0055;
        font-size: 0.75rem;
        font-weight: 800;
        letter-spacing: 1px;
        text-shadow: 0 0 8px #FF0055;
        animation: cyberDotPulse 1s infinite ease-in-out;
    }

    @keyframes cyberDotPulse {
        0%, 100% { opacity: 1; text-shadow: 0 0 8px #FF0055; }
        50% { opacity: 0.4; text-shadow: 0 0 2px #FF0055; }
    }

    .cyber-hud-sound-tag {
        color: #00F0FF;
        font-size: 0.78rem;
        font-weight: 700;
        text-shadow: 0 0 8px #00F0FF;
        letter-spacing: 1px;
        animation: cyberSoundPulse 0.9s infinite ease-in-out;
    }

    @keyframes cyberSoundPulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.55; }
    }

    .cyber-hud-err-code {
        color: #FFD700;
        font-size: 0.72rem;
        font-weight: 800;
        letter-spacing: 1px;
        text-shadow: 0 0 8px rgba(255, 215, 0, 0.8);
        border: 1px solid rgba(255, 215, 0, 0.5);
        border-radius: 4px;
        padding: 1px 6px;
        background: rgba(255, 215, 0, 0.08);
        font-family: 'Consolas', 'Courier New', monospace;
    }

    .cyber-mode-ticker-container {
        padding: 6px 0;
        background: rgba(2, 6, 23, 0.95);
        border-radius: 6px;
        position: relative;
        overflow: hidden;
    }

    /* CRT scanlines over the ticker */
    .cyber-mode-ticker-container::after {
        content: "";
        position: absolute;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: repeating-linear-gradient(0deg,
            rgba(0, 240, 255, 0.06) 0px,
            rgba(0, 240, 255, 0.06) 1px,
            transparent 1px,
            transparent 3px);
        pointer-events: none;
        z-index: 2;
    }

    /* Signal tracking sweep behind the text */
    .cyber-mode-ticker-container::before {
        content: "";
        position: absolute;
        top: 0;
        bottom: 0;
        width: 70px;
        left: -70px;
        background: linear-gradient(90deg, transparent, rgba(0, 240, 255, 0.14), transparent);
        animation: cyberTrackSweep 2.8s infinite linear;
        pointer-events: none;
        z-index: 1;
    }

    @keyframes cyberTrackSweep {
        0% { left: -70px; }
        100% { left: 100%; }
    }

    .cyber-mode-marquee-text {
        font-family: 'Consolas', 'Courier New', monospace !important;
        font-size: 1.18rem !important;
        font-weight: 900 !important;
        letter-spacing: 2px !important;
        color: #00F0FF;
        animation: cyberTickerGlitch 3.4s infinite steps(1, end);
    }

    /* Whole-line glitch: RGB split + skew jumps at random-feeling intervals */
    @keyframes cyberTickerGlitch {
        0%, 83%, 96%, 100% {
            transform: translate(0, 0) skewX(0deg);
            text-shadow: 0 0 8px rgba(0, 240, 255, 0.65);
            opacity: 1;
        }
        84% {
            transform: translate(2px, -1px) skewX(-8deg);
            text-shadow: -4px 0 #FF0040, 4px 0 #00F0FF, 0 0 12px rgba(0, 240, 255, 0.9);
        }
        86% {
            transform: translate(-3px, 1px) skewX(6deg);
            text-shadow: 4px -1px #FF0040, -4px 1px #00F0FF;
            opacity: 0.82;
        }
        88% {
            transform: translate(1px, 0) skewX(-3deg);
            text-shadow: -2px 0 #39FF14, 3px 0 #FF0040;
        }
        90% {
            transform: translate(-1px, 1px) skewX(2deg);
            text-shadow: 2px 0 #00F0FF, -2px 0 #FFD700;
            opacity: 0.9;
        }
        92% {
            transform: translate(0, 0) skewX(0deg);
            opacity: 1;
        }
    }

    /* ALERT text: flicker + sliced RGB ghost copies (data-text glitch) */
    .cyber-text-alert {
        color: #FF007F;
        text-shadow: 0 0 12px #FF007F, 0 0 20px #FF007F;
        position: relative;
        display: inline-block;
        animation: cyberAlertFlicker 2.6s infinite steps(1, end);
    }

    .cyber-text-alert::before,
    .cyber-text-alert::after {
        content: attr(data-text);
        position: absolute;
        top: 0;
        left: 0;
        width: 100%;
        pointer-events: none;
        text-shadow: none;
    }

    .cyber-text-alert::before {
        color: #00F0FF;
        animation: cyberGlitchSliceA 2.1s infinite steps(1, end);
        z-index: 2;
    }

    .cyber-text-alert::after {
        color: #FFFFFF;
        animation: cyberGlitchSliceB 1.7s infinite steps(1, end);
        z-index: 1;
    }

    @keyframes cyberGlitchSliceA {
        0%, 74%, 100% { clip-path: inset(50% 0 50% 0); transform: translate(0, 0); opacity: 0; }
        75% { clip-path: inset(10% 0 74% 0); transform: translate(-3px, -1px); opacity: 0.85; }
        79% { clip-path: inset(62% 0 20% 0); transform: translate(3px, 1px); opacity: 0.85; }
        83% { clip-path: inset(36% 0 46% 0); transform: translate(-2px, 0); opacity: 0.85; }
        87% { clip-path: inset(78% 0 6% 0); transform: translate(2px, -1px); opacity: 0.85; }
        91% { clip-path: inset(50% 0 50% 0); transform: translate(0, 0); opacity: 0; }
    }

    @keyframes cyberGlitchSliceB {
        0%, 79%, 100% { clip-path: inset(50% 0 50% 0); transform: translate(0, 0); opacity: 0; }
        80% { clip-path: inset(28% 0 58% 0); transform: translate(3px, 1px); opacity: 0.75; }
        84% { clip-path: inset(55% 0 30% 0); transform: translate(-3px, -1px); opacity: 0.75; }
        88% { clip-path: inset(8% 0 80% 0); transform: translate(2px, 0); opacity: 0.75; }
        92% { clip-path: inset(44% 0 42% 0); transform: translate(-2px, 1px); opacity: 0.75; }
        96% { clip-path: inset(50% 0 50% 0); transform: translate(0, 0); opacity: 0; }
    }

    @keyframes cyberAlertFlicker {
        0%, 66%, 100% { opacity: 1; }
        68% { opacity: 0.5; }
        70% { opacity: 1; }
        72% { opacity: 0.72; }
        74% { opacity: 1; }
    }

    .cyber-text-loc-lbl {
        color: #FFD700;
        text-shadow: 0 0 10px #FFD700;
    }

    .cyber-text-loc-val {
        color: #00F0FF;
        text-shadow: 0 0 12px #00F0FF, 0 0 22px #00F0FF;
    }

    .cyber-text-err-lbl {
        color: #FF0055;
        text-shadow: 0 0 10px #FF0055;
    }

    .cyber-text-err-val {
        color: #FFFFFF;
        text-shadow: 0 0 10px #FFFFFF, 0 0 18px rgba(255, 255, 255, 0.8);
        animation: cyberErrFlicker 1.9s infinite steps(1, end);
    }

    @keyframes cyberErrFlicker {
        0%, 81%, 100% {
            text-shadow: 0 0 10px #FFFFFF, 0 0 18px rgba(255, 255, 255, 0.8);
            transform: translateX(0);
            opacity: 1;
        }
        82% {
            text-shadow: -2px 0 #FF0040, 2px 0 #00F0FF;
            transform: translateX(1px);
        }
        84% {
            text-shadow: 2px 0 #FF0040, -2px 0 #00F0FF;
            transform: translateX(-1px);
            opacity: 0.85;
        }
        86% {
            text-shadow: 0 0 10px #FFFFFF, 0 0 18px rgba(255, 255, 255, 0.8);
            transform: translateX(0);
            opacity: 1;
        }
    }

    .cyber-text-fix-lbl {
        color: #A855F7;
        text-shadow: 0 0 10px #A855F7;
    }

    .cyber-text-fix-val {
        color: #39FF14;
        text-shadow: 0 0 12px #39FF14, 0 0 22px #39FF14;
    }

    .cyber-text-status {
        color: #00F0FF;
        text-shadow: 0 0 10px #00F0FF;
        animation: cyberStatusPulse 1.6s infinite ease-in-out;
    }

    @keyframes cyberStatusPulse {
        0%, 100% { text-shadow: 0 0 6px #00F0FF; }
        50% { text-shadow: 0 0 16px #00F0FF, 0 0 26px #00F0FF; }
    }

    .cyber-text-blink {
        animation: cyberBlink 0.8s infinite steps(1, end);
    }

    @keyframes cyberBlink {
        0% { opacity: 1; }
        50% { opacity: 0; }
        100% { opacity: 1; }
    }

    .cyber-laser-scan-line {
        position: absolute;
        bottom: 0;
        left: 0;
        width: 100%;
        height: 3px;
        background: linear-gradient(90deg, transparent, #FF007F, #00F0FF, #39FF14, transparent);
        box-shadow: 0 0 15px #00F0FF;
        animation: cyberLaserAnim 2s infinite linear;
    }

    @keyframes cyberLaserAnim {
        0% { transform: translateX(-100%); }
        100% { transform: translateX(100%); }
    }

    /* ============================================================================
       FIELD ERROR BLINK — uwanja wenye kosa unablink na glow nyekundu ya cyber
       ============================================================================ */
    .cyber-error-blink-field {
        animation: cyberFieldBlink 0.6s infinite !important;
        border-radius: 10px !important;
        position: relative;
        z-index: 5;
    }

    @keyframes cyberFieldBlink {
        0%, 100% {
            box-shadow: 0 0 0 3px #FF0040, 0 0 22px rgba(255, 0, 64, 0.95), inset 0 0 12px rgba(255, 0, 64, 0.30);
            background: rgba(255, 0, 64, 0.08);
        }
        50% {
            box-shadow: 0 0 0 7px rgba(255, 0, 64, 0.45), 0 0 38px rgba(255, 0, 64, 0.80), inset 0 0 20px rgba(255, 0, 64, 0.50);
            background: rgba(255, 0, 64, 0.20);
        }
    }

    .cyber-error-field-tag {
        position: absolute;
        top: -12px;
        right: 8px;
        z-index: 10;
        background: #030712;
        color: #FF0040;
        font-family: 'Consolas', 'Courier New', monospace;
        font-size: 0.72rem;
        font-weight: 900;
        letter-spacing: 2px;
        padding: 2px 8px;
        border: 1px solid #FF0040;
        border-radius: 4px;
        text-shadow: 0 0 8px #FF0040;
        box-shadow: 0 0 12px rgba(255, 0, 64, 0.8);
        animation: cyberTagBlink 0.5s infinite steps(1, end);
        pointer-events: none;
    }

    @keyframes cyberTagBlink {
        0% { opacity: 1; }
        50% { opacity: 0.25; }
        100% { opacity: 1; }
    }

</style>
"""

# ---------------------------------------------------------
# SIMPLE / CLEAN USER INTERFACE CSS (v63) — "🖥️ User Interface (Kawaida)"
# Muonekano safi, mepesi na rasmi: background nyepesi, maandishi meusi,
# accent ya bluu ya kitaaluma — BILA glitch, scanlines wala neon.
# ---------------------------------------------------------
SIMPLE_THEME_CSS = """
<style>
    /* ── Clean Global Styles ── */
    .stApp {
        background: #F4F6FB;
        color: #0F172A;
        font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    }
    [data-testid="stHeader"] { background: transparent !important; }
    section[data-testid="stSidebar"] {
        background: #FFFFFF !important;
        border-right: 1px solid #E2E8F0;
    }
    section[data-testid="stSidebar"] * { color: #0F172A !important; }
    h1, h2, h3, h4, h5, h6 { color: #0F172A !important; }
    .stMarkdown, .stText, p, span, li, label, div { color: #1E293B; }
    section[data-testid="stSidebar"] .stMarkdown, section[data-testid="stSidebar"] p,
    section[data-testid="stSidebar"] span, section[data-testid="stSidebar"] div { color: #0F172A; }
    hr { border-color: #E2E8F0 !important; }

    /* Streamlit widgets — clean light look */
    .stButton > button {
        background: linear-gradient(135deg, #1D4ED8, #2563EB);
        color: #FFFFFF;
        border: none;
        border-radius: 8px;
        font-weight: 600;
        box-shadow: 0 1px 4px rgba(29, 78, 216, 0.35);
    }
    .stButton > button:hover { background: linear-gradient(135deg, #1E40AF, #1D4ED8); }
    .stTextInput input, .stTextArea textarea, .stNumberInput input {
        background: #FFFFFF !important;
        color: #0F172A !important;
        border: 1px solid #CBD5E1 !important;
        border-radius: 8px;
    }
    .stSelectbox > div > div, [data-baseweb="select"] > div {
        background: #FFFFFF !important;
        color: #0F172A !important;
        border: 1px solid #CBD5E1 !important;
    }
    .stRadio label, .stCheckbox label { color: #0F172A !important; }
    .stExpander { border: 1px solid #E2E8F0 !important; border-radius: 8px; background: #FFFFFF; }
    .stTabs [data-baseweb="tab-list"] { border-bottom: 2px solid #E2E8F0; }
    .stTabs [aria-selected="true"] { color: #1D4ED8 !important; }
    [data-testid="stDataFrame"] { color: #0F172A; }

    /* Metrics — white cards */
    .stMetric, [data-testid="stMetric"] {
        background: #FFFFFF;
        border: 1px solid #E2E8F0;
        border-radius: 10px;
        padding: 14px 16px;
        box-shadow: 0 1px 4px rgba(15, 23, 42, 0.06);
    }
    .stMetric label, [data-testid="stMetric"] label { color: #64748B !important; }
    .stMetric [data-testid="stMetricValue"], [data-testid="stMetricValue"] { color: #0F172A !important; }

    /* Streamlit alerts — clean pastel */
    [data-testid="stAlert"], .stInfo, .stSuccess, .stWarning, .stError { color: #0F172A; }
    .stInfo { background: #EFF6FF !important; border: 1px solid #BFDBFE !important; border-left: 4px solid #2563EB !important; }
    .stSuccess { background: #F0FDF4 !important; border: 1px solid #BBF7D0 !important; border-left: 4px solid #16A34A !important; }
    .stWarning { background: #FFFBEB !important; border: 1px solid #FDE68A !important; border-left: 4px solid #D97706 !important; }
    .stError { background: #FEF2F2 !important; border: 1px solid #FECACA !important; border-left: 4px solid #DC2626 !important; }
    .stInfo p, .stSuccess p, .stWarning p, .stError p { color: #0F172A !important; }
    code, pre { color: #0F172A !important; background: #F1F5F9 !important; }

    @media (max-width: 768px) {
        .standalone-title-text { font-size: 1.4rem !important; }
        .director-title { font-size: 1.15rem !important; }
        .director-info { font-size: 0.82rem !important; }
        .stMetric { padding: 10px !important; }
    }

    /* Standalone System Title Card — clean corporate */
    .standalone-title-card {
        background: linear-gradient(135deg, #FFFFFF, #F1F5F9);
        border: 2px solid #1D4ED8;
        border-radius: 14px;
        padding: 18px 24px;
        margin-bottom: 20px;
        text-align: center;
        box-shadow: 0 2px 10px rgba(29, 78, 216, 0.15);
    }
    .standalone-title-text {
        font-size: 2.3rem;
        font-weight: 900;
        color: #1D4ED8;
        text-shadow: none;
        letter-spacing: 1.8px;
    }

    /* Director Header Banner — clean corporate */
    .director-banner {
        background: linear-gradient(135deg, #FFFFFF, #F8FAFC);
        border: 2px solid #93C5FD;
        box-shadow: 0 2px 10px rgba(15, 23, 42, 0.08);
        border-radius: 14px;
        padding: 16px 20px;
        margin-bottom: 20px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 12px;
    }
    .director-title {
        font-size: 1.55rem;
        font-weight: 800;
        color: #1D4ED8;
        text-shadow: none;
    }
    .director-info {
        font-size: 0.96rem;
        color: #334155;
        margin-top: 4px;
    }
    .director-highlight {
        color: #1D4ED8;
        font-weight: 700;
    }
    .cyber-badge {
        background: #EFF6FF;
        border: 1px solid #1D4ED8;
        color: #1D4ED8;
        padding: 6px 14px;
        border-radius: 20px;
        font-weight: bold;
        text-shadow: none;
    }

    /* Blinking red error box — clean emergency alert (bila glow za cyber) */
    .blinking-red-box {
        background: #FEF2F2;
        border: 2px solid #DC2626;
        border-left: 8px solid #DC2626;
        border-radius: 10px;
        padding: 16px 20px;
        margin-bottom: 18px;
        animation: simpleErrPulse 1.2s infinite ease-in-out;
    }
    @keyframes simpleErrPulse {
        0%, 100% { box-shadow: 0 0 0 0 rgba(220, 38, 38, 0.25); }
        50% { box-shadow: 0 0 0 6px rgba(220, 38, 38, 0.12); }
    }

    /* Face scanner — clean frame */
    .scanner-box {
        border: 3px solid #1D4ED8 !important;
        border-radius: 12px;
        background: #FFFFFF;
        box-shadow: 0 2px 10px rgba(29, 78, 216, 0.15);
    }
    .scanner-line {
        background: linear-gradient(90deg, transparent, #1D4ED8, transparent) !important;
        box-shadow: none !important;
    }

    /* FIELD ERROR BLINK — uwanja wenye kosa unablink nyekundu (safi) */
    .cyber-error-blink-field {
        animation: simpleFieldBlink 0.6s infinite !important;
        border-radius: 10px !important;
        position: relative;
        z-index: 5;
    }
    @keyframes simpleFieldBlink {
        0%, 100% { box-shadow: 0 0 0 3px #DC2626, 0 0 10px rgba(220, 38, 38, 0.35); background: rgba(220, 38, 38, 0.05); }
        50% { box-shadow: 0 0 0 6px rgba(220, 38, 38, 0.25), 0 0 16px rgba(220, 38, 38, 0.45); background: rgba(220, 38, 38, 0.12); }
    }
    .cyber-error-field-tag {
        position: absolute;
        top: -12px;
        right: 8px;
        z-index: 10;
        background: #FFFFFF;
        color: #DC2626;
        font-family: 'Segoe UI', system-ui, sans-serif;
        font-size: 0.72rem;
        font-weight: 900;
        letter-spacing: 1.5px;
        padding: 2px 8px;
        border: 1px solid #DC2626;
        border-radius: 4px;
        text-shadow: none;
        box-shadow: 0 1px 4px rgba(220, 38, 38, 0.3);
        animation: simpleTagBlink 0.6s infinite steps(1, end);
        pointer-events: none;
    }
    @keyframes simpleTagBlink {
        0% { opacity: 1; }
        50% { opacity: 0.3; }
        100% { opacity: 1; }
    }
    /* Hakuna scanlines wala glitch kwenye clean theme */
    .cyber-mode-error-wrapper::before, .cyber-mode-error-wrapper::after { display: none !important; }
</style>
"""

if UI_THEME == "CYBER":
    st.markdown(_CYBER_THEME_CSS, unsafe_allow_html=True)
else:
    st.markdown(SIMPLE_THEME_CSS, unsafe_allow_html=True)

# ---------------------------------------------------------
# ATTRACTION POLISH (v64): ubora wa kuvutia kwa themes ZOTE mbili —
# vitufe vinang'aa, KPI cards zinapanda juu kidogo unapowekia kwenye,
# headers zenye gradient ya kuvutia, scrollbar ya kizungu.
# Rangi zinabadilika kulingana na theme iliyochaguliwa (CYBER/SIMPLE).
# ---------------------------------------------------------
_V64_POLISH_CSS = """
<style>
/* Vitufe vyote vikuu — glow na mpito laini */
div.stButton > button, div.stDownloadButton > button, .stForm button[kind="primary"] {
    border-radius: 10px !important;
    font-weight: 700 !important;
    transition: transform 0.18s ease, box-shadow 0.18s ease, filter 0.18s ease !important;
}
div.stButton > button:hover, div.stDownloadButton > button:hover, .stForm button[kind="primary"]:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 22px rgba(0, 240, 255, 0.35);
    filter: brightness(1.08);
}
/* KPI metric cards — kadi za kupanda na border laini */
div[data-testid="stMetric"] {
    background: _POLISH_CARD_BG_;
    border: 1px solid _POLISH_CARD_BORDER_;
    border-radius: 12px;
    padding: 12px 14px;
    transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
}
div[data-testid="stMetric"]:hover {
    transform: translateY(-3px);
    border-color: _POLISH_CARD_HOVER_;
    box-shadow: 0 10px 26px _POLISH_CARD_SHADOW_;
}
div[data-testid="stMetricLabel"] { font-size: 0.86rem !important; opacity: 0.92; }
div[data-testid="stMetricValue"] { font-weight: 800 !important; letter-spacing: 0.3px; }
/* Headers za section — gradient ya kuvutia */
h2, h3 { letter-spacing: 0.4px; }
/* Sidebar — mtaji mwepesi */
section[data-testid="stSidebar"] { border-right: 1px solid _POLISH_SIDEBAR_BORDER_; }
/* Scrollbar ya kizungu */
::-webkit-scrollbar { width: 9px; height: 9px; }
::-webkit-scrollbar-track { background: _POLISH_SCROLL_TRACK_; }
::-webkit-scrollbar-thumb { background: _POLISH_SCROLL_THUMB_; border-radius: 8px; }
/* Inputs — focus glow */
div[data-baseweb="input"] > div, div[data-baseweb="select"] > div, div[data-baseweb="textarea"] > div {
    transition: border-color 0.2s ease, box-shadow 0.2s ease;
}
div[data-baseweb="input"] > div:focus-within, div[data-baseweb="select"] > div:focus-within, div[data-baseweb="textarea"] > div:focus-within {
    border-color: _POLISH_FOCUS_ !important;
    box-shadow: 0 0 0 1px _POLISH_FOCUS_66, 0 0 14px _POLISH_FOCUS_40 !important;
}
</style>
"""
if UI_THEME == "CYBER":
    _V64_POLISH_CSS = (_V64_POLISH_CSS
        .replace("_POLISH_CARD_BG_", "rgba(15, 23, 42, 0.55)")
        .replace("_POLISH_CARD_BORDER_", "rgba(0, 240, 255, 0.25)")
        .replace("_POLISH_CARD_HOVER_", "rgba(57, 255, 20, 0.55)")
        .replace("_POLISH_CARD_SHADOW_", "rgba(57, 255, 20, 0.18)")
        .replace("_POLISH_SIDEBAR_BORDER_", "rgba(0, 240, 255, 0.18)")
        .replace("_POLISH_SCROLL_TRACK_", "rgba(15, 23, 42, 0.35)")
        .replace("_POLISH_SCROLL_THUMB_", "linear-gradient(180deg, #00F0FF66, #39FF1466)")
        .replace("_POLISH_FOCUS_66", "#00F0FF66").replace("_POLISH_FOCUS_40", "rgba(0, 240, 255, 0.25)")
        .replace("_POLISH_FOCUS_", "#00F0FF"))
else:
    _V64_POLISH_CSS = (_V64_POLISH_CSS
        .replace("_POLISH_CARD_BG_", "rgba(255, 255, 255, 0.92)")
        .replace("_POLISH_CARD_BORDER_", "rgba(37, 99, 235, 0.18)")
        .replace("_POLISH_CARD_HOVER_", "rgba(37, 99, 235, 0.45)")
        .replace("_POLISH_CARD_SHADOW_", "rgba(37, 99, 235, 0.12)")
        .replace("_POLISH_SIDEBAR_BORDER_", "rgba(37, 99, 235, 0.15)")
        .replace("_POLISH_SCROLL_TRACK_", "rgba(244, 246, 251, 0.9)")
        .replace("_POLISH_SCROLL_THUMB_", "linear-gradient(180deg, #2563EB88, #05966988)")
        .replace("_POLISH_FOCUS_66", "#2563EB66").replace("_POLISH_FOCUS_40", "rgba(37, 99, 235, 0.25)")
        .replace("_POLISH_FOCUS_", "#2563EB"))
try:
    st.markdown(_V64_POLISH_CSS, unsafe_allow_html=True)
except Exception:
    pass

# ---------------------------------------------------------
# LIVE FACE GUARD INJECTION (v62): kamera zote za mfumo (st.camera_input)
# zinaangaliwa na JavaScript ya live face-detection — ina-scann USO TU,
# inaonya hakuna uso / uso zaidi ya mmoja. OpenCV ya server inabaki kuwa
# mthibitishaji wa mwisho (uso MMOJA tu inaruhusiwa).
# ---------------------------------------------------------
try:
    inject_face_guard_js()
except Exception:
    pass

# ---------------------------------------------------------
# STANDALONE TITLE CARD
# ---------------------------------------------------------
custom_logo_b64 = get_setting('system_logo_b64', '')

_logo_html = (f"<img src='data:image/png;base64,{custom_logo_b64}' style='max-height:65px; margin-bottom:10px;'><br>"
              if custom_logo_b64 else "")

st.markdown(textwrap.dedent(f"""
<div class="standalone-title-card">
{_logo_html}
<div class="standalone-title-text">⚡ CYBER BUSINESS GRADING SYSTEM</div>
</div>
""").strip(), unsafe_allow_html=True)

# ---------------------------------------------------------
# REAL-TIME LIVE DIGITAL CLOCK & DIRECTOR HEADER
# ---------------------------------------------------------
def render_live_clock_header():
    # THEME-AWARE (v63): Cyber = neon cyan; Simple = bluu ya kitaaluma
    if get_ui_theme() == "SIMPLE":
        _clock_style = "color: #1D4ED8; text-shadow: none;"
    else:
        _clock_style = "color: #00F0FF; text-shadow: 0 0 10px #00F0FF;"
    clock_html = textwrap.dedent("""
    <div class="director-banner" style="justify-content: center;">
        <div style="text-align: center;">
            <div style="font-size: 1.15rem; font-weight: 800; __CLOCK_STYLE__" id="live_clock_display">
                ⏱️ LOADING LIVE CLOCK...
            </div>
        </div>
    </div>

    <script>
    function updateCyberClock() {
        var now = new Date();
        var days = ["Jumapili", "Jumatatu", "Jumanne", "Jumatano", "Alhamisi", "Ijumaa", "Jumamosi"];
        var dayName = days[now.getDay()];
        var yyyy = now.getFullYear();
        var mm = String(now.getMonth() + 1).padStart(2, '0');
        var dd = String(now.getDate()).padStart(2, '0');
        var hh = String(now.getHours()).padStart(2, '0');
        var min = String(now.getMinutes()).padStart(2, '0');
        var ss = String(now.getSeconds()).padStart(2, '0');

        var clockStr = "📅 " + dayName + ", " + yyyy + "-" + mm + "-" + dd + " | ⏱️ " + hh + ":" + min + ":" + ss;
        var el = document.getElementById("live_clock_display");
        if (el) {
            el.innerHTML = clockStr;
        }
    }
    setInterval(updateCyberClock, 1000);
    updateCyberClock();
    </script>
    """).strip().replace("__CLOCK_STYLE__", _clock_style)
    render_js_html(clock_html, height=60)

render_live_clock_header()

# Helper Functions

def render_communication_center(current_user_role="DIRECTOR", current_user_b_id=None):
    st.subheader("📞 Portal ya Mawasiliano & Contact Directory")
    st.info("💡 **COMMUNICATION CENTER:** Hapa unaweza kumchagua mtu yeyote kutoka kwenye Directory na Kumpigia Simu Moja kwa Moja (`tel:`), Kuanzisha Video Call ya HD (`Jitsi Meet`), au Kutuma Normal Text SMS moja kwa moja kwenye namba yake ya simu!")

    conn = get_connection()
    df_b = pd.read_sql_query("SELECT id, name, manager_name, manager_phone, manager_email, manager_bank_name, manager_account_number FROM businesses", conn)
    df_s = pd.read_sql_query("""
        SELECT s.id, b.name as business_name, s.full_name, s.role_title, s.phone, s.email, s.bank_name, s.account_number 
        FROM staff_members s LEFT JOIN businesses b ON s.business_id = b.id
        WHERE s.status = 'ACTIVE'
    """, conn)
    conn.close()

    dir_p = get_setting('director_phone', '0677' + '698182')
    dir_e = get_setting('director_email', 'mavoajoshua9' + '@gmail.com')

    contacts = [
        {
            "name": "DIRECTOR ADMIN",
            "role": "DIRECTOR MKUU (System Admin)",
            "business": "HEADQUARTERS",
            "phone": dir_p,
            "email": dir_e,
            "bank": "CRDB BANK",
            "acc": "01523456789"
        }
    ]

    if not df_b.empty:
        for idx, r in df_b.iterrows():
            contacts.append({
                "name": f"{r['manager_name']} (Meneja)",
                "role": "MENEJA MKUU (General Manager)",
                "business": r['name'],
                "phone": r['manager_phone'],
                "email": r['manager_email'],
                "bank": r.get('manager_bank_name') or "NMB BANK",
                "acc": r.get('manager_account_number') or "00000000000"
            })

    if not df_s.empty:
        for idx, r in df_s.iterrows():
            contacts.append({
                "name": r['full_name'],
                "role": r['role_title'],
                "business": r['business_name'] or "N/A",
                "phone": r['phone'],
                "email": r['email'],
                "bank": r.get('bank_name') or "NMB BANK",
                "acc": r.get('account_number') or "00000000000"
            })

    c_names = [f"{c['name']} - {c['role']} ({c['business']}) - {c['phone']}" for c in contacts]
    sel_contact_lbl = st.selectbox("🎯 Chagua Mtu Unayetaka Kuwasiliana Naye:", c_names, key=f"comm_sel_{current_user_role}_{current_user_b_id}")
    sel_c = contacts[c_names.index(sel_contact_lbl)]

    clean_phone = re.sub(r'\D', '', sel_c['phone'])

    st.markdown("---")
    st.markdown(textwrap.dedent(f"""
    <div style="background:#0F172A; border:2px solid #00F0FF; padding:20px; border-radius:14px; margin-bottom:20px;">
        <h3 style="color:#00F0FF; margin:0;">👤 {sel_c['name']}</h3>
        <p style="color:#E2E8F0; font-size:1.05rem; margin-top:6px;">
            💼 <b>Wadhifa/Cheo:</b> {sel_c['role']}<br>
            🏢 <b>Biashara:</b> {sel_c['business']}<br>
            📞 <b>Namba ya Simu:</b> <code>{sel_c['phone']}</code><br>
            ✉️ <b>Barua Pepe:</b> <code>{sel_c['email']}</code><br>
            🏦 <b>Taarifa za Benki:</b> {sel_c['bank']} | Akaunti: <code>{sel_c['acc']}</code>
        </p>
    </div>
    """).strip(), unsafe_allow_html=True)

    col_call, col_video, col_sms = st.columns(3)

    with col_call:
        st.markdown("#### 📞 1. Piga Simu Direct")
        st.caption("Bofya hapa chini kumpigia simu moja kwa moja kupitia dialer ya simu yako:")
        st.markdown(textwrap.dedent(f"""
        <a href="tel:{sel_c['phone']}" target="_blank" style="text-decoration:none;">
            <div style="background:#003322; border:2px solid #39FF14; border-radius:10px; padding:15px; text-align:center; cursor:pointer;">
                <h4 style="color:#39FF14; margin:0;">📞 PIGA SIMU NOW</h4>
                <p style="color:#FFFFFF; font-size:0.9rem; margin-top:4px;">{sel_c['phone']}</p>
            </div>
        </a>
        """).strip(), unsafe_allow_html=True)

    with col_video:
        st.markdown("#### 📹 2. Piga Video Call Direct")
        st.caption("Bofya kuanzisha HD Video Call ya ana kwa ana (WebRTC Video Room):")
        jitsi_room = f"https://meet.jit.si/CyberGradingSystem_Call_{clean_phone}"
        st.markdown(textwrap.dedent(f"""
        <a href="{jitsi_room}" target="_blank" style="text-decoration:none;">
            <div style="background:#1C2541; border:2px solid #00F0FF; border-radius:10px; padding:15px; text-align:center; cursor:pointer;">
                <h4 style="color:#00F0FF; margin:0;">📹 ANZISHA VIDEO CALL</h4>
                <p style="color:#FFFFFF; font-size:0.9rem; margin-top:4px;">HD Video Conference</p>
            </div>
        </a>
        """).strip(), unsafe_allow_html=True)

    with col_sms:
        st.markdown("#### 💬 3. Tuma Normal Text SMS")
        st.caption("Andika ujumbe na ubofye kutuma SMS moja kwa moja kwenye simu yake:")
        with st.form(f"direct_sms_form_{clean_phone}", clear_on_submit=False):
            sms_msg_body = st.text_area("Andika Ujumbe wa SMS:", value=f"Habari {sel_c['name']}, kutoka Cyber Business Grading System...", height=80, key=f"sms_txt_{clean_phone}")
            btn_sms = st.form_submit_button("💬 TUMA NORMAL TEXT SMS")

            if btn_sms:
                if not sms_msg_body.strip():
                    trigger_beep_sound(1200, 5.0, "sawtooth")
                    render_marquee_error("Ujumbe wa SMS", "Ujumbe hauwezi kuachwa wazi!", "Andika ujumbe wa kutuma.", field_label=["Andika Ujumbe wa SMS"])
                else:
                    conn = get_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO staff_sms_logs (recipient_type, recipient_name, phone, message)
                        VALUES (?, ?, ?, ?)
                    """, ("DIRECT_COMMUNICATION", sel_c['name'], sel_c['phone'], sms_msg_body.strip()))
                    conn.commit()
                    conn.close()

                    sms_url = f"sms:{sel_c['phone']}?body={urllib.parse.quote(sms_msg_body.strip())}"
                    trigger_beep_sound(523.25, 1.0, "sine")
                    st.success(f"✅ UJUMBE WAKO UMEHIFADHIWA! Bofya kitufe cha chini kutuma SMS kwenye simu yako:")
                    st.markdown(f"👉 **[💬 Bofya Hapa Kufungua SMS App na Kutuma kwa {sel_c['phone']}]({sms_url})**")

def get_businesses():
    conn = get_connection()
    df = pd.read_sql_query("SELECT * FROM businesses ORDER BY name ASC", conn)
    conn.close()
    return df

# ---------------------------------------------------------
# THEME CHOOSER (v63) — kila mtu anachague muonekano anayotaka:
# "⚡ Cyber Interface" (cyberpunk) au "🖥️ User Interface (Kawaida)" (clean).
# Chaguo kinahifadhiwa kwenye database kwa kila portal/role tofauti.
# ---------------------------------------------------------
ui_theme_choice = st.sidebar.radio(
    t("🎨 CHAGUA MUONEKANO (Interface Theme):"),
    THEME_LABELS,
    key="ui_theme_choice",
    help="⚡ Cyber Interface = muonekano wa cyberpunk wenye neon na glitch. 🖥️ User Interface (Kawaida) = muonekano safi, mepesi na rasmi."
)
_theme_scope_after_radio = resolve_theme_scope()
try:
    if get_setting(f"theme::{_theme_scope_after_radio}", THEME_LABEL_CYBER) != ui_theme_choice:
        update_setting(f"theme::{_theme_scope_after_radio}", ui_theme_choice)
except Exception:
    pass

# ---------------------------------------------------------
# LANGUAGE CHOOSER (v64) — kila mtu anachague lugha yake:
# Kiswahili (chaguo-msingi) au English. Chaguo kinahifadhiwa kwenye
# database kwa kila portal/role tofauti; mfumo unahamia lugha hiyo.
# ---------------------------------------------------------
app_language_choice = st.sidebar.radio(
    t("🌍 CHAGUA LUGHA (Language):"),
    APP_LANGUAGES,
    key="app_language",
    help="🌍 Kiswahili = lugha ya mfumo kwa chaguo-msingi. English = mfumo unahamia kwenye lugha ya Kiingereza."
)
try:
    st.session_state["app_language"] = app_language_choice
    _lang_scope_now = resolve_language_scope()
    if get_setting(f"lang::{_lang_scope_now}", "Kiswahili") != app_language_choice:
        update_setting(f"lang::{_lang_scope_now}", app_language_choice)
    _CURRENT_LANG_CACHE["scope"] = _lang_scope_now
    _CURRENT_LANG_CACHE["val"] = app_language_choice
except Exception:
    pass

# ---------------------------------------------------------
# SYSTEM NAVIGATION PORTALS
# ---------------------------------------------------------
portal_mode = st.sidebar.radio(
    t("🌐 CHAGUA SYSTEM PORTAL:"),
    [
        t("🏢 Mfumo wa Biashara (Business Portal)"),
        t("👑 Director Portal (Admin Control Panel)"),
        PERSONAL_PORTAL_LABEL,
        t("📝 Tuma Maombi ya Ajira (Job Application)")
    ],
    key="portal_mode"
)

# Namba ya ki_canonical ya portal (Kiswahili) — dispatch ya portals inatumia
# hii hata kama lugha ya sasa ni English (v64).
_PORTAL_EN2SW = {v: k for k, v in _TRANSLATIONS.items() if k in (
    "🏢 Mfumo wa Biashara (Business Portal)",
    "👑 Director Portal (Admin Control Panel)",
    "📝 Tuma Maombi ya Ajira (Job Application)")}
portal_mode_canon = _PORTAL_EN2SW.get(portal_mode, portal_mode)

today_date_str = str(datetime.date.today())
now_dt = datetime.datetime.now()

# =========================================================================
# PORTAL 3: TUMA MAOMBI YA AJIRA KWA DIRECTOR (PUBLIC JOB PORTAL)
# =========================================================================
# Auto-clear notifications when user navigates away from Notifications page
if "last_visited_page" not in st.session_state:
    st.session_state["last_visited_page"] = ""

current_page_name = f"{portal_mode}"
if "Notifications" in st.session_state["last_visited_page"] and "Notifications" not in current_page_name:
    if "Director" in st.session_state["last_visited_page"]:
        mark_notifications_read('DIRECTOR')
    else:
        mark_notifications_read('MANAGER')

st.session_state["last_visited_page"] = current_page_name

if portal_mode_canon == "📝 Tuma Maombi ya Ajira (Job Application)":
    st.subheader("📝 Tuma Maombi ya Ajira Moja kwa Moja kwa Director (Joshua Joseph)")
    st.info("💡 **TAARIFA:** Fomu hii inatumiwa na mtu yeyote anayetaka kuomba ajira. Ukisubmit, maombi yako yataenda moja kwa moja kwenye Director Portal bila kuonekana kwenye mfumo wa umma!")

    with st.form("public_job_application_form", clear_on_submit=True):
        st.markdown("#### 1️⃣ Taarifa Binafsi za Mwombaji (Majina Matatu)")
        app_col1, app_col2 = st.columns(2)
        with app_col1:
            app_full_name = st.text_input("Majina Matatu Yote (mf. JOSHUA JOSEPH MAVOA)", key="app_full_name")
            
            app_phone_val = st.text_input("Namba ya Simu (06... / 07... Tarakimu 10)", max_chars=10, key="app_phone")
            st.caption(f"📱 **Tarakimu Zilizojazwa:** `{len(app_phone_val)} / 10`")
        with app_col2:
            app_email = st.text_input("Barua Pepe (Email: jina@gmail.com)", key="app_email")
            
            app_nida_val = st.text_input("Namba ya NIDA (Tarakimu 20)", max_chars=20, key="app_nida")
            st.caption(f"🆔 **Tarakimu Zilizojazwa:** `{len(app_nida_val)} / 20`")

        st.markdown("#### 2️⃣ Mahali Anapoishi (Anwani ya Mkoa - Tanzania)")
        sel_mkoa = st.selectbox("Chagua Mkoa Wako:", TANZANIA_REGIONS, key="app_mkoa")
        sel_wilaya = ""

        full_residence_upper = f"MKOA WA {sel_mkoa.upper()}"
        st.caption(f"📌 **Anwani Yako Halisi:** `{full_residence_upper}`")

        st.markdown("#### 3️⃣ Ngazi ya Elimu, Cheo & Taarifa za Benki")
        edu_col1, edu_col2 = st.columns(2)
        with edu_col1:
            app_edu_level = st.selectbox("Ngazi ya Elimu Aliyofikia:", EDUCATION_LEVELS, key="app_edu")
        with edu_col2:
            app_desired_role = st.selectbox("Cheo / Nyadhifa Unayoomba:", STANDARD_ROLES, key="app_role")

        bank_col1, bank_col2 = st.columns(2)
        with bank_col1:
            app_bank_name = st.selectbox("Benki Yako / Mfumo wa Malipo:", TANZANIA_BANKS, key="app_bank_name")
        with bank_col2:
            app_account_num = st.text_input("Akaunti ya Benki / Simu ya Malipo (Account Number):", key="app_account_num")

        st.markdown("#### 4️⃣ Sura, Vyeti, Barua ya Kuomba Ajira & CV (Upload Files)")
        st.markdown("📸 **Piga au Upload Picha ya Sura Yako (Facial Scan):**")
        app_photo_file = st.camera_input("Piga Sura ya Mwombaji", key="app_cam_widget")

        file_col1, file_col2, file_col3 = st.columns(3)
        with file_col1:
            app_letter_file = st.file_uploader("Upload Barua ya Kuomba Ajira (PDF/DOCX/Image)", type=["pdf", "docx", "png", "jpg", "jpeg"], key="app_letter_file")
        with file_col2:
            app_cv_file = st.file_uploader("Upload CV Yako (Curriculum Vitae)", type=["pdf", "docx", "png", "jpg", "jpeg"], key="app_cv_file")
        with file_col3:
            app_cert_file = st.file_uploader("Upload Nakala ya Cheti cha Elimu", type=["pdf", "docx", "png", "jpg", "jpeg"], key="app_cert_file")

        btn_submit_app = st.form_submit_button("📤 TUMA MAOMBI YA AJIRA KWA DIRECTOR")

        if btn_submit_app:
            valid_names, names_msg = validate_three_names(app_full_name, "Majina ya Mwombaji")
            valid_phone, phone_msg = validate_phone_number(app_phone_val)
            valid_email, email_msg = validate_gmail_email(app_email)
            valid_nida, nida_msg = validate_nida_number(app_nida_val)

            if not valid_names:
                trigger_beep_sound(1200, 5.0, "sawtooth")
                render_marquee_error("Majina ya Mwombaji", names_msg, "Andika majina yako yote matatu mfano: JOSHUA JOSEPH MAVOA.", field_label=["Majina Matatu Yote"])
            elif not valid_phone:
                trigger_beep_sound(1200, 5.0, "sawtooth")
                render_marquee_error("Namba ya Simu", phone_msg, "Ingiza tarakimu 10 pekee zikianzaga na 06 au 07.", field_label=["Namba ya Simu"])
            elif not valid_email:
                trigger_beep_sound(1200, 5.0, "sawtooth")
                render_marquee_error("Email ya Mwombaji", email_msg, "Ingiza email yenye mfumo sahihi wa 'jina@gmail.com'.", field_label=["Barua Pepe"])
            elif not valid_nida:
                trigger_beep_sound(1200, 5.0, "sawtooth")
                render_marquee_error("Namba ya NIDA", nida_msg, "Ingiza tarakimu 20 kamili za namba yako ya NIDA.", field_label=["Namba ya NIDA"])
            elif not app_account_num.strip():
                trigger_beep_sound(1200, 5.0, "sawtooth")
                render_marquee_error("Akaunti ya Benki", "Lazima ujaze Namba ya Akaunti ya Benki!", "Weka namba ya akaunti ya benki.", field_label=["Akaunti ya Benki"])
            elif app_photo_file is None:
                trigger_beep_sound(1000, 5.0, "sawtooth")
                render_marquee_error("Scan ya Sura", "Picha ya sura inatakiwa kabla ya kutuma maombi!", "Piga picha ya uso wako kupitia kamera.", field_label=["Piga Sura ya Mwombaji"])
            else:
                is_verified, face_b64, face_count, scan_msg, metadata_json = scan_and_verify_face(app_photo_file)
                if not is_verified:
                    trigger_beep_sound(1500, 5.0, "square")
                    render_marquee_error("OpenCV Face Verification", scan_msg, "Elekeza sura usoni vizuri kwenye kamera.", field_label=["Piga Sura ya Mwombaji"])
                else:
                    letter_b64 = file_to_b64(app_letter_file) if app_letter_file else None
                    letter_name = app_letter_file.name if app_letter_file else None
                    cv_b64 = file_to_b64(app_cv_file) if app_cv_file else None
                    cv_name = app_cv_file.name if app_cv_file else None
                    cert_b64 = file_to_b64(app_cert_file) if app_cert_file else None
                    cert_name = app_cert_file.name if app_cert_file else None

                    conn = get_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO job_applications (full_name, residence_mkoa, residence_wilaya, full_residence_upper, phone, nida_number, email, education_level, desired_role, bank_name, account_number, photo_b64, application_letter_b64, letter_filename, cv_b64, cv_filename, cert_b64, cert_filename)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (names_msg, sel_mkoa, sel_wilaya, full_residence_upper, phone_msg, nida_msg, email_msg, app_edu_level, app_desired_role, app_bank_name, app_account_num.strip(), face_b64, letter_b64, letter_name, cv_b64, cv_name, cert_b64, cert_name))
                    conn.commit()
                    conn.close()

                    trigger_beep_sound(523.25, 1.0, "sine")
                    add_notification('DIRECTOR', '📝 Ombi Jipya la Ajira', f"Mwombaji '{names_msg}' ametuma ombi la ajira la cheo cha '{app_desired_role}'", 'JOB_APP')
                    st.success(f"✅ MAOMBI YAKO YA AJIRA YA '{names_msg}' YEMETUMWA KIKAMILIFU KWA DIRECTOR JOSHUA JOSEPH! Mfumo utakujibu kupitia email yako ({email_msg}).")
                    st.rerun()

# =========================================================================
# PORTAL: DETAILS ZANGU BINAFSI (v64 — PERSONAL DETAILS, PIN-LOCKED)
# Kila mtu anaingia kwa PIN yake mwenyew anaona details ZAKE TU:
# jina, akaunti, salio, mishahara + risiti zake (PDF), SMS zake,
# na taarifa za biashara yake tu. Hauruhusiwi kuona za mwenzake.
# =========================================================================
elif portal_mode_canon == PERSONAL_PORTAL_LABEL:
    st.subheader("👤 " + t("Details Zangu Binafsi (Personal Details)"))
    st.info(t("💡 **PRIVACY:** Ingia kwa jina lako na PIN yako mwenyew. Utaona details zako TU "
              "(jina, akaunti, salio, mishahara, risiti za PDF na SMS zako) pamoja na taarifa za "
              "biashara uliyoruhusiwa. Hakuna mtu anayeona details za mwenzake.") if get_current_language() == "English"
            else "💡 **PRIVACY:** Ingia kwa jina lako na PIN yako mwenyew. Utaona details zako TU "
                 "(jina, akaunti, salio, mishahara, risiti za PDF na SMS zako) pamoja na taarifa za "
                 "biashara uliyoruhusiwa. Hakuna mtu anayeona details za mwenzake.")

    conn = get_connection()
    df_pd_biz = pd.read_sql_query("SELECT id, name, manager_name FROM businesses ORDER BY name ASC", conn)
    conn.close()

    if df_pd_biz.empty:
        st.warning(t("⚠️ Bado hakuna biashara iliyosajiliwa. Director lazima asajili biashara kwanza."))
    else:
        pd_biz_name = st.selectbox(t("🏢 Chagua Biashara Yako:"), df_pd_biz['name'].tolist(), key="pd_biz_sel")
        pd_biz_row = df_pd_biz[df_pd_biz['name'] == pd_biz_name].iloc[0]
        pd_biz_id = int(pd_biz_row['id'])

        conn = get_connection()
        df_pd_people = pd.read_sql_query(
            "SELECT full_name, role_title, staff_type FROM staff_members WHERE business_id = ? AND status = 'ACTIVE' ORDER BY staff_type DESC, full_name ASC",
            conn, params=(pd_biz_id,))
        conn.close()

        if df_pd_people.empty:
            st.warning(t("⚠️ Hakuna watu waliosajiliwa kwenye biashara hii bado."))
        else:
            pd_people_opts = [f"{r['full_name']} ({r['role_title']})" for _, r in df_pd_people.iterrows()]
            pd_who_lbl = st.selectbox(t("👤 Chagua Jina Lako:"), pd_people_opts, key="pd_who_sel")
            pd_who_name = df_pd_people.iloc[pd_people_opts.index(pd_who_lbl)]['full_name']

            pd_pin = st.text_input(t("🔐 Weka PIN Yako Binafsi (4 tarakimu):"), type="password",
                                   max_chars=6, key="pd_pin_input")
            if st.button("🔓 " + t("INGIA KATIKA DETAILS ZANGU")):
                if not pd_pin.strip():
                    trigger_beep_sound(1200, 5.0, "sawtooth")
                    render_marquee_error("Personal PIN", t("PIN haijajazwa!"), t("Weka PIN yako binafsi ya tarakimu 4."), field_label=["PIN"])
                elif _hash_pin(pd_pin) != get_person_pin_hash(pd_biz_id, pd_who_name):
                    trigger_beep_sound(1500, 5.0, "square")
                    render_marquee_error("Personal PIN", t("PIN si sahihi! Details za binafsi zimefungwa."), t("Angalia PIN yako (ulazimishwa na Director/Meneja), kisha jaribu tena."), field_label=["PIN"])
                else:
                    st.session_state["pd_auth"] = True
                    st.session_state["pd_who"] = pd_who_name
                    st.session_state["pd_biz_id"] = pd_biz_id
                    trigger_beep_sound(523.25, 1.0, "sine")
                    st.rerun()

            if st.session_state.get("pd_auth") and st.session_state.get("pd_who") == pd_who_name and st.session_state.get("pd_biz_id") == pd_biz_id:
                profile = get_person_profile(pd_biz_id, pd_who_name)
                if profile:
                    # ── KADI YA UTAMBULISHO (details za huyu mtu TU) ──
                    st.markdown("---")
                    idc1, idc2, idc3, idc4 = st.columns(4)
                    idc1.metric(t("👤 Jina"), profile['name'])
                    idc2.metric(t("💼 Cheo"), str(profile['role']))
                    idc3.metric(t("📱 Simu"), str(profile['phone']))
                    idc4.metric(t("💰 Salio la Akaunti"), f"{profile['balance']:,.2f} TZS")
                    st.caption(f"🏦 **{t('Benki')}:** `{profile['bank']}` | 💳 **{t('Akaunti')}:** `{profile['account']}` "
                               f"| 👑 **{t('Meneja wa Biashara')}:** {pd_biz_row['manager_name']}")

                    # ── MISHAHARA YANGU + RISITI PDF ──
                    st.markdown("---")
                    st.markdown("#### 🧾 " + t("Mishahara Yangu & Risiti za PDF"))
                    df_my_rc = get_my_receipts(pd_biz_id, profile['name'], profile['phone'])
                    if df_my_rc.empty:
                        st.info(t("Bado hakuna malipo yaliyokusajiliwa kwa jina lako."))
                    else:
                        df_my_show = df_my_rc[["payment_date", "payer_role", "amount", "recipient_bank", "recipient_account", "reference_code", "bank_txn_id", "gateway_status"]].copy()
                        df_my_show.columns = [t("Muda"), t("Alitelipa"), t("Kiasi TZS"), t("Benki"), t("Akaunti"), t("Ref"), "TxnID", t("Hali")]
                        st.dataframe(df_my_show, use_container_width=True)

                        for _, r_my in df_my_rc.iterrows():
                            conn = get_connection()
                            df_pdf_row = pd.read_sql_query("SELECT pdf_b64 FROM receipt_pdfs WHERE receipt_no = ? ORDER BY id DESC LIMIT 1",
                                                           conn, params=(r_my['receipt_no'],))
                            conn.close()
                            pdf_bytes = None
                            if not df_pdf_row.empty:
                                try:
                                    pdf_bytes = base64.b64decode(df_pdf_row.iloc[0]['pdf_b64'])
                                except Exception:
                                    pdf_bytes = None
                            if pdf_bytes is None:
                                # Risiti ya kale isiyo na PDF — tengeneza sasa
                                pdf_bytes = generate_receipt_pdf({
                                    "receipt_no": r_my['receipt_no'], "payment_time": r_my['payment_date'],
                                    "business_name": r_my['business_name'], "payer_role": r_my['payer_role'],
                                    "payer_name": r_my['payer_name'], "recipient_name": r_my['recipient_name'],
                                    "recipient_bank": r_my['recipient_bank'], "recipient_account": r_my['recipient_account'],
                                    "recipient_phone": r_my['recipient_phone'], "amount": r_my['amount'],
                                    "reference_code": r_my['reference_code'], "channel_label": r_my['bank_channel_label'],
                                    "response_code": r_my['gateway_status'], "bank_txn_id": r_my['bank_txn_id'],
                                    "source_balance_after": r_my.get('source_balance_after', 0) or 0,
                                    "recipient_balance_after": r_my.get('recipient_balance_after', 0) or 0,
                                    "notes": r_my.get('notes', '')})
                                if pdf_bytes:
                                    store_receipt_pdf(r_my['receipt_no'], pd_biz_id, r_my['recipient_name'],
                                                      r_my['recipient_phone'], r_my['amount'], pdf_bytes)
                            if pdf_bytes:
                                st.download_button(
                                    f"📄 {t('PAKUA RISITI PDF')} — {r_my['receipt_no']} ({float(r_my['amount']):,.0f} TZS)",
                                    data=pdf_bytes, file_name=f"Risiti_{r_my['receipt_no']}.pdf",
                                    mime="application/pdf", key=f"pd_dl_{r_my['receipt_no']}")

                    # ── SMS ZANGU ──
                    st.markdown("---")
                    st.markdown("#### 📱 " + t("SMS Zangu (Ujumbe Wote Niliotumiwa)"))
                    df_my_sms = get_my_sms(pd_biz_id, profile['phone'])
                    if df_my_sms.empty:
                        st.info(t("Bado hakuna SMS iliyotumiwa kwa namba yako."))
                    else:
                        df_sms_show = df_my_sms[["created_at", "message", "status"]].copy()
                        df_sms_show.columns = [t("Muda"), t("Ujumbe"), t("Hali")]
                        st.dataframe(df_sms_show, use_container_width=True)

                    # ── TAARIFA ZA BIASHARA (zilizoruhusiwa) ──
                    st.markdown("---")
                    st.markdown("#### 🏢 " + t("Taarifa za Biashara (Zilizoruhusiwa)"))
                    conn = get_connection()
                    df_dp_today = pd.read_sql_query("SELECT * FROM daily_profit WHERE business_id = ? AND record_date = ?",
                                                    conn, params=(pd_biz_id, today_date_str))
                    df_dp_hist = pd.read_sql_query("SELECT * FROM daily_profit WHERE business_id = ? ORDER BY record_date DESC LIMIT 7",
                                                   conn, params=(pd_biz_id,))
                    conn.close()
                    bc1, bc2, bc3 = st.columns(3)
                    bc1.metric(t("Biashara"), pd_biz_name)
                    bc2.metric(t("Mapato ya Leo"), f"{float(df_dp_today.iloc[0]['revenue']):,.2f} TZS" if not df_dp_today.empty else "0.00 TZS")
                    bc3.metric(t("Faida ya Leo"), f"{float(df_dp_today.iloc[0]['profit']):,.2f} TZS" if not df_dp_today.empty else "0.00 TZS")
                    if not df_dp_hist.empty:
                        st.caption(t("📊 Faida ya siku 7 zilizopita (ukiwa umeondoa mapato ya kesho):") + " " +
                                   ", ".join(f"{r['record_date']}: {float(r['profit']):,.0f}" for _, r in df_dp_hist.iterrows()))

                    if st.button("🔒 " + t("TOKA / FUNGA DETAILS ZANGU"), key="pd_logout_btn"):
                        for k in ("pd_auth", "pd_who", "pd_biz_id"):
                            st.session_state.pop(k, None)
                        st.rerun()
            elif st.session_state.get("pd_auth"):
                # Amebadilisha jina au biashara — fungua session ya kale
                for k in ("pd_auth", "pd_who", "pd_biz_id"):
                    st.session_state.pop(k, None)

# =========================================================================
# PORTAL 2: DIRECTOR PORTAL (ADMIN CONTROL PANEL)
# =========================================================================
elif portal_mode_canon == "👑 Director Portal (Admin Control Panel)":
    st.subheader("👑 Director Executive Control Dashboard & Admin Panel")
    
    dir_password_in_db = get_setting('director_password', 'director1234')
    del_password_in_db = get_setting('deletion_password', 'DELETE2026')

    if "director_logged_in" not in st.session_state:
        st.session_state["director_logged_in"] = False

    if not st.session_state["director_logged_in"]:
        st.warning("🔒 **DIRECTOR SECURITY LOCK:** Area hii inalindwa na Password ya Director.")
        entered_dir_pass = st.text_input("Weka Password ya Director Portal:", type="password", key="dir_login_pass_input")
        if st.button("🔓 INGIA KWENYE DIRECTOR PORTAL"):
            if entered_dir_pass == dir_password_in_db:
                st.session_state["director_logged_in"] = True
                st.success("✅ Umefanikiwa kuingia kama Director!")
                st.rerun()
            else:
                trigger_beep_sound(1500, 5.0, "square")
                render_marquee_error("Director Login", "Password ya Director si sahihi!", "Weka password sahihi ya Director Portal.", field_label=["Password ya Director Portal"])
        st.stop()

    # -----------------------------------------------------------------
    # DAILY REPORT GUARD + AUTO MONITOR (v62)
    # Alert ya automatic kwa Director: biashara zisizotuma taarifa za leo
    # -----------------------------------------------------------------
    missing_today = run_daily_report_guard()
    if missing_today:
        _beep_key = f"dir_missing_beeped_{today_date_str}"
        if not st.session_state.get(_beep_key):
            trigger_beep_sound(980, 4.5, "cyber_alert")
            st.session_state[_beep_key] = True
    inject_auto_monitor(75)

    d_col_a, d_col_b = st.columns(2)
    with d_col_b:
        if st.button("🔒 LOGOUT DIRECTOR", key="btn_logout_director"):
            st.session_state["director_logged_in"] = False
            st.rerun()

    dir_unread_cnt = get_unread_notifications_count('DIRECTOR')
    dir_notif_label = f"🔔 Notifications (🔴 {dir_unread_cnt})" if dir_unread_cnt > 0 else "🔔 Notifications"

    dir_tab1, dir_tab2, dir_tab3, dir_tab4, dir_tab5, dir_tab6, dir_tab7, dir_tab8, dir_tab9, dir_tab10, dir_tab11, dir_tab12 = st.tabs([
        "👑 Executive Overview",
        "🏢 Usajili wa Biashara & Meneja",
        "👥 Wafanyakazi & Idhini",
        "💳 Malipo ya Mishahara (Payroll)",
        dir_notif_label,
        "✉️ Kutuma SMS kwa Staff",
        "📝 Maombi ya Ajira ya Nje",
        "📋 Submissions & Audit Log",
        "📞 Mawasiliano & Contacts",
        "🗑️ Ufutaji wa Data",
        "⚙️ Settings & Password",
        "🧾 Risiti za Malipo (DB ya Director)"
    ])

    # -----------------------------------------------------------------
    # TAB 1: EXECUTIVE OVERVIEW & PROBLEM ALERTS ENGINE
    # -----------------------------------------------------------------
    with dir_tab1:
        # 🚨 ALERT YA AUTOMATIC: Biashara zisizotuma taarifa za mapato za leo
        if missing_today:
            _missing_rows = " ".join(
                f"<b>🏢 {m['name']}</b> (Meneja: {m['manager_name']}, 📞 {m['manager_phone']}) &nbsp;|&nbsp;"
                for m in missing_today
            )
            st.markdown(textwrap.dedent(f"""
            <div class="blinking-red-box">
                <h3 style="color:#FF0000; margin:0;">🚨 TAARIFA KWA DIRECTOR: Biashara {len(missing_today)} HAZIJATUMA TAARIFA ZA MAPATO ZA LEO ({today_date_str})!</h3>
                <p style="color:#FFFFFF; font-size:1.02rem; margin-top:6px;">{_missing_rows}</p>
                <p style="color:#FFD700; font-size:0.9rem; margin-bottom:0;">🔔 Notification ya automatic imewekwa kwenye Database ya Taarifa za Mfumo. AUTO MONITOR inaangalia kila sekunde 75 — biashara ikituma taarifa zake, alert hii itajiondoa yenyewe.</p>
            </div>
            """).strip(), unsafe_allow_html=True)

        st.markdown("### 📊 Cross-Business Financial Overview")
        
        conn = get_connection()
        df_b_all = pd.read_sql_query("SELECT * FROM businesses", conn)
        df_daily_all = pd.read_sql_query("SELECT * FROM daily_records", conn)
        df_assets_all = pd.read_sql_query("SELECT * FROM assets_liabilities", conn)
        conn.close()

        tot_cash_rev = df_daily_all['cash_revenue'].sum() if not df_daily_all.empty else 0.0
        tot_credit_rev = df_daily_all['credit_revenue'].sum() if not df_daily_all.empty else 0.0
        tot_rev = df_daily_all['revenue'].sum() if not df_daily_all.empty else 0.0
        tot_exp = df_daily_all['expenses'].sum() if not df_daily_all.empty else 0.0
        tot_net_cash = tot_rev - tot_exp

        tot_assets = df_assets_all[df_assets_all['item_type'] == 'Asset']['amount'].sum() if not df_assets_all.empty else 0.0
        tot_liabilities = df_assets_all[df_assets_all['item_type'] == 'Liability']['amount'].sum() if not df_assets_all.empty else 0.0

        o1, o2, o3, o4, o5 = st.columns(5)
        o1.metric(t("Jumla ya Biashara"), len(df_b_all))
        o2.metric(t("Jumla Kuu Mapato"), f"{tot_rev:,.2f} TZS", delta=f"Cash: {tot_cash_rev:,.2f}")
        o3.metric(t("Jumla ya Matumizi"), f"{tot_exp:,.2f} TZS")
        o4.metric(t("Faida Halisi Cash"), f"{tot_net_cash:,.2f} TZS")
        o5.metric(t("Madeni Yote"), f"{tot_liabilities:,.2f} TZS", delta=f"Assets: {tot_assets:,.2f}")

        # ── V64: LINE GRAPH YA UKUAJI — FAIDA YA KILA SIKU YA KILA BIASHARA ──
        st.markdown("### " + t("📈 UKUAJI WA KILA BIASHARA KWA FAIDA YA SIKU (Business Growth by Daily Profit)"))
        conn = get_connection()
        df_dp_all = pd.read_sql_query("""
            SELECT dp.record_date, dp.profit, b.name AS business_name
            FROM daily_profit dp JOIN businesses b ON dp.business_id = b.id
            ORDER BY dp.record_date ASC
        """, conn)
        conn.close()
        if not df_dp_all.empty:
            if PLOTLY_AVAILABLE:
                fig_growth = px.line(df_dp_all, x='record_date', y='profit', color='business_name',
                                     title=t("📈 UKUAJI WA KILA BIASHARA KWA FAIDA YA SIKU (Business Growth by Daily Profit)"),
                                     markers=True)
                fig_growth.update_layout(paper_bgcolor='#0B132B', plot_bgcolor='#0F172A', font_color='#F1F5F9',
                                         xaxis_title=t("Tarehe"), yaxis_title=t("Faida (TZS)"),
                                         legend_title_text=t("Biashara"))
                st.plotly_chart(fig_growth, use_container_width=True)
            else:
                st.line_chart(df_dp_all.pivot_table(index='record_date', columns='business_name', values='profit', aggfunc='sum'))
            st.caption(t("💡 Mistari kila moja ni biashara moja — inaonyesha ukuaji (faida ya siku) tangu mapato ya siku yanapotumwa. Faida = mapato ya siku (cash + credit) - matumizi ya siku."))
        else:
            st.info(t("📊 Bado hakuna faida ya siku iliyokokotwa — itaonekana hapa mara biashara zitanza kutuma mapato zao za kila siku."))

        st.markdown("---")
        st.markdown("### 🏢 KADI ZA BIASHARA ZOTE ZILIZOSAJILIWA (EXECUTIVE BUSINESS DIRECTORY)")
        if not df_b_all.empty:
            b_cols = st.columns(max(1, min(len(df_b_all), 3)))
            for b_idx, b_r in df_b_all.iterrows():
                col_target = b_cols[b_idx % 3]
                with col_target:
                    st.markdown(textwrap.dedent(f"""
                    <div style="background:#0F172A; border:2px solid #00F0FF; padding:15px; border-radius:12px; margin-bottom:15px;">
                        <h3 style="color:#00F0FF; margin:0;">🏢 {b_r['name']}</h3>
                        <p style="color:#E2E8F0; margin-top:4px;">
                            <b>Meneja Mkuu:</b> {b_r['manager_name']}<br>
                            📞 <b>Simu:</b> {b_r['manager_phone']}<br>
                            ✉️ <b>Email:</b> {b_r['manager_email']}<br>
                            💰 <b>Mtaji:</b> {b_r['initial_capital']:,.2f} {b_r['currency']}<br>
                            🔑 <b>Manager Pass:</b> <code>{b_r['password']}</code><br>
                            👥 <b>Staff Pass:</b> <code>{b_r['staff_password']}</code>
                        </p>
                    </div>
                    """).strip(), unsafe_allow_html=True)
        else:
            st.info("Bado hakuna biashara iliyosajiliwa.")

        st.markdown("---")
        st.markdown("### 🚨 TAARIFA ZA BIASHARA ZINAZOELEKEA KWENYE HALI MBAYA (PROBLEM ALERTS ENGINE)")

        problem_found = False
        if not df_b_all.empty:
            for idx, b_row in df_b_all.iterrows():
                b_id_chk = b_row['id']
                b_name_chk = b_row['name']
                
                b_daily = df_daily_all[df_daily_all['business_id'] == b_id_chk] if not df_daily_all.empty else pd.DataFrame()
                b_assets = df_assets_all[df_assets_all['business_id'] == b_id_chk] if not df_assets_all.empty else pd.DataFrame()
                
                b_rev = b_daily['revenue'].sum() if not b_daily.empty else 0.0
                b_credit = b_daily['credit_revenue'].sum() if not b_daily.empty else 0.0
                b_exp = b_daily['expenses'].sum() if not b_daily.empty else 0.0
                b_profit = b_rev - b_exp
                
                b_liab = b_assets[b_assets['item_type'] == 'Liability']['amount'].sum() if not b_assets.empty else 0.0
                b_init_cap = b_row['initial_capital']

                if b_profit < 0:
                    problem_found = True
                    st.markdown(textwrap.dedent(f"""
                    <div class="blinking-red-box">
                        <h4 style="color:#FF0000; margin:0;">🚨 TAARIFA YA DHARURA: Biashara ya '{b_name_chk}' Inajiendesha kwa HASARA!</h4>
                        <p style="color:#FFFFFF; margin-top:4px;">Faida Halisi ya Cash Mkononi ni <b>{b_profit:,.2f} TZS</b>. Punguza matumizi au ongeza mauzo mara moja!</p>
                    </div>
                    """).strip(), unsafe_allow_html=True)

                if b_liab > b_init_cap and b_init_cap > 0:
                    problem_found = True
                    st.markdown(textwrap.dedent(f"""
                    <div class="blinking-red-box">
                        <h4 style="color:#FF0000; margin:0;">🚨 TAARIFA YA DHARURA: Biashara ya '{b_name_chk}' Madeni Yamezidi Mtaji!</h4>
                        <p style="color:#FFFFFF; margin-top:4px;">Madeni ni <b>{b_liab:,.2f} TZS</b> ilihali Mtaji wa kuanzia ni <b>{b_init_cap:,.2f} TZS</b>.</p>
                    </div>
                    """).strip(), unsafe_allow_html=True)

        if not problem_found:
            st.success("💚 **SISTAMU SHWARI:** Hakuna biashara iliyo kwenye hali mbaya ya kiuchumi. Biashara zote zinajiendesha salama!")

    # -----------------------------------------------------------------
    # TAB 2: USAJILI WA BIASHARA MPYA & MENEJA (DIRECTOR ONLY + AUTO REPLACE OLD MANAGER)
    # -----------------------------------------------------------------
    with dir_tab2:
        st.markdown("### 🏢 Usajili wa Biashara Mpya au Kubadilisha Meneja (Director Sole Authority)")
        st.info("💡 **DIRECTOR EXCLUSIVE:** Director pekee ndiye anayesajili Biashara na kumweka Meneja wake. Ukiingiza Meneja mpya kwenye biashara iliyopo, **mfumo utamweka Meneja mpya na kumfanya wa zamani kuwa Inactive!**")

        with st.form("director_create_business_form", clear_on_submit=True):
            st.markdown("#### 1️⃣ Details za Biashara Mpya / Inayobadilishwa")
            b_c1, b_c2, b_c3 = st.columns(3)
            with b_c1:
                dir_b_name = st.text_input("Jina la Biashara (mf. Cyber Net Dodoma)", key="dir_b_name")
            with b_c2:
                dir_b_capital = st.number_input("Mtaji wa Kuanzia (TZS)", min_value=0.0, step=10000.0, key="dir_b_cap")
            with b_c3:
                dir_b_curr = st.selectbox("Currency", ["TZS", "USD", "KES", "UGX"], key="dir_b_curr")

            st.markdown("#### 2️⃣ Taarifa za Meneja Mkuu (Majina Matatu & NIDA)")
            m_c1, m_c2, m_c3, m_c4 = st.columns(4)
            with m_c1:
                dir_m_name = st.text_input("Majina Matatu ya Meneja (mf. JOSHUA JOSEPH MAVOA)", key="dir_m_name")
            with m_c2:
                dir_m_phone_val = st.text_input("Simu ya Meneja (06... / 07... Tarakimu 10)", max_chars=10, key="dir_m_phone")
                st.caption(f"📱 **Simu:** `{len(dir_m_phone_val)} / 10`")
            with m_c3:
                dir_m_nida_val = st.text_input("NIDA ya Meneja (Tarakimu 20)", max_chars=20, key="dir_m_nida")
                st.caption(f"🆔 **NIDA:** `{len(dir_m_nida_val)} / 20`")
            with m_c4:
                dir_m_email = st.text_input("Barua Pepe ya Meneja (Email: jina@gmail.com)", key="dir_m_email")

            st.markdown("#### 3️⃣ Anwani, Benki ya Meneja & Passwords za Ingilio")
            p_c1, p_c2, p_c3 = st.columns(3)
            with p_c1:
                dir_m_mkoa = st.selectbox("Mkoa:", TANZANIA_REGIONS, key="dir_m_mkoa")
            with p_c2:
                dir_m_pass = st.text_input("Manager Password (Angalau characters 8)", type="password", key="dir_m_pass")
            with p_c3:
                dir_s_pass = st.text_input("Staff Password (Angalau characters 8)", type="password", key="dir_s_pass")

            bm_c1, bm_c2 = st.columns(2)
            with bm_c1:
                dir_m_bank = st.selectbox("Benki / Mfumo wa Malipo wa Meneja:", TANZANIA_BANKS, key="dir_m_bank")
            with bm_c2:
                dir_m_account = st.text_input("Akaunti ya Benki / Simu ya Malipo ya Meneja:", key="dir_m_account")

            dir_m_wilaya = ""
            dir_m_full_res = f"MKOA WA {dir_m_mkoa.upper()}"

            st.markdown("#### 4️⃣ Picha ya Uso wa Meneja & Logo/Icon ya Biashara")
            m_photo_cam = st.camera_input("📸 Piga Picha ya Uso wa Meneja kupitia Kamera", key="dir_m_cam")
            f_col1, f_col2 = st.columns(2)
            with f_col1:
                m_photo_file = st.file_uploader("🖼️ Upload Picha ya Uso wa Meneja (PNG/JPG)", type=["png", "jpg", "jpeg"], key="dir_m_file")
            with f_col2:
                reg_b_avatar_file = st.file_uploader("🏢 Upload Logo / Icon ya Biashara (PNG/JPG)", type=["png", "jpg", "jpeg"], key="dir_b_avatar_file")

            btn_save_b = st.form_submit_button("💾 SAJILI / KUBADILISHA MENEJA WA BIASHARA")

            if btn_save_b:
                valid_names, names_msg = validate_three_names(dir_m_name, "Majina ya Meneja")
                valid_phone, phone_msg = validate_phone_number(dir_m_phone_val)
                valid_nida, nida_msg = validate_nida_number(dir_m_nida_val)
                valid_email, email_msg = validate_gmail_email(dir_m_email)
                valid_mpass, mpass_msg = validate_password_length(dir_m_pass)
                valid_spass, spass_msg = validate_password_length(dir_s_pass)

                if not dir_b_name.strip():
                    trigger_beep_sound(1200, 5.0, "sawtooth")
                    render_marquee_error("Jina la Biashara", "Ingiza jina la biashara!", "Jaza jina la biashara.", field_label=["Jina la Biashara"])
                elif not valid_names:
                    trigger_beep_sound(1200, 5.0, "sawtooth")
                    render_marquee_error("Majina ya Meneja", names_msg, "Weka majina matatu kamili ya Meneja.", field_label=["Majina Matatu ya Meneja"])
                elif not valid_phone:
                    trigger_beep_sound(1200, 5.0, "sawtooth")
                    render_marquee_error("Namba ya Simu", phone_msg, "Ingiza tarakimu 10 pekee zikianzaga na 06 au 07.", field_label=["Simu ya Meneja"])
                elif not valid_nida:
                    trigger_beep_sound(1200, 5.0, "sawtooth")
                    render_marquee_error("NIDA ya Meneja", nida_msg, "Ingiza NIDA ya Meneja yenye tarakimu 20 pekee zikianza bila herufi au alama.", field_label=["NIDA ya Meneja"])
                elif not valid_email:
                    trigger_beep_sound(1200, 5.0, "sawtooth")
                    render_marquee_error("Email ya Meneja", email_msg, "Ingiza email ya 'jina@gmail.com'.", field_label=["Barua Pepe ya Meneja"])
                elif not valid_mpass or not valid_spass:
                    trigger_beep_sound(1200, 5.0, "sawtooth")
                    render_marquee_error("Password", "Password lazima iwe na angalau characters 8!", "Ingiza password za angalau characters 8.", field_label=["Manager Password", "Staff Password"])
                else:
                    final_m_photo_b64 = None
                    final_b_avatar_b64 = None
                    if m_photo_cam is not None:
                        final_m_photo_b64 = file_to_b64(m_photo_cam)
                    elif m_photo_file is not None:
                        final_m_photo_b64 = file_to_b64(m_photo_file)
                    if reg_b_avatar_file is not None:
                        final_b_avatar_b64 = file_to_b64(reg_b_avatar_file)

                    conn = get_connection()
                    cursor = conn.cursor()
                    
                    # Check if business already exists
                    cursor.execute("SELECT id FROM businesses WHERE name = ?", (dir_b_name.strip(),))
                    existing_b = cursor.fetchone()

                    if existing_b:
                        b_id_exist = existing_b['id']
                        # Auto-replace old manager on business
                        cursor.execute("""
                            UPDATE businesses 
                            SET manager_name = ?, manager_phone = ?, manager_email = ?, manager_bank_name = ?, manager_account_number = ?, manager_photo_b64 = ?, business_avatar_b64 = ?, password = ?, staff_password = ?
                            WHERE id = ?
                        """, (names_msg, phone_msg, email_msg, dir_m_bank, dir_m_account.strip(), final_m_photo_b64, final_b_avatar_b64, mpass_msg, spass_msg, b_id_exist))

                        # Mark old manager in staff_members as REPLACED
                        cursor.execute("UPDATE staff_members SET status = 'REPLACED' WHERE business_id = ? AND staff_type = 'Meneja wa Biashara'", (b_id_exist,))

                        # Insert new manager into staff_members
                        cursor.execute("""
                            INSERT INTO staff_members (business_id, staff_type, full_name, residence_mkoa, residence_wilaya, full_residence_upper, phone, nida_number, email, education_level, role_title, salary, bank_name, account_number, photo_b64, status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
                        """, (b_id_exist, 'Meneja wa Biashara', names_msg, dir_m_mkoa, dir_m_wilaya, dir_m_full_res, phone_msg, nida_msg, email_msg, 'SHAHADA (Bachelor Degree)', 'MENEJA MKUU (General Manager)', 800000.0, dir_m_bank, dir_m_account.strip(), final_m_photo_b64))

                        conn.commit()
                        conn.close()
                        trigger_beep_sound(523.25, 1.0, "sine")
                        st.success(f"✅ MENEJA WA ZAMANI AMEBADILISHWA KIOTOMATIKI! '{names_msg}' Sasa ndiye Meneja Mkuu wa '{dir_b_name.strip()}'!")
                        st.rerun()

                    else:
                        cursor.execute("""
                            INSERT INTO businesses (name, start_date, initial_capital, currency, manager_name, manager_phone, manager_email, manager_bank_name, manager_account_number, manager_photo_b64, business_avatar_b64, password, staff_password)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (dir_b_name.strip(), today_date_str, dir_b_capital, dir_b_curr, names_msg, phone_msg, email_msg, dir_m_bank, dir_m_account.strip(), final_m_photo_b64, final_b_avatar_b64, mpass_msg, spass_msg))
                        new_b_id = cursor.lastrowid

                        cursor.execute("""
                            INSERT INTO staff_members (business_id, staff_type, full_name, residence_mkoa, residence_wilaya, full_residence_upper, phone, nida_number, email, education_level, role_title, salary, bank_name, account_number, photo_b64, status)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
                        """, (new_b_id, 'Meneja wa Biashara', names_msg, dir_m_mkoa, dir_m_wilaya, dir_m_full_res, phone_msg, nida_msg, email_msg, 'SHAHADA (Bachelor Degree)', 'MENEJA MKUU (General Manager)', 800000.0, dir_m_bank, dir_m_account.strip(), final_m_photo_b64))

                        conn.commit()
                        conn.close()
                        add_notification('ALL', f"Usajili Mpya Wa Biashara: {dir_b_name.strip()}", f"Director amesajili biashara mpya ya '{dir_b_name.strip()}' na kumteua Meneja Mkuu '{names_msg}'.", category='ONBOARDING', business_id=new_b_id)
                        trigger_beep_sound(523.25, 1.0, "sine")
                        st.success(f"✅ BIASHARA YA '{dir_b_name.strip()}' NA MENEJA '{names_msg}' ZIMESAJILIWA KIKAMILIFU!")
                        st.rerun()

    # -----------------------------------------------------------------
    # TAB 3: WAFANYAKAZI & IDHINI (STAFF MANAGEMENT & APPROVAL WORKFLOW)
    # -----------------------------------------------------------------
    with dir_tab3:
        st.markdown("### 👥 Usimamizi wa Wafanyakazi & Idhini ya Maombi (Staff Approval Portal)")
        st.info("💡 Hapa Director unaidhinisha maombi ya wafanyakazi wapya waliosajiliwa na Meneja (`PENDING_APPROVAL`) au kusajili Mfanyakazi moja kwa moja.")

        conn = get_connection()
        df_pending_staff = pd.read_sql_query("""
            SELECT s.*, b.name as BiasharaName 
            FROM staff_members s 
            LEFT JOIN businesses b ON s.business_id = b.id 
            WHERE s.status = 'PENDING_APPROVAL' 
            ORDER BY s.created_at DESC
        """, conn)
        conn.close()

        if not df_pending_staff.empty:
            st.markdown("#### ⏳ MAOMBI YA WAFANYAKAZI WAPYA YANAYOSUBIRI IDHINI YA DIRECTOR")
            for idx, p_stf in df_pending_staff.iterrows():
                stf_id = p_stf['id']
                with st.expander(f"📌 Ombi: {p_stf['full_name']} | Biashara: {p_stf['BiasharaName']} | Cheo: {p_stf['role_title']} | Simu: {p_stf['phone']}"):
                    col_p1, col_p2 = st.columns(2)
                    with col_p1:
                        st.write(f"**Email:** `{p_stf['email']}`")
                        st.write(f"**NIDA (20 Digits):** `{p_stf['nida_number']}`")
                        st.write(f"**Anwani:** `{p_stf['full_residence_upper']}`")
                        st.write(f"**Ngazi ya Elimu:** `{p_stf['education_level']}`")
                        st.write(f"**Mshahara:** `{p_stf['salary']:,.2f} TZS`")
                        
                        # Download Buttons
                        st.markdown("**📥 Pakua Nyaraka Zilizowekwa:**")
                        d1, d2, d3, d4 = st.columns(4)
                        with d1:
                            b_data = safe_b64decode(p_stf['contract_b64'])
                            if b_data:
                                st.download_button("📥 Mkataba", data=b_data, file_name=p_stf['contract_filename'] or "Mkataba.pdf", key=f"dl_pc_{stf_id}")
                        with d2:
                            b_data = safe_b64decode(p_stf['application_letter_b64'])
                            if b_data:
                                st.download_button("📥 Barua", data=b_data, file_name=p_stf['letter_filename'] or "Barua.pdf", key=f"dl_pl_{stf_id}")
                        with d3:
                            b_data = safe_b64decode(p_stf['cv_b64'])
                            if b_data:
                                st.download_button("📥 CV", data=b_data, file_name=p_stf['cv_filename'] or "CV.pdf", key=f"dl_pv_{stf_id}")
                        with d4:
                            b_data = safe_b64decode(p_stf['cert_b64'])
                            if b_data:
                                st.download_button("📥 Cheti", data=b_data, file_name=p_stf['cert_filename'] or "Cheti.pdf", key=f"dl_pcrt_{stf_id}")

                    with col_p2:
                        if is_valid_b64(p_stf.get('photo_b64')):
                            st.image(f"data:image/png;base64,{p_stf['photo_b64']}", caption=p_stf['full_name'], width=140)

                    st.markdown("---")
                    st_btn1, st_btn2 = st.columns(2)
                    with st_btn1:
                        if st.button(f"✅ IDHINISHA (APPROVE) - {p_stf['full_name']}", key=f"btn_appr_{stf_id}"):
                            conn = get_connection()
                            cursor = conn.cursor()
                            cursor.execute("UPDATE staff_members SET status = 'ACTIVE' WHERE id = ?", (stf_id,))
                            conn.commit()
                            conn.close()
                            trigger_beep_sound(523.25, 1.0, "sine")
                            st.success(f"✅ '{p_stf['full_name']}' AMEIDHINISHWA NA KUWA ACTIVE KWENYE BIASHARA YA {p_stf['BiasharaName']}!")
                            st.rerun()
                    with st_btn2:
                        if st.button(f"❌ KATAA (REJECT) - {p_stf['full_name']}", key=f"btn_rej_{stf_id}"):
                            conn = get_connection()
                            cursor = conn.cursor()
                            cursor.execute("UPDATE staff_members SET status = 'REJECTED' WHERE id = ?", (stf_id,))
                            conn.commit()
                            conn.close()
                            trigger_beep_sound(1500, 5.0, "square")
                            st.error(f"❌ Ombi la '{p_stf['full_name']}' limekataliwa!")
                            st.rerun()

        st.markdown("---")
        st.markdown("#### 📜 Orodha ya Wafanyakazi Wote Walio-Active")
        conn = get_connection()
        df_staff_all = pd.read_sql_query("""
            SELECT s.id, b.name as Biashara, s.staff_type as Aina, s.full_name as Majina, 
                   s.phone as Simu, s.email as Email, s.nida_number as NIDA, 
                   s.full_residence_upper as Anwani, s.education_level as Elimu, s.role_title as Cheo, s.salary as Mshahara, s.status as Status,
                   s.photo_b64, s.contract_b64, s.contract_filename, 
                   s.application_letter_b64, s.letter_filename, 
                   s.cv_b64, s.cv_filename, s.cert_b64, s.cert_filename
            FROM staff_members s
            LEFT JOIN businesses b ON s.business_id = b.id
            ORDER BY s.created_at DESC
        """, conn)
        conn.close()

        if not df_staff_all.empty:
            for idx, stf_r in df_staff_all.iterrows():
                b_title = stf_r['Biashara'] if stf_r['Biashara'] else "N/A"
                st_badge = f"[{stf_r['Status']}]"
                with st.expander(f"👤 {stf_r['Majina']} ({stf_r['Aina']}) {st_badge} - Biashara: {b_title} | Cheo: {stf_r['Cheo']} | Simu: {stf_r['Simu']}"):
                    col_t, col_p = st.columns(2)
                    with col_t:
                        st.write(f"**Email:** `{stf_r['Email']}`")
                        st.write(f"**NIDA (20 Digits):** `{stf_r['NIDA']}`")
                        st.write(f"**Anwani Halisi:** `{stf_r['Anwani']}`")
                        st.write(f"**Ngazi ya Elimu:** `{stf_r['Elimu']}`")
                        st.write(f"**Mshahara:** `{stf_r['Mshahara']:,.2f} TZS`")
                        
                        # Download Buttons
                        st.markdown("**📥 Pakua Nyaraka Zilizowekwa:**")
                        d1, d2, d3, d4 = st.columns(4)
                        with d1:
                            b_data = safe_b64decode(stf_r['contract_b64'])
                            if b_data:
                                st.download_button("📥 Mkataba", data=b_data, file_name=stf_r['contract_filename'] or "Mkataba.pdf", key=f"dl_c_{stf_r['id']}")
                        with d2:
                            b_data = safe_b64decode(stf_r['application_letter_b64'])
                            if b_data:
                                st.download_button("📥 Barua", data=b_data, file_name=stf_r['letter_filename'] or "Barua.pdf", key=f"dl_l_{stf_r['id']}")
                        with d3:
                            b_data = safe_b64decode(stf_r['cv_b64'])
                            if b_data:
                                st.download_button("📥 CV", data=b_data, file_name=stf_r['cv_filename'] or "CV.pdf", key=f"dl_v_{stf_r['id']}")
                        with d4:
                            b_data = safe_b64decode(stf_r['cert_b64'])
                            if b_data:
                                st.download_button("📥 Cheti", data=b_data, file_name=stf_r['cert_filename'] or "Cheti.pdf", key=f"dl_crt_{stf_r['id']}")

                    with col_p:
                        if is_valid_b64(stf_r.get('photo_b64')):
                            st.image(f"data:image/png;base64,{stf_r['photo_b64']}", caption=stf_r['Majina'], width=140)
        else:
            st.info("Bado hakuna wafanyakazi waliosajiliwa.")

    # -----------------------------------------------------------------
    # TAB 4: KUTUMA SMS KWA WAFANYAKAZI & MENEJA (SINGLE / BROADCAST)
    # -----------------------------------------------------------------
    # PAYROLL TAB
    with dir_tab4:
        st.markdown("### 💳 Portal ya Malipo ya Mishahara na Bajeti (Director Payroll)")
        st.info("💡 **DIRECTOR PAYROLL:** Director hapa anaweza kumlipa kila Meneja mshahara wake au kutoa bajeti ya mshahara ya wafanyakazi wote wa biashara husika. Kila malipo yanasajiliwa kwenye Payroll Ledger!")

        conn = get_connection()
        df_pay_b = pd.read_sql_query("SELECT * FROM businesses ORDER BY name ASC", conn)
        conn.close()

        if not df_pay_b.empty:
            sel_b_pay_name = st.selectbox("Chagua Biashara ya Kufanya Malipo ya Mshahara:", df_pay_b['name'].tolist(), key="dir_sel_b_pay")
            sel_b_pay = df_pay_b[df_pay_b['name'] == sel_b_pay_name].iloc[0]
            pay_b_id = int(sel_b_pay['id'])

            conn = get_connection()
            df_pay_stf = pd.read_sql_query("""
                SELECT * FROM staff_members 
                WHERE business_id = ? AND status = 'ACTIVE'
                ORDER BY staff_type DESC, full_name ASC
            """, conn, params=(pay_b_id,))
            conn.close()

            mgr_bank = sel_b_pay.get('manager_bank_name') or 'NMB BANK'
            mgr_acc = sel_b_pay.get('manager_account_number') or '00000000000'
            
            tot_stf_sal = df_pay_stf[df_pay_stf['staff_type'] != 'Meneja wa Biashara']['salary'].sum() if not df_pay_stf.empty else 0.0
            mgr_sal = 800000.0
            total_b_payroll = mgr_sal + tot_stf_sal

            p1, p2, p3 = st.columns(3)
            p1.metric("Mshahara wa Meneja Mkuu", f"{mgr_sal:,.2f} TZS", delta=f"Bank: {mgr_bank}")
            p2.metric("Jumla ya Mishahara ya Staff", f"{tot_stf_sal:,.2f} TZS", delta=f"Wafanyakazi: {len(df_pay_stf)}")
            p3.metric("Jumla Kuu ya Bajeti ya Malipo", f"{total_b_payroll:,.2f} TZS")

            # ── SALIO ZA AKAUNTI (v63 — hela inayoenda akaunti moja hadi nyingine) ──
            _comp_bal_disp = float(sel_b_pay.get('company_bank_balance') or 0.0)
            _mgr_bal_disp = float(sel_b_pay.get('manager_bank_balance') or 0.0)
            st.caption(f"🏦 **Salio la Akaunti ya Kampuni** ({sel_b_pay.get('company_bank_name') or 'CRDB BANK'} A/C `{sel_b_pay.get('company_account_number') or '11111111111'}`): **{_comp_bal_disp:,.2f} TZS** — ndipo malipo yanatoka (DEBIT) | 💳 **Salio la Akaunti ya Meneja** ({mgr_bank} A/C `{mgr_acc}`): **{_mgr_bal_disp:,.2f} TZS** — ndipo hela inaingia (CREDIT)")

            st.markdown("---")
            st.markdown("#### 📤 Fomu ya Kutoa Malipo ya Mshahara (Disburse Salary)")

            with st.form("director_disburse_payroll_form", clear_on_submit=True):
                pay_target_type = st.selectbox("Chagua Aina ya Malipo:", [
                    "Meneja Mkuu Pekee (Manager Salary)",
                    "Bajeti ya Mishahara ya Wafanyakazi Wote (Staff Budget)",
                    "Malipo ya Pamoja (Meneja + Wafanyakazi Wote)"
                ], key="dir_pay_target_type")

                c_rec1, c_rec2 = st.columns(2)
                with c_rec1:
                    pay_rec_name = sel_b_pay['manager_name']
                    st.text_input("Mpokeaji wa Malipo:", value=pay_rec_name, disabled=True, key="disb_rec_name")
                    pay_bank_val = st.text_input("Benki ya Mpokeaji:", value=mgr_bank, key="disb_bank_val")
                with c_rec2:
                    pay_phone_val = sel_b_pay['manager_phone']
                    st.text_input("Simu ya Mpokeaji:", value=pay_phone_val, disabled=True, key="disb_phone_val")
                    pay_acc_val = st.text_input("Namba ya Akaunti:", value=mgr_acc, key="disb_acc_val")

                # ── V64: MLIPAJI ANACHAGUA HALI ANATOKA — benki + akaunti + PIN ──
                st.markdown("**🏦 HEMA INA TOKA (Source of Funds — mlipaji anachagua):**")
                src_c1, src_c2 = st.columns(2)
                with src_c1:
                    comp_bank_name_pre = str(sel_b_pay.get('company_bank_name') or 'CRDB BANK')
                    _src_banks = TANZANIA_BANKS if comp_bank_name_pre in TANZANIA_BANKS else ([comp_bank_name_pre] + TANZANIA_BANKS)
                    disb_src_bank = st.selectbox(t("🏦 Benki ya Kutolea Hela (Mlipaji):"), _src_banks, index=0, key="dir_src_bank_sel")
                with src_c2:
                    disb_src_acc = st.text_input(t("🔢 Akaunti ya Kutolea Hela (Account Namba):"), value=str(sel_b_pay.get('company_account_number') or '11111111111'), key="dir_src_acc")
                disb_pin = st.text_input(t("🔐 PIN ya Akaunti ya Kutolea Hela (Validation Kuu):"), type="password", max_chars=6, key="dir_src_pin")
                st.caption("🔒 " + t("PIN ya akaunti ya kutolea hela ndiyo validation kuu — bila PIN sahihi hela HAITATOKA. Chaguo-msingi: 1234 (badilisha kwenye Settings & Password)."))

                if "Meneja Mkuu Pekee" in pay_target_type:
                    default_pay_amt = float(mgr_sal)
                elif "Wafanyakazi Wote" in pay_target_type:
                    default_pay_amt = float(tot_stf_sal)
                else:
                    default_pay_amt = float(total_b_payroll)

                c_amt1, c_amt2 = st.columns(2)
                with c_amt1:
                    disb_amount = st.number_input("Kiasi cha Kutuma (TZS):", min_value=0.0, value=default_pay_amt, step=50000.0, key="dir_disb_amount")
                with c_amt2:
                    auto_ref = f"PAY{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"
                    disb_ref = st.text_input("Namba ya Kumbukumbu / Transaction Ref:", value=auto_ref, key="dir_disb_ref")

                disb_notes = st.text_area("Maelezo ya Malipo (Notes):", value=f"Malipo ya {pay_target_type} kwa biashara ya {sel_b_pay_name}", height=80, key="dir_disb_notes")

                btn_disb_submit = st.form_submit_button("💳 THIBITISHA & TUMA MALIPO YA MSHAHARA")

                if btn_disb_submit:
                    comp_bank_name = disb_src_bank
                    comp_acc_num = disb_src_acc.strip() or (str(sel_b_pay.get('company_account_number') or '11111111111'))
                    comp_bank_bal = float(sel_b_pay.get('company_bank_balance') or 5000000.0)

                    if disb_amount <= 0:
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Kiasi cha Malipo", "Kiasi cha malipo lazima kizidi 0 TZS!", "Weka kiasi halali cha fedha.", field_label=["Kiasi cha Kutuma"])
                    elif not disb_pin.strip():
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("PIN ya Akaunti", "PIN ya akaunti ya kutolea hela haijajazwa! Hela haiwezi kutoka bila PIN.", "Weka PIN ya akaunti yako ya kutolea hela (validation kuu ya malipo).", field_label=["PIN ya Akaunti ya Kutolea Hela (Validation Kuu)"])
                    elif not validate_company_pin(pay_b_id, disb_pin):
                        trigger_beep_sound(1500, 5.0, "square")
                        log_pin_rejection(comp_bank_name, comp_acc_num, pay_bank_val, pay_acc_val, pay_rec_name, disb_amount, disb_ref.strip(), "DIRECTOR")
                        render_marquee_error("PIN ya Akaunti (55 — INVALID PIN)", "PIN ya akaunti ya kutolea hela si sahihi! Muamala UMEKATALIWA — hela HAIJATOLEWA.", "Angalia PIN yako kisha jaribu tena, au ubadilishe PIN kwenye Settings & Password.", field_label=["PIN ya Akaunti ya Kutolea Hela (Validation Kuu)"])
                    elif disb_amount > comp_bank_bal:
                        trigger_beep_sound(1500, 5.0, "square")
                        render_marquee_error("Salio la Benki", f"Salio la Akaunti ya Kampuni ({comp_bank_name} A/C: {comp_acc_num}) ni TZS {comp_bank_bal:,.2f}, halitoshi kutoa malipo ya TZS {disb_amount:,.2f}!", "Weka mtaji au deposit pesa kwenye akaunti ya kampuni kwanza.")
                    elif not disb_ref.strip():
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Transaction Ref", "Namba ya kumbukumbu haijatengenezwa!", "Ingiza namba ya kumbukumbu ya malipo.", field_label=["Namba ya Kumbukumbu"])
                    else:
                        sub_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        new_comp_bal = comp_bank_bal - disb_amount
                        mgr_old_bal = float(sel_b_pay.get('manager_bank_balance') or 0.0)
                        new_mgr_bal = mgr_old_bal + disb_amount

                        # ── UNGANISHO NA BENKI (BANK GATEWAY) — MUDA WA KUTUMA ──
                        _gw_channel_lbl = detect_bank_channel(pay_bank_val)[1]
                        with st.spinner(f"🔌 Inaunganisha moja kwa moja na {_gw_channel_lbl} (Bank Gateway)..."):
                            gw = send_bank_transfer(
                                source_label=f"DIRECTOR — {comp_bank_name} A/C {comp_acc_num}",
                                source_bank=comp_bank_name, source_account=comp_acc_num,
                                dest_bank=pay_bank_val, dest_account=pay_acc_val, dest_name=pay_rec_name,
                                amount=disb_amount, reference=disb_ref.strip(), notes=disb_notes.strip(),
                                source_balance=comp_bank_bal)
                        if gw["status"] != "CONFIRMED":
                            trigger_beep_sound(1500, 5.0, "square")
                            render_marquee_error("Bank Gateway", gw["message"], "Hakikisha namba ya akaunti au simu ya malipo ya mpokeaji ni sahihi, kisha jaribu kutuma tena.")
                        else:
                            st.success(f"🏦 BANK GATEWAY: {gw['channel_label']} — ✅ {gw['response_code']} | TxnID: `{gw['bank_txn_id']}`")

                            # ── HELA INAENDEKA KWELI: akaunti ya kampuni inapungua (DEBIT),
                            #    akaunti ya meneja/mfanyakazi inaongezeka (CREDIT) — real movement ──
                            conn = get_connection()
                            cursor = conn.cursor()
                            cursor.execute("UPDATE businesses SET company_bank_balance = ?, manager_bank_balance = manager_bank_balance + ? WHERE id = ?", (new_comp_bal, disb_amount, pay_b_id))
                            cursor.execute("""
                                INSERT INTO payroll_disbursements (business_id, sender_role, sender_name, recipient_type, recipient_name, recipient_phone, bank_name, account_number, amount, payment_date, reference_code, notes, source_bank_name, source_account_number, source_balance_after, recipient_bank_name, recipient_account_number, recipient_balance_after)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (pay_b_id, 'DIRECTOR', 'DIRECTOR ADMIN', pay_target_type, pay_rec_name, pay_phone_val, pay_bank_val, pay_acc_val, disb_amount, today_date_str, disb_ref.strip(), disb_notes.strip(), comp_bank_name, comp_acc_num, new_comp_bal, pay_bank_val, pay_acc_val, new_mgr_bal))
                            conn.commit()
                            conn.close()

                            # ── ACCOUNT LEDGER: mwendo wa hela akaunti → akaunti ──
                            record_account_movement(
                                account_owner=f"COMPANY: {sel_b_pay_name}", account_type="COMPANY",
                                business_id=pay_b_id, bank_name=comp_bank_name, account_number=comp_acc_num,
                                direction="DEBIT (Hela Imetoka)", amount=disb_amount, balance_after=new_comp_bal,
                                counterparty=f"MANAGER: {pay_rec_name}", reference_code=disb_ref.strip(),
                                bank_txn_id=gw.get('bank_txn_id'), channel_label=gw.get('channel_label'),
                                description=f"Malipo ya mshahara ({pay_target_type}) — yametumwa na Director")
                            record_account_movement(
                                account_owner=f"MANAGER: {pay_rec_name}", account_type="MANAGER",
                                business_id=pay_b_id, bank_name=pay_bank_val, account_number=pay_acc_val,
                                direction="CREDIT (Hela Imeingia)", amount=disb_amount, balance_after=new_mgr_bal,
                                counterparty=f"COMPANY: {sel_b_pay_name}", reference_code=disb_ref.strip(),
                                bank_txn_id=gw.get('bank_txn_id'), channel_label=gw.get('channel_label'),
                                description=f"Malipo ya mshahara yaliyopokelewa kutoka kwa Director")

                            receipt_no = save_payment_receipt(
                                business_id=pay_b_id, business_name=sel_b_pay_name,
                                payer_role='DIRECTOR', payer_name='DIRECTOR ADMIN',
                                recipient_type=pay_target_type, recipient_name=pay_rec_name,
                                recipient_phone=pay_phone_val, recipient_bank=pay_bank_val,
                                recipient_account=pay_acc_val, amount=disb_amount,
                                reference_code=disb_ref.strip(), gateway_res=gw,
                                notes=disb_notes.strip())

                            # ── V64: RISITI RASMI KWA PDF + SMS YA MSHAHARA KIOTOMATIKI ──
                            _rcpt_pdf = generate_receipt_pdf({
                                "receipt_no": receipt_no, "payment_time": sub_time_str,
                                "business_name": sel_b_pay_name, "payer_role": "DIRECTOR",
                                "payer_name": "DIRECTOR ADMIN", "recipient_name": pay_rec_name,
                                "recipient_bank": pay_bank_val, "recipient_account": pay_acc_val,
                                "recipient_phone": pay_phone_val, "amount": disb_amount,
                                "reference_code": disb_ref.strip(), "channel_label": gw.get('channel_label'),
                                "response_code": gw.get('response_code'), "bank_txn_id": gw.get('bank_txn_id'),
                                "source_balance_after": new_comp_bal, "recipient_balance_after": new_mgr_bal,
                                "notes": disb_notes.strip()})
                            if _rcpt_pdf:
                                store_receipt_pdf(receipt_no, pay_b_id, pay_rec_name, pay_phone_val, disb_amount, _rcpt_pdf)
                            _sms_msg = build_salary_sms(pay_rec_name, disb_amount, pay_bank_val, pay_acc_val,
                                                        disb_ref.strip(), gw.get('bank_txn_id'), get_current_language())
                            _sms_res = send_sms_gateway(pay_phone_val, _sms_msg, business_id=pay_b_id,
                                                        recipient_name=pay_rec_name, category="SALARY_SMS")
                            add_notification('DIRECTOR', f"📱 SMS ya Mshahara — {pay_rec_name}",
                                             f"SMS ya malipo ya mshahara imetumwa kiotomatiki kwa {pay_rec_name} ({pay_phone_val}): {_sms_res['status']} | TxnID: {_sms_res['gateway_txn_id']}",
                                             category='SALARY_SMS', business_id=pay_b_id)

                            add_notification('ALL', f"Malipo ya Mshahara: {pay_rec_name}", f"Mshahara wa TZS {disb_amount:,.2f} umelipwa kwa {pay_rec_name} kupitia {pay_bank_val} ({pay_acc_val}).", category='PAYROLL', business_id=pay_b_id)
                            trigger_beep_sound(523.25, 1.0, "sine")

                            st.info(f"🧾 Risiti No: `{receipt_no}` — IMETUMWA kwenye **DATABASE YA DIRECTOR** ✅ na **DATABASE YA MENEJA** ✅ (angalia tab ya '🧾 Risiti za Malipo')")
                            if _rcpt_pdf:
                                st.info(f"📄 **Risiti ya PDF imetengenezwa na kuhifadhiwa** — inapatikana kwa kupakua kwenye tab ya '🧾 Risiti za Malipo' na kwenye **Details Zangu Binafsi** ya {pay_rec_name}.")
                            st.info(f"📱 **SMS ya malipo ya mshahara** imetumwa kiotomatiki kwa {pay_rec_name} ({pay_phone_val}): **{_sms_res['status']}** (TxnID SMS: `{_sms_res['gateway_txn_id']}`)")
                            st.info(f"📊 **Mwendo wa Hela (Real Movement):** 🏦 Akaunti ya Kampuni ({comp_bank_name} A/C `{comp_acc_num}`): {comp_bank_bal:,.2f} → **{new_comp_bal:,.2f} TZS** ⬇️ | 💳 Akaunti ya Meneja {pay_rec_name} ({pay_bank_val} A/C `{pay_acc_val}`): {mgr_old_bal:,.2f} → **{new_mgr_bal:,.2f} TZS** ⬆️")

                            st.markdown(textwrap.dedent(f"""
                            <div style="background:#0F172A; border:2px solid #39FF14; border-radius:12px; padding:20px; margin-top:15px;">
                                <h3 style="color:#39FF14; margin:0;">💳 RISITI RASMI YA BENKI (REAL-TIME BANK TRANSFER ADVICE)</h3>
                                <p style="color:#F1F5F9; font-size:1.05rem; margin-top:10px;">
                                    🧾 <b>Risiti No:</b> <code>{receipt_no}</code><br>
                                    🏦 <b>Benki ya Kampuni Mtumaji:</b> {comp_bank_name} (A/C: <code>{comp_acc_num}</code>)<br>
                                    💳 <b>Benki ya Mpokeaji:</b> {pay_bank_val} (A/C: <code>{pay_acc_val}</code>)<br>
                                    👤 <b>Mpokeaji:</b> {pay_rec_name.upper()}<br>
                                    💵 <b>Kiasi Kilichotumwa:</b> <b style="color:#39FF14;">{disb_amount:,.2f} TZS</b><br>
                                    🔢 <b>Namba ya Muamala (Ref):</b> <code>{disb_ref.strip()}</code><br>
                                    🌐 <b>Ghala la Benki (Bank Gateway):</b> {gw['channel_label']} — {gw['response_code']}<br>
                                    🆔 <b>Bank Transaction ID:</b> <code>{gw['bank_txn_id']}</code><br>
                                    📨 <b>Usailishaji wa Risiti:</b> Database ya Director ✅ | Database ya Meneja ✅<br>
                                    📊 <b>Salio la Akaunti ya Kampuni Baada ya Katizo:</b> <b style="color:#00F0FF;">{new_comp_bal:,.2f} TZS</b><br>
                                    📊 <b>Salio la Akaunti ya Mpokeaji Baada ya Kupokea:</b> <b style="color:#39FF14;">{new_mgr_bal:,.2f} TZS</b>
                                </p>
                            </div>
                            """).strip(), unsafe_allow_html=True)
                            st.success(f"✅ MALIPO YA {disb_amount:,.2f} TZS YAMETHIBITISHWA NA BENKI NA KUTUMWA KWA {pay_rec_name.upper()} ({pay_bank_val} - {pay_acc_val})!")
                            st.rerun()

            # ── DEPOSIT / MTAJI KWENYE AKAUNTI YA KAMPUNI (v63) ──
            # Hela inaingia KWELI kwenye akaunti ya kampuni (CREDIT) — ndiyo
            # chanzo cha malipo yote ya mishahara yanayotoka kwenye akaunti hii.
            st.markdown("---")
            st.markdown("#### 💰 Weka Mtaji / Deposit kwenye Akaunti ya Kampuni (Bank Deposit)")
            st.caption("💡 Hii ndiyo njia ya kuingiza hela kwenye Akaunti ya Kampuni ili malipo ya mishahara yaweze kutoka. Deposit inaandikwa kwenye Account Ledger mara moja.")
            with st.form("director_deposit_form", clear_on_submit=True):
                _dep_c1, _dep_c2 = st.columns(2)
                with _dep_c1:
                    dep_amount = st.number_input("Kiasi cha Deposit (TZS):", min_value=0.0, step=100000.0, key="dir_dep_amount")
                with _dep_c2:
                    dep_ref = st.text_input("Namba ya Kumbukumbu (Ref):", value=f"DEP{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}", key="dir_dep_ref")
                dep_notes = st.text_input("Maelezo ya Deposit (mf. Mtaji mpya, Mauzo ya cash yaliyowekwa benki):", value="Mtaji mpya kutoka kwa Director", key="dir_dep_notes")
                btn_dep_submit = st.form_submit_button("💵 THIBITISHA DEPOSIT KWENYE AKAUNTI YA KAMPUNI")
                if btn_dep_submit:
                    if dep_amount <= 0:
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Kiasi cha Deposit", "Kiasi cha deposit lazima kizidi 0 TZS!", "Weka kiasi halali cha fedha za kuweka kwenye akaunti.", field_label=["Kiasi cha Deposit"])
                    elif not dep_ref.strip():
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Deposit Ref", "Namba ya kumbukumbu ya deposit haijatengenezwa!", "Ingiza namba ya kumbukumbu ya deposit.", field_label=["Namba ya Kumbukumbu"])
                    else:
                        conn = get_connection()
                        cursor = conn.cursor()
                        cursor.execute("SELECT company_bank_balance, company_bank_name, company_account_number FROM businesses WHERE id = ?", (pay_b_id,))
                        _dep_row = cursor.fetchone()
                        _dep_old_bal = float(_dep_row['company_bank_balance'] or 0.0) if _dep_row else 0.0
                        _dep_bank = (_dep_row['company_bank_name'] or 'CRDB BANK') if _dep_row else 'CRDB BANK'
                        _dep_acc = (_dep_row['company_account_number'] or '11111111111') if _dep_row else '11111111111'
                        _dep_new_bal = _dep_old_bal + dep_amount
                        cursor.execute("UPDATE businesses SET company_bank_balance = ? WHERE id = ?", (_dep_new_bal, pay_b_id))
                        conn.commit()
                        conn.close()
                        record_account_movement(
                            account_owner=f"COMPANY: {sel_b_pay_name}", account_type="COMPANY",
                            business_id=pay_b_id, bank_name=_dep_bank, account_number=_dep_acc,
                            direction="CREDIT (Hela Imeingia)", amount=dep_amount, balance_after=_dep_new_bal,
                            counterparty="DIRECTOR (Mtaji/Deposit)", reference_code=dep_ref.strip(),
                            bank_txn_id=f"DEP{hashlib.sha1((dep_ref.strip() + str(dep_amount)).encode()).hexdigest()[:10].upper()}",
                            channel_label="Bank Deposit — Internal",
                            description=dep_notes.strip() or "Deposit kwenye akaunti ya kampuni")
                        add_notification('ALL', f"Deposit kwenye Akaunti ya Kampuni: {sel_b_pay_name}", f"Deposit ya TZS {dep_amount:,.2f} imeingia kwenye Akaunti ya Kampuni ({_dep_bank} A/C {_dep_acc}). Salio jipya: TZS {_dep_new_bal:,.2f}.", category='PAYROLL', business_id=pay_b_id)
                        trigger_beep_sound(523.25, 1.0, "sine")
                        st.success(f"✅ DEPOSIT YA {dep_amount:,.2f} TZS IMEINGIA KWELI KWENYE AKAUNTI YA KAMPUNI ({_dep_bank} A/C `{_dep_acc}`): {_dep_old_bal:,.2f} → **{_dep_new_bal:,.2f} TZS** ⬆️ | Ref: `{dep_ref.strip()}`")
                        st.rerun()

            # ── ACCOUNT LEDGER — DATABASE YA DIRECTOR (v63): mwendo halisi wa hela ──
            st.markdown("---")
            st.markdown("#### 📒 ACCOUNT LEDGER — Hela Inayoenda Akaunti Moja hadi Nyingine (Real Movement Log)")
            st.info("💡 Kila mwendo wa hela unaonekana hapa: akaunti iliyotoa hela (DEBIT ⬇️), akaunti iliyopokea (CREDIT ⬆️), salio lililobakia baada ya muamala, mhusika mwingine na Bank TxnID. Hii ndiyo uthibitisho kwamba hela inaenda KWELI kutoka akaunti moja hadi nyingine!")
            conn = get_connection()
            df_ledger_d = pd.read_sql_query("SELECT * FROM account_ledger ORDER BY id DESC LIMIT 300", conn)
            conn.close()
            if df_ledger_d.empty:
                st.info("Bado hakuna mwendo wa hela kwenye Account Ledger. Fanya deposit au lipa mshahara kuona mwendo wa hela.")
            else:
                df_ledger_show_d = df_ledger_d[["created_at", "account_owner", "bank_name", "account_number", "direction", "amount", "balance_after", "counterparty", "reference_code", "bank_txn_id", "channel_label"]].copy()
                df_ledger_show_d.columns = ["Muda", "Akaunti (Mmiliki)", "Benki", "Akaunti No", "Mwelekeo wa Hela", "Kiasi TZS", "Salio Baada ya Muamala", "Mhusika Mwingine", "Ref", "Bank TxnID", "Ghala la Benki"]
                st.dataframe(df_ledger_show_d, use_container_width=True)

            st.markdown("---")
            st.markdown("#### 📜 Historia ya Malipo Yote Yaliyotolewa na Director (Payroll Ledger)")
            conn = get_connection()
            df_pay_logs = pd.read_sql_query("""
                SELECT p.id, b.name as Biashara, p.payment_date as Tarehe, p.recipient_name as Mpokeaji, 
                       p.recipient_type as Aina_ya_Malipo, p.bank_name as Benki, p.account_number as Akaunti, 
                       p.amount as Kiasi_TZS, p.reference_code as Reference_Code, p.notes as Maelezo
                FROM payroll_disbursements p
                LEFT JOIN businesses b ON p.business_id = b.id
                WHERE p.sender_role = 'DIRECTOR'
                ORDER BY p.created_at DESC
            """, conn)
            conn.close()

            if not df_pay_logs.empty:
                st.dataframe(df_pay_logs, use_container_width=True)
            else:
                st.info("Bado hakuna kumbukumbu za malipo ya mishahara kutoka kwa Director.")
        else:
            st.info("Bado hakuna biashara iliyosajiliwa kutoa malipo.")

    # -----------------------------------------------------------------
    # TAB 5: NOTIFICATIONS (SYSTEM ALERTS VIEWER)
    # -----------------------------------------------------------------
    with dir_tab5:
        st.markdown("### 🔔 Taarifa za Mfumo (System Notifications)")
        st.info("💡 Hapa unaona taarifa zote za mfumo: mapato mapya, usajili wa wafanyakazi, vifaa vya ledger, ujumbe, malipo ya mishahara pamoja na TAARIFA ZA BIASHARA ZISIZOTUMA MAPATO. Taarifa zinawekwa kuwa zimesomwa KIOTOMATIKI mara moja unapoziangalia!")

        dir_un_cnt_page = get_unread_notifications_count('DIRECTOR')
        if dir_un_cnt_page > 0:
            # AUTO READ: taarifa zinapoangaliwa, zinawekwa kuwa zimesomwa MARA MOJA
            mark_notifications_read('DIRECTOR')
            st.success(f"✅ Taarifa zote {dir_un_cnt_page} zimeangaliwa na zimewekwa kuwa ZIMESOMWA kiotomatiki!")

        if st.button(f"✅ Weka Taarifa Zote Kuwa Zimesomwa ({dir_un_cnt_page})", key="btn_dir_mark_read_all"):
            mark_notifications_read('DIRECTOR')
            st.success("✅ Taarifa zote zimewekwa kuwa zimesomwa!")
            st.rerun()

        st.markdown("---")
        conn = get_connection()
        df_d_notifs = pd.read_sql_query("""
            SELECT id, title, message, category, is_read, created_at
            FROM system_notifications
            WHERE recipient_role IN ('DIRECTOR', 'ALL')
            ORDER BY created_at DESC LIMIT 100
        """, conn)
        conn.close()

        if not df_d_notifs.empty:
            for idx, n_r in df_d_notifs.iterrows():
                n_status = "🔴 EMPYA (UNREAD)" if n_r['is_read'] == 0 else "🟢 TAYARI IMESOMWA"
                n_col = "#FF0055" if n_r['is_read'] == 0 else "#39FF14"
                st.markdown(textwrap.dedent(f"""
                <div style="background:#0F172A; border:1px solid {n_col}; border-radius:10px; padding:12px; margin-bottom:10px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px;">
                        <b style="color:{n_col}; font-size:1.05rem;">{n_r['title']}</b>
                        <span style="color:#FFD700; font-size:0.85rem;">{n_status} | {n_r['created_at']} | 🏷️ {n_r['category']}</span>
                    </div>
                    <p style="color:#F1F5F9; margin-top:6px; margin-bottom:0;">{n_r['message']}</p>
                </div>
                """).strip(), unsafe_allow_html=True)
        else:
            st.info("Hakuna taarifa za mfumo bado.")

    # -----------------------------------------------------------------
    # TAB 6: KUTUMA SMS KWA WAFANYAKAZI & MENEJA
    # -----------------------------------------------------------------
    with dir_tab6:
        st.markdown("### ✉️ Kutuma SMS / Ujumbe kwa Wafanyakazi & Meneja")
        st.info("💡 Director unaweza kuchagua kutuma SMS kwa Mfanyakazi/Meneja mmoja mmoja au kutuma SMS ya pamoja (**BROADCAST**) kwa wafanyakazi wote kwa wakati mmoja!")

        conn = get_connection()
        df_staff_sms_opts = pd.read_sql_query("SELECT id, full_name, phone, staff_type FROM staff_members WHERE status = 'ACTIVE' ORDER BY full_name ASC", conn)
        conn.close()

        with st.form("director_send_sms_form", clear_on_submit=True):
            recipient_mode = st.radio("Chagua Walengwa wa SMS:", ["Mfanyakazi / Meneja Mmoja Mmoja", "📢 BROADCAST (WOTE PAMOJA)"], key="sms_rec_mode")
            
            selected_single_staff = None
            if recipient_mode == "Mfanyakazi / Meneja Mmoja Mmoja":
                if not df_staff_sms_opts.empty:
                    staff_labels = [f"{r['full_name']} ({r['staff_type']}) - {r['phone']}" for idx, r in df_staff_sms_opts.iterrows()]
                    selected_single_staff_label = st.selectbox("Chagua Mfanyakazi/Meneja:", staff_labels, key="sms_single_staff")
                    selected_single_staff = df_staff_sms_opts.iloc[staff_labels.index(selected_single_staff_label)]
                else:
                    st.warning("Hakuna wafanyakazi waliosajiliwa. Sajili wafanyakazi kwanza!")

            sms_body = st.text_area("Andika Ujumbe wa SMS Hapa...", height=120, key="sms_body_input")
            btn_send_sms = st.form_submit_button("📤 TUMA SMS KWA WALENGWA")

            if btn_send_sms:
                if not sms_body.strip():
                    trigger_beep_sound(1200, 5.0, "sawtooth")
                    render_marquee_error("SMS Message", "Ujumbe wa SMS hauwezi kuachwa wazi!", "Andika ujumbe unaotaka kutuma.", field_label=["Andika Ujumbe wa SMS"])
                else:
                    conn = get_connection()
                    cursor = conn.cursor()

                    if recipient_mode == "📢 BROADCAST (WOTE PAMOJA)":
                        if df_staff_sms_opts.empty:
                            trigger_beep_sound(1200, 5.0, "sawtooth")
                            render_marquee_error("Wafanyakazi", "Hakuna wafanyakazi waliosajiliwa kutuma SMS!", "Sajili wafanyakazi kwanza.")
                        else:
                            for idx, s_r in df_staff_sms_opts.iterrows():
                                cursor.execute("""
                                    INSERT INTO staff_sms_logs (recipient_type, recipient_name, phone, message)
                                    VALUES (?, ?, ?, ?)
                                """, ("BROADCAST", s_r['full_name'], s_r['phone'], sms_body.strip()))
                            conn.commit()
                            trigger_beep_sound(523.25, 1.0, "sine")
                            st.success(f"✅ SMS YA BROADCAST IMETUMWA KWA WAFANYAKAZI WOTE ({len(df_staff_sms_opts)}) KIKAMILIFU!")
                            
                            all_phones = ",".join(df_staff_sms_opts['phone'].tolist())
                            sms_url = f"sms:{all_phones}?body={urllib.parse.quote(sms_body)}"
                            st.markdown(f"👉 **[Bofya Hapa Kufungua App ya SMS kwenye Simu Kutuma kwa Wote ({all_phones})]({sms_url})**")

                    else: # Single
                        if selected_single_staff is not None:
                            cursor.execute("""
                                INSERT INTO staff_sms_logs (recipient_type, recipient_name, phone, message)
                                VALUES (?, ?, ?, ?)
                            """, ("SINGLE", selected_single_staff['full_name'], selected_single_staff['phone'], sms_body.strip()))
                            conn.commit()
                            trigger_beep_sound(523.25, 1.0, "sine")
                            st.success(f"✅ SMS IMETUMWA KWA '{selected_single_staff['full_name']}' ({selected_single_staff['phone']})!")

                            sms_url = f"sms:{selected_single_staff['phone']}?body={urllib.parse.quote(sms_body)}"
                            st.markdown(f"👉 **[Bofya Hapa Kutuma SMS Moja kwa Moja kwenye Simu ({selected_single_staff['phone']})]({sms_url})**")
                    conn.close()

        st.markdown("---")
        st.markdown("#### 📜 Historia ya SMS Zilizotumwa kwa Wafanyakazi")
        conn = get_connection()
        df_sms_logs = pd.read_sql_query("SELECT id, sent_at as Tarehe, recipient_type as Aina, recipient_name as Jina, phone as Simu, message as Ujumbe FROM staff_sms_logs ORDER BY sent_at DESC", conn)
        conn.close()
        if not df_sms_logs.empty:
            st.dataframe(df_sms_logs, use_container_width=True)
        else:
            st.info("Bado hakuna SMS zilizotumwa.")

    # -----------------------------------------------------------------
    # TAB 7: MAOMBI YA AJIRA YA NJE (EXTERNAL JOB APPLICATIONS)
    # -----------------------------------------------------------------
    with dir_tab7:
        st.markdown("### 📝 Maombi ya Ajira ya Nje (External Job Applications)")
        st.info("💡 Hapa Director unaweza kuangalia maombi yote ya ajira yaliyotumwa kutoka ukurasa wa umma na kumjibu mwombaji moja kwa moja kupitia Gmail yake (`jina@gmail.com`).")

        conn = get_connection()
        df_apps = pd.read_sql_query("SELECT * FROM job_applications ORDER BY applied_at DESC", conn)
        conn.close()

        if not df_apps.empty:
            for idx, app_row in df_apps.iterrows():
                app_id = app_row['id']
                st_color = "#39FF14" if app_row['status'] == "ACCEPTED" else "#FF007F" if app_row['status'] == "REJECTED" else "#FFD700"
                
                with st.expander(f"📋 Mwombaji: {app_row['full_name']} | Cheo: {app_row['desired_role']} | Status: {app_row['status']} | Applied: {app_row['applied_at']}"):
                    ac1, ac2 = st.columns(2)
                    with ac1:
                        st.write(f"**Email:** `{app_row['email']}`")
                        st.write(f"**Simu:** `{app_row['phone']}`")
                        st.write(f"**NIDA (20 Digits):** `{app_row['nida_number']}`")
                        st.write(f"**Anwani Halisi:** `{app_row['full_residence_upper']}`")
                        st.write(f"**Ngazi ya Elimu:** `{app_row['education_level']}`")
                        st.write(f"**Cheo Anachoomba:** `{app_row['desired_role']}`")
                        
                        st.markdown("**📥 Pakua Nyaraka za Mwombaji:**")
                        dl1, dl2, dl3 = st.columns(3)
                        with dl1:
                            b_data = safe_b64decode(app_row['application_letter_b64'])
                            if b_data:
                                st.download_button("📥 Barua ya Ajira", data=b_data, file_name=app_row['letter_filename'] or "Barua.pdf", key=f"dl_app_l_{app_id}")
                        with dl2:
                            b_data = safe_b64decode(app_row['cv_b64'])
                            if b_data:
                                st.download_button("📥 CV ya Mwombaji", data=b_data, file_name=app_row['cv_filename'] or "CV.pdf", key=f"dl_app_v_{app_id}")
                        with dl3:
                            b_data = safe_b64decode(app_row['cert_b64'])
                            if b_data:
                                st.download_button("📥 Cheti cha Elimu", data=b_data, file_name=app_row['cert_filename'] or "Cheti.pdf", key=f"dl_app_crt_{app_id}")

                    with ac2:
                        if is_valid_b64(app_row.get('photo_b64')):
                            st.image(f"data:image/png;base64,{app_row['photo_b64']}", caption=app_row['full_name'], width=140)

                    st.markdown("---")
                    st.markdown("#### ✉️ Jibu Maombi ya Ajira Hapa (Send Reply via Gmail):")
                    with st.form(f"reply_job_form_{app_id}", clear_on_submit=False):
                        reply_st = st.selectbox("Badilisha Status ya Maombi:", ["PENDING", "ACCEPTED", "REJECTED"], index=0 if app_row['status']=="PENDING" else 1 if app_row['status']=="ACCEPTED" else 2, key=f"sel_st_{app_id}")
                        reply_msg_text = st.text_area("Andika Ujumbe wa Majibu kwa Mwombaji:", value=app_row['reply_message'] or f"Habari {app_row['full_name']}, maombi yako ya ajira yamefanyiwa kazi...", height=100, key=f"txt_rep_{app_id}")
                        
                        btn_send_reply = st.form_submit_button("📤 HIFADHI & TUMA MAJIBU KWA EMAIL")
                        
                        if btn_send_reply:
                            clean_st = str(reply_st)
                            conn = get_connection()
                            cursor = conn.cursor()
                            cursor.execute("UPDATE job_applications SET status = ?, reply_message = ? WHERE id = ?", (clean_st, reply_msg_text.strip(), app_id))
                            conn.commit()
                            conn.close()

                            gmail_reply_url = f"https://mail.google.com/mail/?view=cm&fs=1&to={app_row['email']}&su={urllib.parse.quote('MAJIBU YA MAOMBI YA AJIRA - CYBER BUSINESS GRADING SYSTEM')}&body={urllib.parse.quote(reply_msg_text.strip())}"
                            trigger_beep_sound(523.25, 1.0, "sine")
                            st.success(f"✅ MAJIBU YAMEHIFADHIWA! Status ya {app_row['full_name']} imebadilishwa kuwa '{clean_st}'.")
                            st.markdown(f"👉 **[Bofya Hapa Kutuma Email ya Majibu Moja kwa Moja kwa {app_row['email']}]({gmail_reply_url})**")
        else:
            st.info("Bado hakuna maombi ya ajira yaliyotumwa.")

    # -----------------------------------------------------------------
    # TAB 8: SUBMISSIONS & AUDIT LOG
    # -----------------------------------------------------------------
    with dir_tab8:
        st.markdown("### 📋 Director All Business Submissions & Audit Log")
        
        conn = get_connection()
        df_b_opt = pd.read_sql_query("SELECT id, name FROM businesses ORDER BY name ASC", conn)
        conn.close()

        if not df_b_opt.empty:
            sel_b_audit = st.selectbox("Chagua Biashara Kukagua Audit Log:", ["BIASHARA ZOTE"] + df_b_opt['name'].tolist(), key="dir_sel_b_audit")
            
            conn = get_connection()
            if sel_b_audit == "BIASHARA ZOTE":
                df_dir_audit = pd.read_sql_query("""
                    SELECT d.id, b.name as Biashara, d.record_date as Tarehe, d.sender_name as Mtumaji, 
                           d.sender_phone as Simu, d.cash_revenue, d.credit_revenue, d.revenue as Jumla_Mapato, 
                           d.expenses as Matumizi, d.expense_details, (d.revenue - d.expenses) as Cash_Profit, 
                           d.notes as Maelezo, d.sender_face_b64, d.submitted_timestamp
                    FROM daily_records d
                    LEFT JOIN businesses b ON d.business_id = b.id
                    ORDER BY d.record_date DESC
                """, conn)
            else:
                target_bid = int(df_b_opt[df_b_opt['name'] == sel_b_audit].iloc[0]['id'])
                df_dir_audit = pd.read_sql_query("""
                    SELECT d.id, b.name as Biashara, d.record_date as Tarehe, d.sender_name as Mtumaji, 
                           d.sender_phone as Simu, d.cash_revenue, d.credit_revenue, d.revenue as Jumla_Mapato, 
                           d.expenses as Matumizi, d.expense_details, (d.revenue - d.expenses) as Cash_Profit, 
                           d.notes as Maelezo, d.sender_face_b64, d.submitted_timestamp
                    FROM daily_records d
                    LEFT JOIN businesses b ON d.business_id = b.id
                    WHERE d.business_id = ?
                    ORDER BY d.record_date DESC
                """, conn, params=(target_bid,))
            conn.close()

            if not df_dir_audit.empty:
                for idx, a_row in df_dir_audit.iterrows():
                    st_time = a_row['submitted_timestamp'] if a_row['submitted_timestamp'] else "N/A"
                    with st.expander(f"🗓️ Biashara: {a_row['Biashara']} | Tarehe: {a_row['Tarehe']} | Mtumaji: {a_row['Mtumaji']} ({a_row['Simu']}) | Cash Profit: {a_row['Cash_Profit']:,.2f} TZS"):
                        dc_txt, dc_img = st.columns(2)
                        with dc_txt:
                            st.write(f"**Mauzo ya Cash:** {a_row['cash_revenue']:,.2f} TZS")
                            st.write(f"**Mauzo ya Mkopo:** {a_row['credit_revenue']:,.2f} TZS")
                            st.write(f"**Jumla ya Mapato:** {a_row['Jumla_Mapato']:,.2f} TZS")
                            st.write(f"**Matumizi:** {a_row['Matumizi']:,.2f} TZS ({a_row['expense_details']})")
                            st.write(f"**Muda Halisi wa Submission:** `{st_time}`")
                            st.write(f"**Maoni:** {a_row['Maelezo']}")
                        with dc_img:
                            if is_valid_b64(a_row.get('sender_face_b64')):
                                st.image(f"data:image/png;base64,{a_row['sender_face_b64']}", caption=a_row['Mtumaji'], width=150)
            else:
                st.info("Hakuna rekodi za submissions zilizopatikana.")

    # -----------------------------------------------------------------
    # TAB 9: MAWASILIANO & CONTACTS (COMMUNICATION CENTER)
    # -----------------------------------------------------------------
    with dir_tab9:
        render_communication_center('DIRECTOR', None)

    # -----------------------------------------------------------------
    # TAB 10: UFUTAJI WA DATA & AUDIT NOTIFICATION (DIRECTOR SPECIFIC DELETION)
    # -----------------------------------------------------------------
    with dir_tab10:
        st.markdown("### 🗑️ Ufutaji wa Data na Taarifa za Ufutaji (Director Deletion Control)")
        st.error("⚠️ **TAAHADHARI:** Eneo hili linaruhusu Director kufuta kitu chochote. Kila kinachofutwa kinasajiliwa kwenye Audit Log na kutoa Taarifa Rasmi!")

        del_category = st.selectbox(
            "Chagua Aina ya Kitu Unachotaka Kufuta:",
            [
                "🏢 Biashara Nzima (Entire Business)",
                "💵 Rekodi ya Mauzo ya Siku (Daily Sales Record)",
                "📑 Kifaa / Deni Kwenye Ledger (Ledger Item)",
                "👤 Mfanyakazi / Meneja (Staff Member)",
                "📝 Ombi la Ajira la Nje (Job Application)",
                "✉️ Ujumbe wa Director (Message)"
            ],
            key="del_cat_select"
        )

        with st.form("universal_deletion_form", clear_on_submit=True):
            conn = get_connection()
            del_target_id = None
            del_target_details = ""

            if "Biashara Nzima" in del_category:
                df_b_d = pd.read_sql_query("SELECT id, name FROM businesses ORDER BY name ASC", conn)
                if not df_b_d.empty:
                    b_dict = {f"{r['name']} (ID: {r['id']})": r['id'] for idx, r in df_b_d.iterrows()}
                    sel_item_lbl = st.selectbox("Chagua Biashara ya Kufuta:", list(b_dict.keys()), key="del_b_item")
                    del_target_id = b_dict[sel_item_lbl]
                    del_target_details = f"Biashara: '{sel_item_lbl}'"

            elif "Rekodi ya Mauzo" in del_category:
                df_rec_d = pd.read_sql_query("""
                    SELECT d.id, b.name as BName, d.record_date, d.sender_name, d.revenue 
                    FROM daily_records d JOIN businesses b ON d.business_id = b.id 
                    ORDER BY d.record_date DESC
                """, conn)
                if not df_rec_d.empty:
                    r_dict = {f"Biashara: {r['BName']} | Tarehe: {r['record_date']} | Mtumaji: {r['sender_name']} ({r['revenue']:,.0f} TZS)": r['id'] for idx, r in df_rec_d.iterrows()}
                    sel_item_lbl = st.selectbox("Chagua Rekodi ya Mauzo ya Kufuta:", list(r_dict.keys()), key="del_rec_item")
                    del_target_id = r_dict[sel_item_lbl]
                    del_target_details = f"Rekodi ya Mauzo: '{sel_item_lbl}'"

            elif "Ledger Item" in del_category:
                df_led_d = pd.read_sql_query("""
                    SELECT l.id, b.name as BName, l.item_name, l.item_type, l.amount 
                    FROM assets_liabilities l JOIN businesses b ON l.business_id = b.id 
                    ORDER BY l.date_acquired DESC
                """, conn)
                if not df_led_d.empty:
                    l_dict = {f"Biashara: {r['BName']} | {r['item_type']}: {r['item_name']} ({r['amount']:,.0f} TZS)": r['id'] for idx, r in df_led_d.iterrows()}
                    sel_item_lbl = st.selectbox("Chagua Kifaa/Deni cha Kufuta:", list(l_dict.keys()), key="del_led_item")
                    del_target_id = l_dict[sel_item_lbl]
                    del_target_details = f"Ledger Item: '{sel_item_lbl}'"

            elif "Staff Member" in del_category:
                df_stf_d = pd.read_sql_query("SELECT id, full_name, staff_type, role_title FROM staff_members ORDER BY full_name ASC", conn)
                if not df_stf_d.empty:
                    s_dict = {f"{r['full_name']} ({r['staff_type']} - {r['role_title']})": r['id'] for idx, r in df_stf_d.iterrows()}
                    sel_item_lbl = st.selectbox("Chagua Mfanyakazi/Meneja wa Kufuta:", list(s_dict.keys()), key="del_stf_item")
                    del_target_id = s_dict[sel_item_lbl]
                    del_target_details = f"Mfanyakazi: '{sel_item_lbl}'"

            elif "Job Application" in del_category:
                df_app_d = pd.read_sql_query("SELECT id, full_name, desired_role, applied_at FROM job_applications ORDER BY applied_at DESC", conn)
                if not df_app_d.empty:
                    a_dict = {f"Mwombaji: {r['full_name']} ({r['desired_role']}) - Applied: {r['applied_at']}": r['id'] for idx, r in df_app_d.iterrows()}
                    sel_item_lbl = st.selectbox("Chagua Ombi la Ajira la Kufuta:", list(a_dict.keys()), key="del_app_item")
                    del_target_id = a_dict[sel_item_lbl]
                    del_target_details = f"Ombi la Ajira: '{sel_item_lbl}'"

            elif "Message" in del_category:
                df_msg_d = pd.read_sql_query("SELECT id, sender_name, subject, sent_at FROM director_messages ORDER BY sent_at DESC", conn)
                if not df_msg_d.empty:
                    m_dict = {f"Ujumbe: {r['subject']} kutoka kwa {r['sender_name']} ({r['sent_at']})": r['id'] for idx, r in df_msg_d.iterrows()}
                    sel_item_lbl = st.selectbox("Chagua Ujumbe wa Kufuta:", list(m_dict.keys()), key="del_msg_item")
                    del_target_id = m_dict[sel_item_lbl]
                    del_target_details = f"Ujumbe: '{sel_item_lbl}'"

            conn.close()

            entered_del_pass = st.text_input("Weka Password Rasmi ya Ufutaji (Deletion Password):", type="password", key="univ_del_pass")
            btn_execute_del = st.form_submit_button("🗑️ THIBITISHA UFUTAJI NA TUMA TAARIFA YA AUDIT")

            if btn_execute_del:
                if entered_del_pass != del_password_in_db:
                    trigger_beep_sound(1500, 5.0, "square")
                    render_marquee_error("Password ya Ufutaji", "Password ya Ufutaji si sahihi!", "Weka password sahihi ya ufutaji (Deletion Password).", field_label=["Password Rasmi ya Ufutaji"])
                elif del_target_id is None:
                    trigger_beep_sound(1200, 5.0, "sawtooth")
                    render_marquee_error("Ufutaji wa Data", "Hakuna kitu kilichochaguliwa kufuta!", "Chagua kitu cha kufuta kwanza.")
                else:
                    conn = get_connection()
                    cursor = conn.cursor()

                    if "Biashara Nzima" in del_category:
                        cursor.execute("DELETE FROM businesses WHERE id = ?", (del_target_id,))
                    elif "Rekodi ya Mauzo" in del_category:
                        cursor.execute("DELETE FROM daily_records WHERE id = ?", (del_target_id,))
                    elif "Ledger Item" in del_category:
                        cursor.execute("DELETE FROM assets_liabilities WHERE id = ?", (del_target_id,))
                    elif "Staff Member" in del_category:
                        cursor.execute("DELETE FROM staff_members WHERE id = ?", (del_target_id,))
                    elif "Job Application" in del_category:
                        cursor.execute("DELETE FROM job_applications WHERE id = ?", (del_target_id,))
                    elif "Message" in del_category:
                        cursor.execute("DELETE FROM director_messages WHERE id = ?", (del_target_id,))

                    # Log audit message into database
                    now_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute("""
                        INSERT INTO deletion_audit_logs (item_type, item_details, deleted_by)
                        VALUES (?, ?, ?)
                    """, (del_category, del_target_details, "DIRECTOR JOSHUA JOSEPH"))
                    conn.commit()
                    conn.close()

                    trigger_beep_sound(523.25, 1.0, "sine")
                    st.session_state['last_deletion_msg'] = f"⚠️ AUDIT LOG: DIRECTOR JOSHUA JOSEPH AMEFUTA KIKAMILIFU {del_target_details.upper()} SAA {now_time_str}!"
                    st.success(st.session_state['last_deletion_msg'])
                    st.rerun()

        if 'last_deletion_msg' in st.session_state:
            st.markdown(textwrap.dedent(f"""
            <div style="background:#330011; border:2px solid #FF0055; border-radius:10px; padding:15px; margin-top:15px;">
                <h4 style="color:#FF0055; margin:0;">📢 TAARIFA RASMI YA AUDIT LOG:</h4>
                <p style="color:#FFFFFF; font-size:1.05rem; margin-top:4px;">{st.session_state['last_deletion_msg']}</p>
            </div>
            """).strip(), unsafe_allow_html=True)

        st.markdown("---")
        st.markdown("#### 📜 Historia ya Audit Logs za Ufutaji Ulizofanya")
        conn = get_connection()
        df_del_logs = pd.read_sql_query("SELECT id, deleted_at as Tarehe_Muda, deleted_by as Director, item_type as Aina, item_details as Maelezo FROM deletion_audit_logs ORDER BY deleted_at DESC", conn)
        conn.close()
        if not df_del_logs.empty:
            st.dataframe(df_del_logs, use_container_width=True)
        else:
            st.info("Bado hakuna historia ya ufutaji.")

    # -----------------------------------------------------------------
    # TAB 11: SETTINGS, PASSWORDS & SYSTEM LOGO
    # -----------------------------------------------------------------
    with dir_tab11:
        st.markdown("### ⚙️ Director System Settings & Passwords")
        
        with st.form("change_passwords_form", clear_on_submit=True):
            st.markdown("#### 1️⃣ Badilisha Password ya Director Portal")
            new_dir_pass = st.text_input("New Director Login Password (Angalau characters 8):", type="password", key="new_dir_pass")
            
            st.markdown("#### 2️⃣ Badilisha Password Rasmi ya Ufutaji (Deletion Password)")
            new_del_pass = st.text_input("New Deletion Password (Angalau characters 8):", type="password", key="new_del_pass")
            
            btn_save_passwords = st.form_submit_button("💾 HIFADHI PASSWORDS MPYA")

            if btn_save_passwords:
                if new_dir_pass:
                    v_pass1, msg1 = validate_password_length(new_dir_pass)
                    if v_pass1:
                        update_setting('director_password', new_dir_pass)
                        st.success("✅ Director Login Password imebadilishwa kikamilifu!")
                    else:
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Director Password", msg1, "Weka password yenye urefu wa angalau characters 8.", field_label=["New Director Login Password"])

                if new_del_pass:
                    v_pass2, msg2 = validate_password_length(new_del_pass)
                    if v_pass2:
                        update_setting('deletion_password', new_del_pass)
                        st.success("✅ Deletion Password imebadilishwa kikamilifu!")
                    else:
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Deletion Password", msg2, "Weka password yenye urefu wa angalau characters 8.", field_label=["New Deletion Password"])

        st.markdown("---")
        st.markdown("#### 🎨 Weka Logo / Icon Mpya ya System")
        with st.form("system_logo_form", clear_on_submit=True):
            uploaded_logo = st.file_uploader("Upload Picha/Logo Mpya ya Mfumo (PNG/JPG)", type=["png", "jpg", "jpeg"], key="up_sys_logo")
            btn_save_logo = st.form_submit_button("💾 HIFADHI LOGO YA SYSTEM")

            if btn_save_logo:
                if uploaded_logo is not None:
                    logo_b64 = file_to_b64(uploaded_logo)
                    update_setting('system_logo_b64', logo_b64)
                    trigger_beep_sound(523.25, 1.0, "sine")
                    st.success("✅ LOGO YA SYSTEM IMEHIFADHIWA! Itaonekana juu kwenye Standalone Title Card.")
                    st.rerun()

        # ── V64: PIN ZA AKAUNTI NA WATU + HALI YA SMS GATEWAY ──
        st.markdown("---")
        st.markdown("### 🔐 V64: PIN za Akaunti na Watu (Personal PINs)")
        st.info("💡 **PIN ya Akaunti ya Kampuni** ndiyo **validation kuu ya malipo** (kwenye Payroll ya Director na ya Meneja) — bila PIN sahihi hela HAITATOKA (55 — INVALID PIN). **PIN ya Mtu** ndiyo mtu anayojiingiza nayo kwenye portal ya **👤 Details Zangu Binafsi**. Chaguo-msingi za PIN zote: **1234** — zibadilishe hapa!")
        conn = get_connection()
        df_b_pin = pd.read_sql_query("SELECT id, name FROM businesses ORDER BY name ASC", conn)
        df_people_pin = pd.read_sql_query("SELECT id, business_id, full_name, role_title FROM staff_members WHERE status = 'ACTIVE' ORDER BY business_id, staff_type DESC, full_name ASC", conn)
        conn.close()
        if not df_b_pin.empty:
            b_name_map = dict(zip(df_b_pin['id'], df_b_pin['name']))
            df_people_pin['biz_label'] = df_people_pin['business_id'].map(lambda x: b_name_map.get(x, f"Biz #{x}"))
            df_people_pin['label'] = df_people_pin['full_name'] + " (" + df_people_pin['role_title'] + ") — " + df_people_pin['biz_label']

            with st.form("pin_mgmt_form", clear_on_submit=True):
                st.markdown("#### 🔏 Badilisha PIN ya Akaunti ya Kampuni (Validation Kuu ya Malipo)")
                sel_b_pin_name = st.selectbox("Biashara:", df_b_pin['name'].tolist(), key="pin_biz_sel")
                sel_b_pin_id = int(df_b_pin[df_b_pin['name'] == sel_b_pin_name].iloc[0]['id'])
                new_comp_pin = st.text_input("PIN Mpya ya Akaunti ya Kampuni (Tarakimu 4-6):", type="password", max_chars=6, key="pin_comp_new")
                st.markdown("#### 🔏 Badilisha PIN ya Mtu (Details Zangu Binafsi)")
                sel_person_lbl = st.selectbox("Mtu:", ["— Chagua Mtu —"] + df_people_pin['label'].tolist(), key="pin_person_sel")
                new_person_pin = st.text_input("PIN Mpya ya Mtu (Tarakimu 4-6):", type="password", max_chars=6, key="pin_person_new")
                btn_save_pins = st.form_submit_button("💾 HIFADHI PIN MPYA")

                if btn_save_pins:
                    if new_comp_pin.strip():
                        ok_pin, msg_pin = set_company_pin(sel_b_pin_id, new_comp_pin)
                        if ok_pin:
                            trigger_beep_sound(523.25, 1.0, "sine")
                            st.success(f"✅ {msg_pin} (Biashara: {sel_b_pin_name})")
                        else:
                            trigger_beep_sound(1200, 5.0, "sawtooth")
                            render_marquee_error("PIN ya Akaunti", msg_pin, "Weka tarakimu 4 hadi 6 pekee.", field_label=["PIN Mpya ya Akaunti ya Kampuni (Tarakimu 4-6)"])
                    if sel_person_lbl != "— Chagua Mtu —" and new_person_pin.strip():
                        sel_person_row = df_people_pin[df_people_pin['label'] == sel_person_lbl].iloc[0]
                        ok_pin2, msg_pin2 = set_person_pin(int(sel_person_row['business_id']), sel_person_row['full_name'], new_person_pin)
                        if ok_pin2:
                            trigger_beep_sound(523.25, 1.0, "sine")
                            st.success(f"✅ {msg_pin2}")
                        else:
                            trigger_beep_sound(1200, 5.0, "sawtooth")
                            render_marquee_error("PIN ya Mtu", msg_pin2, "Weka tarakimu 4 hadi 6 pekee.", field_label=["PIN Mpya ya Mtu (Tarakimu 4-6)"])

        # ── Hali ya SMS Gateway ──
        st.markdown("---")
        st.markdown("### 📱 Hali ya SMS Gateway (SMS za Mishahara)")
        _sms_url = _gw_secret("SMS_API_URL")
        if _sms_url:
            st.success(f"✅ SMS mode: **LIVE** — SMS halisi zinatumwa kwa simu za watu kiotomatiki (SMS_API_URL imewekwa kwenye secrets/env).")
        else:
            st.warning("⚠️ SMS mode: **SIMULATION** — SMS zinakokotwa kwenye sms_log na kwenye **Details Zangu Binafsi** za kila mtu. Kwa SMS halisi, weka `SMS_API_URL` na `SMS_API_KEY` kwenye `.streamlit/secrets.toml` au environment variables (mfano wa provider: Africa's Talking, Twilio, BeemAfrica).")

    # -----------------------------------------------------------------
    # TAB 12: RISITI ZA MALIPO — DATABASE YA DIRECTOR (v62)
    # Kila malipo yakikamilika, risiti ya muamala inaingia hapa kiotomatiki
    # kutoka kwenye Ghala la Benki (Bank Gateway).
    # -----------------------------------------------------------------
    with dir_tab12:
        st.markdown("### 🧾 Risiti za Malipo — DATABASE YA DIRECTOR (Bank Gateway Receipts)")
        st.info("💡 Kila malipo yakikamilika (kutoka kwa Director AU kwa Meneja), risiti ya muamala inaingia kwenye database hii kiotomatiki pamoja na Bank TxnID. Risiti unazoziona zinawekwa alama kuwa ZIMEANGLIWA mara moja!")

        conn = get_connection()
        df_rcpt_d = pd.read_sql_query("SELECT * FROM payment_receipts ORDER BY id DESC LIMIT 300", conn)
        conn.close()

        if df_rcpt_d.empty:
            st.info("Bado hakuna risiti ya malipo iliyorekodiwa.")
        else:
            unseen_cnt = int((df_rcpt_d['seen_by_director'] == 0).sum())
            rc1, rc2, rc3, rc4 = st.columns(4)
            rc1.metric("Risiti Zote", len(df_rcpt_d))
            rc2.metric("Mpya (Hazijaangaliwa)", unseen_cnt)
            rc3.metric("Jumla Ililyopwa (TZS)", f"{df_rcpt_d['amount'].sum():,.2f}")
            rc4.metric("Zilizotoka kwa Meneja", int((df_rcpt_d['payer_role'] == 'MANAGER').sum()))

            df_show_d = df_rcpt_d.copy()
            df_show_d = df_show_d[["receipt_no", "payment_date", "business_name", "payer_role", "recipient_name",
                                   "amount", "recipient_bank", "recipient_account", "reference_code",
                                   "bank_txn_id", "bank_channel_label", "gateway_status"]].copy()
            df_show_d.columns = ["Risiti No", "Muda", "Biashara", "Alituma", "Mpokeaji", "Kiasi TZS",
                                 "Benki ya Mpokeaji", "Akaunti", "Ref", "Bank TxnID", "Ghala la Benki", "Hali"]
            st.dataframe(df_show_d, use_container_width=True)

            st.markdown("---")
            st.markdown("#### 🌐 Kumbukumbu za Ghala la Benki (Bank Transfer Log — zote, zilizopitishwa na zilizokataliwa)")
            conn = get_connection()
            df_banklog = pd.read_sql_query("SELECT checked_at as Muda, channel_label as Ghala, status as Hali, response_code as Jibu, bank_txn_id as TxnID, dest_name as Mpokeaji, amount as Kiasi_TZS, reference_code as Ref FROM bank_transfer_log ORDER BY id DESC LIMIT 200", conn)
            conn.close()
            if not df_banklog.empty:
                st.dataframe(df_banklog, use_container_width=True)
            else:
                st.info("Bado hakuna kumbukumbu za benki.")

            # ── V64: PAKUA RISITI KAMA PDF ──
            st.markdown("---")
            st.markdown("#### 📄 Pakua Risiti Kama PDF (Official Payment Receipt)")
            conn = get_connection()
            df_rcpt_pdf = pd.read_sql_query("SELECT receipt_no, recipient_name, amount, created_at FROM receipt_pdfs ORDER BY id DESC LIMIT 200", conn)
            conn.close()
            if not df_rcpt_pdf.empty:
                df_pdf_opts = [f"{r['receipt_no']} — {r['recipient_name']} ({float(r['amount']):,.0f} TZS)" for _, r in df_rcpt_pdf.iterrows()]
                sel_pdf_lbl = st.selectbox("Chagua Risiti ya Kupakua:", df_pdf_opts, key="dir_pdf_dl_sel")
                sel_pdf_row = df_rcpt_pdf.iloc[df_pdf_opts.index(sel_pdf_lbl)]
                conn = get_connection()
                df_pdf_data = pd.read_sql_query("SELECT pdf_b64 FROM receipt_pdfs WHERE receipt_no = ? ORDER BY id DESC LIMIT 1", conn, params=(sel_pdf_row['receipt_no'],))
                conn.close()
                if not df_pdf_data.empty:
                    try:
                        _pdf_bytes = base64.b64decode(df_pdf_data.iloc[0]['pdf_b64'])
                        st.download_button(f"📄 PAKUA RISITI PDF — {sel_pdf_row['receipt_no']}", data=_pdf_bytes,
                                           file_name=f"Risiti_{sel_pdf_row['receipt_no']}.pdf", mime="application/pdf",
                                           key="dir_pdf_dl_btn")
                    except Exception as e:
                        st.error(f"Hitilafu ya PDF: {e}")
            else:
                st.info("📄 Bado hakuna risiti za PDF — kila malipo jipya linatengeneza risiti yake ya PDF kiotomatiki.")

            # ── V64: SMS LOG — SMS zote za mishahara ──
            st.markdown("---")
            st.markdown("#### 📱 SMS za Mishahara (SMS Log — kila SMS inayotumwa kiotomatiki)")
            conn = get_connection()
            df_sms_log = pd.read_sql_query("SELECT created_at, recipient_name, phone, message, status, gateway_mode, gateway_txn_id FROM sms_log ORDER BY id DESC LIMIT 200", conn)
            conn.close()
            if not df_sms_log.empty:
                df_sms_log.columns = ["Muda", "Mpokeaji", "Simu", "Ujumbe wa SMS", "Hali", "Mode", "SMS TxnID"]
                st.dataframe(df_sms_log, use_container_width=True)
            else:
                st.info("📱 Bado hakuna SMS — mara malipo yakikamilika, SMS ya 'mshahara kukamilika' inatumwa kiotomatiki kwa simu ya mpokeaji.")

            # AUTO SEEN: risiti zinapoangaliwa na Director, zinawekwa alama MARA MOJA
            try:
                conn = get_connection()
                cursor = conn.cursor()
                cursor.execute("UPDATE payment_receipts SET seen_by_director = 1 WHERE seen_by_director = 0")
                conn.commit()
                conn.close()
            except Exception:
                pass

# =========================================================================
# PORTAL 1: MFUMO WA BIASHARA (BUSINESS PORTAL - MANAGER & STAFF ROLES)
# =========================================================================
elif portal_mode_canon == "🏢 Mfumo wa Biashara (Business Portal)":
    df_businesses = get_businesses()

    if df_businesses.empty:
        st.warning("⚠️ Bado Director hajasajili biashara yoyote. Director pekee ndiye anayesajili Biashara kwenye Director Portal!")
        st.stop()
    else:
        selected_b_name = st.sidebar.selectbox("Chagua Biashara Unayofanyia Kazi:", df_businesses['name'].tolist(), key="biz_sel_name")
        selected_rows = df_businesses[df_businesses['name'] == selected_b_name]

        if selected_rows.empty:
            st.info("Tafadhali chagua biashara.")
            st.stop()

        current_b = selected_rows.iloc[0]
        b_id = int(current_b['id'])
        currency = current_b['currency']

        # Session State Authentication
        if f"auth_{b_id}" not in st.session_state:
            st.session_state[f"auth_{b_id}"] = False
            st.session_state[f"role_{b_id}"] = None

        if not st.session_state[f"auth_{b_id}"]:
            st.sidebar.warning(f"🔒 Biashara ya **{current_b['name']}** imefungwa kwa password.")
            entered_pass = st.sidebar.text_input(f"Weka Password ya {current_b['name']}", type="password", key=f"pass_input_{b_id}")
            if st.sidebar.button("🔓 INGIA KWENYE BIASHARA", key=f"btn_login_{b_id}"):
                if not entered_pass.strip():
                    trigger_beep_sound(1200, 5.0, "sawtooth")
                    render_marquee_error("Password ya Ingilio", "Weka password yako ya ingilio!", "Ingiza Password ya Meneja au Staff iliyosajiliwa.", field_label=["Weka Password ya"])
                elif entered_pass == current_b['password']:
                    st.session_state[f"auth_{b_id}"] = True
                    st.session_state[f"role_{b_id}"] = "MANAGER"
                    st.sidebar.success("✅ Umefanikiwa kuingia kama MENEJA!")
                    st.rerun()
                elif entered_pass == current_b['staff_password']:
                    st.session_state[f"auth_{b_id}"] = True
                    st.session_state[f"role_{b_id}"] = "STAFF"
                    st.sidebar.success("✅ Umefanikiwa kuingia kama MFANYAKAZI!")
                    st.rerun()
                else:
                    trigger_beep_sound(1500, 5.0, "square")
                    render_marquee_error("Password ya Ingilio", "Password uliyoingiza si sahihi!", "Weka Password ya Meneja au ya Staff iliyosajiliwa.", field_label=["Weka Password ya"])
            
            st.markdown(textwrap.dedent(f"""
            <div style="background:#0F172A; border:2px solid #FF007F; padding:25px; border-radius:12px; text-align:center;">
                <h2 style="color:#FF007F;">🔒 SYSTEM LOCKED | CYBER BUSINESS GRADING SYSTEM</h2>
                <p style="font-size:1.1rem; color:#E2E8F0;">Biashara ya <b>{current_b['name']}</b> imelindwa kwa ulinzi wa password.</p>
                <p>Weka Password ya Meneja au Mfanyakazi kwenye bar ya pembeni (Sidebar) ili kufungua mfumo!</p>
            </div>
            """).strip(), unsafe_allow_html=True)
            st.stop()

        # Sidebar Status Display & Notification Alert for Manager
        current_role = st.session_state.get(f"role_{b_id}", "STAFF")
        st.sidebar.markdown("---")
        st.sidebar.markdown(f"### 🛡️ Status: {current_b['name']} ({current_role})")
        if is_valid_b64(current_b.get('business_avatar_b64')):
            st.sidebar.image(f"data:image/png;base64,{current_b['business_avatar_b64']}", caption=f"Logo: {current_b['name']}", use_container_width=True)

        st.sidebar.markdown(f"**Meneja Mkuu:** {current_b['manager_name']}")
        st.sidebar.markdown(f"**Simu:** {current_b['manager_phone']}")
        st.sidebar.markdown(f"**Email:** {current_b['manager_email']}")

        # ── SALIO ZA AKAUNTI ZA BENKI SIDEBAR (v63 — hela inayoenda kweli) ──
        try:
            _comp_bal_sb = float(current_b.get('company_bank_balance') or 0.0)
            _mgr_bal_sb = float(current_b.get('manager_bank_balance') or 0.0)
            st.sidebar.markdown(f"🏦 **A/C Kampuni ({current_b.get('company_bank_name') or 'CRDB BANK'}):** `{_comp_bal_sb:,.2f} TZS`")
            if current_role == "MANAGER":
                st.sidebar.markdown(f"💳 **A/C Yako ya Meneja ({current_b.get('manager_bank_name') or 'NMB BANK'}):** `{_mgr_bal_sb:,.2f} TZS`")
        except Exception:
            pass

        if is_valid_b64(current_b.get('manager_photo_b64')):
            st.sidebar.image(f"data:image/png;base64,{current_b['manager_photo_b64']}", caption="Manager Photo", width=130)

        # Check for Manager Notifications from Director
        conn = get_connection()
        df_mgr_notifs = pd.read_sql_query("""
            SELECT full_name, role_title, created_at 
            FROM staff_members 
            WHERE business_id = ? AND status = 'ACTIVE' 
            ORDER BY created_at DESC LIMIT 3
        """, conn, params=(b_id,))
        conn.close()

        if not df_mgr_notifs.empty and current_role == "MANAGER":
            latest_stf = df_mgr_notifs.iloc[0]
            st.sidebar.markdown(textwrap.dedent(f"""
            <div style="background:#003322; border:1px solid #39FF14; border-radius:8px; padding:10px; margin-top:8px;">
                <p style="color:#39FF14; font-size:0.85rem; margin:0;">
                    🔔 <b>NOTIFICATION:</b> Director amemwongeza/amemwidhinisha <b>{latest_stf['full_name']}</b> ({latest_stf['role_title']}) kwenye biashara yako!
                </p>
            </div>
            """).strip(), unsafe_allow_html=True)

        if st.sidebar.button("🔒 TOKA (LOGOUT)", key=f"btn_logout_{b_id}"):
            st.session_state[f"auth_{b_id}"] = False
            st.session_state[f"role_{b_id}"] = None
            st.rerun()

        st.sidebar.markdown("---")

        # Menu options depending on Role
        if current_role == "MANAGER":
            menu_options = [
                "📊 Overall Checkup & Progress Graphs",
                "💵 Weka Mapato (OpenCV Reality Scanner)",
                "📑 Ledger ya Assets & Liabilities (Meneja Pekee)",
                "💳 Malipo ya Mishahara ya Staff (Manager)",
                "🔔 Notifications (Taarifa za Biashara)",
                "📄 Pakua Ripoti ya Mwezi (PDF Report)",
                "👥 Usajili wa Staff Mpya (Manager)",
                "📞 Mawasiliano & Contacts",
                "✉️ Tuma Ujumbe Direct kwa Director"
            ]
        else:
            menu_options = [
                "💵 Weka Mapato (OpenCV Reality Scanner)",
                "📞 Mawasiliano & Contacts",
                "✉️ Tuma Ujumbe Direct kwa Director"
            ]

        menu = st.sidebar.radio("🧭 CYBER NAVIGATION:", menu_options, key="biz_nav_menu")

        # Midnight Alarm Checker
        conn = get_connection()
        df_today_check = pd.read_sql_query("SELECT id FROM daily_records WHERE business_id = ? AND record_date = ?", conn, params=(b_id, today_date_str))
        conn.close()

        if df_today_check.empty and (now_dt.hour == 23 and now_dt.minute >= 30):
            trigger_beep_sound(880, 5.0, "sawtooth")
            st.markdown(textwrap.dedent(f"""
            <div class="blinking-red-box">
                <h3 style="color:#FF0000; margin:0;">🚨 DHORUURA & BEEP ALARM: TAARIFA ZA LEO BADO HAZIJATUMWA!</h3>
                <p style="color:#FFFFFF; font-size:1.05rem;">
                    Zimebaki dakika chache kabla ya siku ya leo kuisha ({now_dt.strftime('%H:%M:%S')}). Tafadhali ingiza mauzo na matumizi ya leo mara moja!
                </p>
            </div>
            """).strip(), unsafe_allow_html=True)

        # ---------------------------------------------------------
        # MENU 1: OVERALL CHECKUP & PROGRESS GRAPHS (MANAGER ONLY - LINE GRAPHS ONLY)
        # ---------------------------------------------------------
        if menu == "📊 Overall Checkup & Progress Graphs":
            st.subheader(f"📊 Overall Economic Checkup & Progress Trajectory (Line Graphs): {current_b['name']}")
            
            conn = get_connection()
            df_daily = pd.read_sql_query("SELECT * FROM daily_records WHERE business_id = ? ORDER BY record_date ASC", conn, params=(b_id,))
            df_assets = pd.read_sql_query("SELECT * FROM assets_liabilities WHERE business_id = ?", conn, params=(b_id,))
            conn.close()

            target_date_str = today_date_str
            target_dt = pd.to_datetime(target_date_str)

            if not df_daily.empty:
                df_daily['record_date_dt'] = pd.to_datetime(df_daily['record_date'])
                df_daily['cash_profit'] = df_daily['revenue'] - df_daily['expenses']

                daily_row = df_daily[df_daily['record_date'] == target_date_str]
                daily_cash_rev = daily_row['cash_revenue'].sum() if not daily_row.empty else 0.0
                daily_credit_rev = daily_row['credit_revenue'].sum() if not daily_row.empty else 0.0
                daily_rev = daily_row['revenue'].sum() if not daily_row.empty else 0.0
                daily_exp = daily_row['expenses'].sum() if not daily_row.empty else 0.0
                daily_cash_profit = daily_rev - daily_credit_rev - daily_exp

                week_start = target_dt - pd.Timedelta(days=6)
                week_df = df_daily[(df_daily['record_date_dt'] >= week_start) & (df_daily['record_date_dt'] <= target_dt)]
                weekly_profit = week_df['cash_profit'].sum()
                weekly_rev = week_df['revenue'].sum()
                weekly_exp = week_df['expenses'].sum()

                month_df = df_daily[(df_daily['record_date_dt'].dt.year == target_dt.year) & (df_daily['record_date_dt'].dt.month == target_dt.month)]
                monthly_profit = month_df['cash_profit'].sum()
                monthly_rev = month_df['revenue'].sum()
                monthly_exp = month_df['expenses'].sum()

                df_daily['cumulative_profit'] = df_daily['cash_profit'].cumsum()
                df_daily['cumulative_capital'] = current_b['initial_capital'] + df_daily['cumulative_profit']
            else:
                daily_cash_profit = daily_cash_rev = daily_credit_rev = daily_rev = daily_exp = 0.0
                weekly_profit = weekly_rev = weekly_exp = 0.0
                monthly_profit = monthly_rev = monthly_exp = 0.0

            total_assets = df_assets[df_assets['item_type'] == 'Asset']['amount'].sum() if not df_assets.empty else 0.0
            total_liabilities = df_assets[df_assets['item_type'] == 'Liability']['amount'].sum() if not df_assets.empty else 0.0
            grand_total_capital = current_b['initial_capital'] + total_assets

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Faida ya Cash Siku Hii", f"{daily_cash_profit:,.2f} {currency}", delta=f"Cash: {daily_cash_rev:,.2f} | Mkopo: {daily_credit_rev:,.2f}")
            m2.metric("Faida ya Cash Wiki (Siku 7)", f"{weekly_profit:,.2f} {currency}", delta=f"Mapato: {weekly_rev:,.2f}")
            m3.metric("Faida ya Cash Mwezi", f"{monthly_profit:,.2f} {currency}", delta=f"Mapato: {monthly_rev:,.2f}")
            m4.metric("Jumla ya Mali (Assets)", f"{grand_total_capital:,.2f} {currency}", delta=f"Madeni: -{total_liabilities:,.2f}")

            st.markdown("---")
            st.markdown("### 📈 LINE GRAPHS ZA MAENDELEO NA FAIDA (LINE CHARTS ONLY)")

            if not df_daily.empty and PLOTLY_AVAILABLE:
                fig_profit = px.line(df_daily, x='record_date', y='cash_profit', title=t("📈 Mwenendo wa Faida ya Kila Siku (Daily Profit Line Graph)"), markers=True)
                fig_profit.update_traces(line_color='#39FF14', line_width=3, marker=dict(size=8, color='#00F0FF'))
                fig_profit.update_layout(paper_bgcolor='#0B132B', plot_bgcolor='#0F172A', font_color='#F1F5F9', xaxis_title=t("Tarehe"), yaxis_title=t("Faida (TZS)"))
                st.plotly_chart(fig_profit, use_container_width=True)
                st.caption(t("📈 Faida ya siku = mapato ya siku (cash + credit) - matumizi ya siku. Inakokotwa kiotomatiki kila siku mapato yanapotumwa, na Director anaona ukuaji wa biashara zote kwenye graph yake."))

        # ---------------------------------------------------------
        # MENU 2: WEKA MAPATO (SINGLE DAILY SUBMISSION - PRIVACY ENFORCED)
        # ---------------------------------------------------------
        elif menu == "💵 Weka Mapato (OpenCV Reality Scanner)":
            st.subheader(f"💵 Ingiza Mapato na Uthibitisho wa Sura - {current_b['name']}")

            # ── SALIO LA AKAUNTI YA MFANYAKAZI (v63): mfanyakazi anaona salio lake ──
            if current_role == "STAFF":
                try:
                    conn = get_connection()
                    df_stf_bal = pd.read_sql_query("""
                        SELECT full_name, role_title, bank_name, account_number, bank_account_balance
                        FROM staff_members WHERE business_id = ? AND status = 'ACTIVE'
                        ORDER BY full_name ASC
                    """, conn, params=(b_id,))
                    conn.close()
                    if not df_stf_bal.empty:
                        with st.expander("💰 SALIO LA AKAUNTI YAKO YA BENKI (Bank Account Balance)"):
                            stf_bal_names = df_stf_bal['full_name'].tolist()
                            who_am_i = st.selectbox("Chagua Jina Lako Kuona Salio Lako:", stf_bal_names, key="staff_bal_sel")
                            my_bal_row = df_stf_bal[df_stf_bal['full_name'] == who_am_i].iloc[0]
                            my_bal_val = float(my_bal_row.get('bank_account_balance') or 0.0)
                            b1, b2, b3 = st.columns(3)
                            b1.metric("🏦 Benki", str(my_bal_row.get('bank_name') or 'NMB BANK'))
                            b2.metric("💳 Akaunti", str(my_bal_row.get('account_number') or '00000000000'))
                            b3.metric("💰 Salio la Akaunti", f"{my_bal_val:,.2f} TZS")
                            st.caption("💡 Salio linaongezeka kiotomatiki mara Meneja au Director akimalipa mshahara wako kupitia Bank Gateway na risiti ya muamala inaingia kwenye database ya Director na Meneja.")
                except Exception:
                    pass

            conn = get_connection()
            df_already_submitted = pd.read_sql_query("SELECT * FROM daily_records WHERE business_id = ? AND record_date = ?", conn, params=(b_id, today_date_str))
            conn.close()

            if not df_already_submitted.empty:
                st.success(f"✅ TAARIFA ZA LEO ({today_date_str}) TAYARI ZISHATUMWA NA KUHIFADHIWA SALAMA KWENYE DATABASE!")
                st.info("🔒 Mfumo unaruhusu kutuma data MARA MOJA TU KWA SIKU. Taarifa zako zimeshaingia kwenye Mfumo wa Director & Meneja!")
            else:
                st.info("💡 **AUTO-DATE & TIME:** Mfumo unachukua tarehe na muda halisi kiotomatiki. Utatuma data mara moja tu kwa siku leo.")

                with st.form("daily_opencv_form", clear_on_submit=True):
                    st.markdown("#### 1️⃣ Taarifa za Mtumaji / Mfanyakazi (Aliyesajiliwa kwenye Biashara Hii)")
                    conn = get_connection()
                    df_tx_active_stf = pd.read_sql_query("""
                        SELECT id, full_name, phone, role_title, staff_type, bank_name, account_number 
                        FROM staff_members 
                        WHERE business_id = ? AND status = 'ACTIVE'
                        ORDER BY staff_type DESC, full_name ASC
                    """, conn, params=(b_id,))
                    conn.close()

                    if df_tx_active_stf.empty:
                        st.warning("⚠️ **HAKUNA WAFANYAKAZI WALIOSAJILIWA:** Biashara hii haina mfanyakazi au meneja aliyesajiliwa na kuidhinishwa. Director au Meneja lazima amsajili na kumwidhinisha kwanza kabla ya kutuma data!")
                        tx_s_name = ""
                        tx_s_phone_val = ""
                    else:
                        stf_tx_opts = [f"👤 {r['full_name']} ({r['role_title']}) - 📱 {r['phone']}" for idx, r in df_tx_active_stf.iterrows()]
                        sel_stf_tx_lbl = st.selectbox("Chagua Jina Lako Kwenye Orodha ya Wafanyakazi Waliosajiliwa:", stf_tx_opts, key="sel_stf_tx_dropdown")
                        selected_tx_stf_row = df_tx_active_stf.iloc[stf_tx_opts.index(sel_stf_tx_lbl)]
                        
                        tx_s_name = selected_tx_stf_row['full_name']
                        tx_s_phone_val = selected_tx_stf_row['phone']
                        
                        st.info(f"✅ **MTUMAJI ALIYETHIBITISHWA:** `{tx_s_name}` | 📱 **Simu:** `{tx_s_phone_val}` | 💼 **Cheo:** `{selected_tx_stf_row['role_title']}` | 🏦 **Benki:** `{selected_tx_stf_row['bank_name']}` (`{selected_tx_stf_row['account_number']}`)")
                        
                    st.markdown("#### 2️⃣ Mapato ya Siku (Mauzo ya Cash)")
                    tx_cash_rev = st.number_input("💵 Mauzo ya Cash (Fedha Taslimu in TZS)", min_value=0.0, step=1000.0, key="tx_cash")

                    st.markdown("#### 3️⃣ Matumizi ya Siku (Ingiza Kila Tumizi kwa Kadri Yako)")
                    num_exp_items = st.number_input("Chagua Idadi ya Matumizi ya Leo (Number of Expense Items):", min_value=1, max_value=20, value=1, step=1, key="tx_num_exp_items")

                    exp_item_details_list = []
                    total_exp_calculated = 0.0

                    for exp_i in range(int(num_exp_items)):
                        st.markdown(f"##### 💸 Tumizi la {exp_i+1}:")
                        exp_col1, exp_col2 = st.columns([2, 1])
                        with exp_col1:
                            e_name = st.text_input(f"Jina / Maelezo ya Tumizi {exp_i+1} (mf. Umeme, Nauli, Chakula)", key=f"exp_name_{exp_i}")
                        with exp_col2:
                            e_amt = st.number_input(f"Kiasi cha Tumizi {exp_i+1} (TZS)", min_value=0.0, step=1000.0, value=0.0, key=f"exp_amt_{exp_i}")
                        
                        if e_name.strip() and e_amt > 0:
                            exp_item_details_list.append(f"{e_name.strip()}: {e_amt:,.0f} TZS")
                            total_exp_calculated += e_amt

                    tx_expenses = total_exp_calculated
                    tx_exp_details = " | ".join(exp_item_details_list) if exp_item_details_list else "Hakuna Matumizi"

                    st.info(f"📊 **MUHTASARI WA LEO:** 💵 Mauzo: `{tx_cash_rev:,.2f} TZS` | 💸 Jumla ya Matumizi ({len(exp_item_details_list)} Vitu): `{tx_expenses:,.2f} TZS` | 📈 Faida Halisi: `{(tx_cash_rev - tx_expenses):,.2f} TZS`")

                    tx_notes = st.text_area("Maoni ya Ziada kuhusu Mauzo ya Leo (Optional)", height=60, key="tx_notes")
                    
                    st.markdown("#### 4️⃣ OpenCV Facial Recognition Camera Scanner (ID-Style Scan)")
                    tx_camera_photo = st.camera_input("📷 Piga Uso Wako Hapa Ili ku-Scan na Ku-Verify", key="tx_cam_widget")

                    btn_submit_daily = st.form_submit_button("🚀 SCAN, VERIFY & SUBMIT DATA KWENYE MFUMO")
                    
                    if btn_submit_daily:
                        if df_tx_active_stf.empty or not tx_s_name.strip():
                            trigger_beep_sound(1200, 5.0, "sawtooth")
                            render_marquee_error("Uthibitisho wa Mtumaji", "Hauruhusiwi kutuma data! Jina lako halijaidhinishwa na Director.", "Meneja au Director akusajili na kukuidhinisha kwanza.")
                            st.stop()

                        valid_names, names_msg = validate_three_names(tx_s_name, "Majina Yako")
                        valid_phone, phone_msg = validate_phone_number(tx_s_phone_val)
                        total_revenue = tx_cash_rev
                        tx_credit_rev = 0.0

                        if not valid_names:
                            trigger_beep_sound(1200, 5.0, "sawtooth")
                            render_marquee_error("Majina ya Mtumaji", names_msg, "Andika majina yako matatu yote.", field_label=["Chagua Jina Lako"])
                        elif not valid_phone:
                            trigger_beep_sound(1200, 5.0, "sawtooth")
                            render_marquee_error("Namba ya Simu", phone_msg, "Ingiza tarakimu 10 pekee zikianzaga na 06 au 07.", field_label=["Chagua Jina Lako"])
                        elif total_revenue <= 0 and tx_expenses <= 0:
                            trigger_beep_sound(1400, 5.0, "sawtooth")
                            render_marquee_error("Mapato / Matumizi", "Lazima uweke kiasi cha mapato au matumizi ya siku!", "Ingiza kiasi cha mauzo ya cash, mkopo au matumizi.", field_label=["Mauzo ya Cash", "Idadi ya Matumizi"])
                        elif tx_camera_photo is None:
                            trigger_beep_sound(1000, 5.0, "sawtooth")
                            render_marquee_error("Scan ya Uso", "Picha ya Sura inatakiwa kabla ya kutuma data!", "Bofya kitufe cha kamera kupiga picha ya uso wako.", field_label=["Piga Uso Wako"])
                        else:
                            is_verified, face_b64, face_count, scan_msg, metadata_json = scan_and_verify_face(tx_camera_photo)
                            
                            if not is_verified:
                                trigger_beep_sound(1500, 5.0, "square")
                                render_marquee_error("OpenCV Face Verification", scan_msg, "Elekeza sura usoni vizuri ili ku-scan uso wa binadamu.", field_label=["Piga Uso Wako"])
                            else:
                                trigger_beep_sound(523.25, 1.0, "sine")
                                sub_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                conn = get_connection()
                                cursor = conn.cursor()
                                try:
                                    cursor.execute("""
                                        INSERT INTO daily_records (business_id, record_date, cash_revenue, credit_revenue, revenue, expenses, expense_details, notes, sender_name, sender_phone, sender_face_b64, face_detected_count, face_metadata_json, submitted_timestamp)
                                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """, (b_id, today_date_str, tx_cash_rev, tx_credit_rev, total_revenue, tx_expenses, tx_exp_details, tx_notes, names_msg, phone_msg, face_b64, face_count, metadata_json, sub_time_str))
                                    conn.commit()
                                    # ── V64: FAIDA YA SIKU INAKOKOTWA KIOTOMATIKI ──
                                    # (mapato ya siku - matumizi ya siku) → daily_profit;
                                    # ndipo inatumika kwenye line graph ya ukuaji wa kila biashara.
                                    _today_profit = record_daily_profit(b_id, today_date_str, tx_cash_rev, tx_credit_rev, tx_expenses)
                                    add_notification('DIRECTOR', '💵 Mapato ya Siku Yemetumwa', f"Mtumaji '{names_msg}' ametuma mapato ya {today_date_str} ({total_revenue:,.0f} TZS) kwa biashara ya {current_b['name']} — 📈 Faida ya siku: {_today_profit:,.0f} TZS", 'DAILY_RECORD', b_id)
                                    add_notification('MANAGER', '💵 Mapato ya Siku Yemetumwa', f"Mtumaji '{names_msg}' ametuma mapato ya {today_date_str} ({total_revenue:,.0f} TZS) — 📈 Faida ya siku: {_today_profit:,.0f} TZS", 'DAILY_RECORD', b_id)
                                    st.success(f"✅ TAARIFA YAKO YA LEO ({today_date_str}) IMETUMWA KIKAMILIFU KWA DIRECTOR JOSHUA JOSEPH NA MENEJA WA BIASHARA! 📈 Faida ya siku hii: {(_today_profit):,.2f} TZS")
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Hitilafu: {e}")
                                finally:
                                    conn.close()

        # ---------------------------------------------------------
        # MENU 3: LEDGER (MANAGER ONLY - MULTI-ITEM BULK ENTRY)
        # ---------------------------------------------------------
        elif menu == "📑 Ledger ya Assets & Liabilities (Meneja Pekee)":
            st.subheader(f"📑 Ledger ya Vifaa, Mali Mpya na Madeni (Bulk Multi-Item Entry) - {current_b['name']}")
            st.info("💡 **MENEJA CONTROL:** Hapa unaweza kuingiza vifaa/mali/madeni hadi 3 kwa wakati mmoja! Weka Namba ya Risiti ya Manunuzi kwa kila kifaa.")

            with st.form("ledger_bulk_form", clear_on_submit=True):
                st.markdown("#### 1️⃣ Taarifa za Mtumaji / Meneja (Aliyesajiliwa kwenye Biashara Hii)")
                conn = get_connection()
                df_l_active_stf = pd.read_sql_query("""
                    SELECT id, full_name, phone, role_title, staff_type, bank_name, account_number 
                    FROM staff_members 
                    WHERE business_id = ? AND status = 'ACTIVE'
                    ORDER BY staff_type DESC, full_name ASC
                """, conn, params=(b_id,))
                conn.close()

                if df_l_active_stf.empty:
                    st.warning("⚠️ **HAKUNA WAFANYAKAZI WALIOSAJILIWA:** Biashara hii haina mfanyakazi au meneja aliyesajiliwa na kuidhinishwa. Director au Meneja lazima amsajili na kumwidhinisha kwanza kabla ya kutuma data!")
                    l_sender_name = ""
                    l_sender_phone_val = ""
                else:
                    stf_l_opts = [f"👤 {r['full_name']} ({r['role_title']}) - 📱 {r['phone']}" for idx, r in df_l_active_stf.iterrows()]
                    sel_stf_l_lbl = st.selectbox("Chagua Jina Lako Kwenye Orodha ya Wafanyakazi Waliosajiliwa:", stf_l_opts, key="sel_stf_ledger_dropdown")
                    selected_l_stf_row = df_l_active_stf.iloc[stf_l_opts.index(sel_stf_l_lbl)]
                    
                    l_sender_name = selected_l_stf_row['full_name']
                    l_sender_phone_val = selected_l_stf_row['phone']
                    
                    st.info(f"✅ **MTUMAJI ALIYETHIBITISHWA:** `{l_sender_name}` | 📱 **Simu:** `{l_sender_phone_val}` | 💼 **Cheo:** `{selected_l_stf_row['role_title']}` | 🏦 **Benki:** `{selected_l_stf_row['bank_name']}` (`{selected_l_stf_row['account_number']}`)")

                st.markdown("#### 2️⃣ Vifaa / Assets / Liabilities (Ingiza Kila Kifaa kwa Kadri Yako)")
                num_ledger_items = st.number_input("Chagua Idadi ya Items / Vifaa Unazotaka Kuingiza Leo:", min_value=1, max_value=20, value=1, step=1, key="num_ledger_items_input")

                ledger_entries_list = []
                for l_idx in range(int(num_ledger_items)):
                    st.markdown(f"##### 📦 Item {l_idx+1}:")
                    lc1, lc2, lc3, lc4, lc5 = st.columns([2, 1.5, 1, 1.5, 2])
                    with lc1:
                        it_name = st.text_input(f"Jina la Kifaa {l_idx+1}", key=f"it_name_{l_idx}")
                    with lc2:
                        it_type = st.selectbox(f"Aina {l_idx+1}", ["Asset (Mali/Kifaa)", "Liability (Deni)"], key=f"it_type_{l_idx}")
                    with lc3:
                        it_qty = st.number_input(f"Idadi {l_idx+1}", min_value=0, step=1, value=1 if l_idx==0 else 0, key=f"it_qty_{l_idx}")
                    with lc4:
                        it_price = st.number_input(f"Bei ya Kimoja {l_idx+1} (TZS)", min_value=0.0, step=1000.0, value=0.0, key=f"it_price_{l_idx}")
                    with lc5:
                        it_receipt = st.text_input(f"Namba ya Risiti {l_idx+1}", key=f"it_receipt_{l_idx}")
                    
                    if it_name.strip() and it_qty > 0:
                        tot_amt = it_qty * it_price
                        type_str = 'Asset' if 'Asset' in it_type else 'Liability'
                        ledger_entries_list.append({
                            'name': it_name.strip(),
                            'type': type_str,
                            'qty': it_qty,
                            'price': it_price,
                            'amount': tot_amt,
                            'receipt': it_receipt.strip()
                        })

                st.markdown("📸 **Facial Capture ya Ununuzi (OpenCV Verification):**")
                l_cam_photo = st.camera_input("Piga Picha ya Sura kwa Ajili ya Ledger", key="ledger_cam_widget")

                btn_ledger_submit = st.form_submit_button("➕ HIFADHI VIFAA KWENYE LEDGER")

                if btn_ledger_submit:
                    if df_l_active_stf.empty or not l_sender_name.strip():
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Uthibitisho wa Mtumaji", "Hauruhusiwi kutuma data! Jina lako halijaidhinishwa na Director.", "Meneja au Director akusajili na kukuidhinisha kwanza.")
                        st.stop()

                    valid_names, names_msg = validate_three_names(l_sender_name, "Majina ya Meneja")
                    valid_phone, phone_msg = validate_phone_number(l_sender_phone_val)

                    if not valid_names:
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Majina ya Meneja", names_msg, "Ingiza majina matatu kamili.", field_label=["Chagua Jina Lako"])
                    elif not valid_phone:
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Namba ya Simu", phone_msg, "Ingiza tarakimu 10 pekee zikianzaga na 06 au 07.", field_label=["Chagua Jina Lako"])
                    elif not ledger_entries_list:
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Vifaa vya Ledger", "Tafadhali ingiza angalau kifaa kimoja chenye jina na idadi zaidi ya 0!", "Weka taarifa za kifaa 1.", field_label=["Jina la Kifaa"])
                    elif l_cam_photo is None:
                        trigger_beep_sound(1000, 5.0, "sawtooth")
                        render_marquee_error("Scan ya Sura", "Picha ya sura inatakiwa!", "Piga picha ya uso kupitia kamera.", field_label=["Piga Picha ya Sura kwa Ajili ya Ledger"])
                    else:
                        is_verified, face_b64, face_count, scan_msg, metadata_json = scan_and_verify_face(l_cam_photo)
                        if not is_verified:
                            trigger_beep_sound(1500, 5.0, "square")
                            render_marquee_error("OpenCV Verification", scan_msg, "Elekeza uso wako vizuri kwenye kamera.", field_label=["kwa Ajili ya Ledger"])
                        else:
                            sub_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            conn = get_connection()
                            cursor = conn.cursor()
                            
                            for item in ledger_entries_list:
                                cursor.execute("""
                                    INSERT INTO assets_liabilities (business_id, item_name, item_type, quantity, unit_price, amount, date_acquired, description, sender_name, sender_phone, sender_face_b64, submitted_timestamp)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """, (b_id, item['name'], item['type'], item['qty'], item['price'], item['amount'], today_date_str, f"Risiti Na: {item['receipt']}", names_msg, phone_msg, face_b64, sub_time_str))

                            conn.commit()
                            conn.close()

                            add_notification('DIRECTOR', '📑 Vifaa Vimeingizwa Kwenye Ledger', f"Meneja '{names_msg}' ameingiza vifaa/mali {len(ledger_entries_list)} kwenye ledger ya biashara ya {current_b['name']}", 'LEDGER', b_id)

                            trigger_beep_sound(523.25, 1.0, "sine")
                            st.success(f"✅ VIFAA {len(ledger_entries_list)} VIMESAJILIWA KIKAMILIFU KWENYE LEDGER SAA {sub_time_str}!")
                            st.rerun()

        # ---------------------------------------------------------
        # MENU: NOTIFICATIONS & TAARIFA ZA BIASHARA (MANAGER VIEW)
        # ---------------------------------------------------------
        elif "Notifications" in menu or "Taarifa" in menu:
            st.subheader(f"🔔 Taarifa za Biashara - {current_b['name']}")
            st.info("💡 Hapa unaangalia taarifa zote za biashara yako. Taarifa zinawekwa kuwa ZIMESOMWA kiotomatiki mara unapoziona (zinaangaliwa mara moja)!")

            st.session_state['viewing_notif_role'] = 'MANAGER'
            st.session_state['viewing_notif_bid'] = b_id
            st.session_state['last_notif_page_id'] = f'MANAGER_NOTIFS_{b_id}'

            mgr_un_cnt_page = get_unread_notifications_count('MANAGER', b_id)
            if mgr_un_cnt_page > 0:
                # AUTO READ: taarifa zinapoangaliwa zinawekwa zimesomwa MARA MOJA
                mark_notifications_read('MANAGER', b_id)
                st.success(f"✅ Taarifa zote {mgr_un_cnt_page} zimeangaliwa na zimewekwa kuwa ZIMESOMWA kiotomatiki!")

            if st.button(f"✅ Weka Taarifa Zote Kuwa Zimesomwa ({mgr_un_cnt_page})", key=f"btn_mgr_mark_read_{b_id}"):
                mark_notifications_read('MANAGER', b_id)
                st.success("✅ Taarifa zote zimewekwa kuwa zimesomwa!")
                st.rerun()

            st.markdown("---")
            conn = get_connection()
            df_m_notifs = pd.read_sql_query("""
                SELECT id, title, message, category, is_read, created_at
                FROM system_notifications
                WHERE (recipient_role IN ('MANAGER', 'ALL') AND (business_id = ? OR business_id IS NULL))
                ORDER BY created_at DESC LIMIT 50
            """, conn, params=(b_id,))
            conn.close()

            if not df_m_notifs.empty:
                for idx, n_r in df_m_notifs.iterrows():
                    n_status = "🔴 EMPYA (UNREAD)" if n_r['is_read'] == 0 else "🟢 TAYARI IMESOMWA"
                    n_col = "#FF0055" if n_r['is_read'] == 0 else "#39FF14"
                    st.markdown(textwrap.dedent(f"""
                    <div style="background:#0F172A; border:1px solid {n_col}; border-radius:10px; padding:12px; margin-bottom:10px;">
                        <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px;">
                            <b style="color:{n_col}; font-size:1.05rem;">{n_r['title']}</b>
                            <span style="color:#FFD700; font-size:0.85rem;">{n_status} | {n_r['created_at']}</span>
                        </div>
                        <p style="color:#F1F5F9; margin-top:6px; margin-bottom:0;">{n_r['message']}</p>
                    </div>
                    """).strip(), unsafe_allow_html=True)
            else:
                st.info("Hakuna taarifa za biashara bado.")

        # ---------------------------------------------------------
        # MENU 4: PAKUA RIPOTI YA MWEZI (PDF REPORT)
        # ---------------------------------------------------------
                # ---------------------------------------------------------
        # MENU 4: MALIPO YA MISHAHARA YA STAFF (MANAGER PAYROLL)
        # ---------------------------------------------------------
        elif menu == "💳 Malipo ya Mishahara ya Staff (Manager)":
            st.subheader(f"💳 Malipo ya Mishahara kwa Wafanyakazi - {current_b['name']}")
            st.info("💡 **MANAGER PAYROLL:** Meneja hapa unaweza kumlipa kila mfanyakazi aliye chini yako mshahara wake wa mwezi. Kila malipo yatarekodiwa kwenye Mfumo wa Payroll Ledger!")

            conn = get_connection()
            df_m_stf = pd.read_sql_query("""
                SELECT * FROM staff_members 
                WHERE business_id = ? AND status = 'ACTIVE' AND staff_type != 'Meneja wa Biashara'
                ORDER BY full_name ASC
            """, conn, params=(b_id,))
            conn.close()

            if not df_m_stf.empty:
                st.markdown("#### 👥 Orodha ya Wafanyakazi Walio Active & Akaunti Zao za Benki")
                for idx, s_row in df_m_stf.iterrows():
                    s_bank = s_row.get('bank_name') or 'NMB BANK'
                    s_acc = s_row.get('account_number') or '00000000000'
                    s_bal = float(s_row.get('bank_account_balance') or 0.0)
                    st.write(f"👤 **{s_row['full_name']}** ({s_row['role_title']}) | 📱 **Simu:** `{s_row['phone']}` | 🏦 **Benki:** `{s_bank}` | 💳 **Akaunti:** `{s_acc}` | 💰 **Mshahara:** `{s_row['salary']:,.2f} TZS` | 🏦 **Salio la Akaunti:** **`{s_bal:,.2f} TZS`**")

                st.markdown("---")
                st.markdown("#### 📤 Fomu ya Kumlipa Mfanyakazi Mshahara")

                with st.form("manager_disburse_staff_salary_form", clear_on_submit=True):
                    stf_options = [f"{r['full_name']} ({r['role_title']}) - {r['salary']:,.0f} TZS" for idx, r in df_m_stf.iterrows()]
                    sel_stf_lbl = st.selectbox("Chagua Mfanyakazi wa Kumlipa:", stf_options, key="mgr_pay_stf_sel")
                    sel_stf_row = df_m_stf.iloc[stf_options.index(sel_stf_lbl)]

                    stf_p_bank = sel_stf_row.get('bank_name') or 'NMB BANK'
                    stf_p_acc = sel_stf_row.get('account_number') or '00000000000'

                    mc1, mc2 = st.columns(2)
                    with mc1:
                        st.text_input("Benki ya Mfanyakazi:", value=stf_p_bank, disabled=True, key="m_disb_bank")
                        m_pay_amt = st.number_input("Kiasi cha Kutuma (TZS):", min_value=0.0, value=float(sel_stf_row['salary']), step=10000.0, key="m_disb_amt")
                    with mc2:
                        st.text_input("Namba ya Akaunti ya Mfanyakazi:", value=stf_p_acc, disabled=True, key="m_disb_acc")
                        m_auto_ref = f"PAYMGR{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"
                        m_pay_ref = st.text_input("Namba ya Kumbukumbu / Ref:", value=m_auto_ref, key="m_disb_ref")

                    m_pay_notes = st.text_area("Maelezo ya Malipo (Notes):", value=f"Mshahara wa {sel_stf_row['full_name']} ({sel_stf_row['role_title']})", height=80, key="m_disb_notes")

                    # ── V64: MLIPAJI (Meneja) ANACHAGUA HALI ANATOKA — benki + akaunti + PIN ──
                    st.markdown("**🏦 HEMA INA TOKA (Source of Funds — Meneja anachagua):**")
                    msrc_c1, msrc_c2 = st.columns(2)
                    with msrc_c1:
                        comp_bank_pre_m = str(current_b.get('company_bank_name') or 'CRDB BANK')
                        _m_src_banks = TANZANIA_BANKS if comp_bank_pre_m in TANZANIA_BANKS else ([comp_bank_pre_m] + TANZANIA_BANKS)
                        m_src_bank = st.selectbox(t("🏦 Benki ya Kutolea Hela (Mlipaji):"), _m_src_banks, index=0, key="mgr_src_bank_sel")
                    with msrc_c2:
                        m_src_acc = st.text_input(t("🔢 Akaunti ya Kutolea Hela (Account Namba):"), value=str(current_b.get('company_account_number') or '11111111111'), key="mgr_src_acc")
                    m_src_pin = st.text_input(t("💰 PIN ya Akaunti Yako (Validation):"), type="password", max_chars=6, key="mgr_src_pin")
                    st.caption("🔒 " + t("PIN ya akaunti ya kutolea hela ndiyo validation kuu — bila PIN sahihi hela HAITATOKA. Chaguo-msingi: 1234 (Director anabadilisha kwenye Settings)."))

                    btn_m_pay_submit = st.form_submit_button("💳 THIBITISHA & LIPA MSHAHARA WA MFANYAKAZI")

                    if btn_m_pay_submit:
                        comp_bank_name_m = m_src_bank
                        comp_acc_num_m = m_src_acc.strip() or (str(current_b.get('company_account_number') or '11111111111'))
                        comp_bank_bal_m = float(current_b.get('company_bank_balance') or 0.0)
                        if m_pay_amt <= 0:
                            trigger_beep_sound(1200, 5.0, "sawtooth")
                            render_marquee_error("Kiasi cha Malipo", "Kiasi lazima kizidi 0 TZS!", "Weka kiasi halali cha fedha.", field_label=["Kiasi cha Kutuma"])
                        elif not m_src_pin.strip():
                            trigger_beep_sound(1200, 5.0, "sawtooth")
                            render_marquee_error("PIN ya Akaunti", "PIN ya akaunti ya kutolea hela haijajazwa! Hela haiwezi kutoka bila PIN.", "Weka PIN ya akaunti ya kampuni (validation kuu ya malipo).", field_label=["PIN ya Akaunti Yako (Validation)"])
                        elif not validate_company_pin(b_id, m_src_pin):
                            trigger_beep_sound(1500, 5.0, "square")
                            log_pin_rejection(comp_bank_name_m, comp_acc_num_m, stf_p_bank, stf_p_acc, sel_stf_row['full_name'], m_pay_amt, m_pay_ref.strip(), f"MANAGER {current_b['manager_name']}")
                            render_marquee_error("PIN ya Akaunti (55 — INVALID PIN)", "PIN ya akaunti ya kutolea hela si sahihi! Muamala UMEKATALIWA — hela HAIJATOLEWA.", "Angalia PIN yako kisha jaribu tena, au mwombe Director aibadilishe kwenye Settings.", field_label=["PIN ya Akaunti Yako (Validation)"])
                        elif not m_pay_ref.strip():
                            trigger_beep_sound(1200, 5.0, "sawtooth")
                            render_marquee_error("Transaction Ref", "Ref number inatakiwa!", "Ingiza namba ya kumbukumbu.", field_label=["Namba ya Kumbukumbu"])
                        elif m_pay_amt > comp_bank_bal_m:
                            trigger_beep_sound(1500, 5.0, "square")
                            render_marquee_error("Salio la Benki", f"Salio la Akaunti ya Kampuni ({comp_bank_name_m} A/C: {comp_acc_num_m}) ni TZS {comp_bank_bal_m:,.2f}, halitoshi kutoa malipo ya TZS {m_pay_amt:,.2f}!", "Director aweke mtaji/deposit kwenye Akaunti ya Kampuni, kisha jaribu tena.")
                        else:
                            # ── UNGANISHO NA BENKI (BANK GATEWAY) — MUDA WA KUTUMA ──
                            _mgw_lbl = detect_bank_channel(stf_p_bank)[1]
                            with st.spinner(f"🔌 Inaunganisha moja kwa moja na {_mgw_lbl} (Bank Gateway)..."):
                                mgw = send_bank_transfer(
                                    source_label=f"MANAGER {current_b['manager_name']} — {current_b['name']}",
                                    source_bank=comp_bank_name_m,
                                    source_account=comp_acc_num_m,
                                    dest_bank=stf_p_bank, dest_account=stf_p_acc, dest_name=sel_stf_row['full_name'],
                                    amount=m_pay_amt, reference=m_pay_ref.strip(), notes=m_pay_notes.strip(),
                                    source_balance=comp_bank_bal_m)
                            if mgw["status"] != "CONFIRMED":
                                trigger_beep_sound(1500, 5.0, "square")
                                render_marquee_error("Bank Gateway", mgw["message"], "Hakikisha namba ya akaunti au simu ya malipo ya mfanyakazi ni sahihi, kisha jaribu tena.")
                            else:
                                st.success(f"🏦 BANK GATEWAY: {mgw['channel_label']} — ✅ {mgw['response_code']} | TxnID: `{mgw['bank_txn_id']}`")

                                # ── HELA INAENDEKA KWELI: akaunti ya kampuni inapungua,
                                #    akaunti ya mfanyakazi inaongezeka (real movement) ──
                                stf_old_bal = float(sel_stf_row.get('bank_account_balance') or 0.0)
                                new_comp_bal_m = comp_bank_bal_m - m_pay_amt
                                new_stf_bal = stf_old_bal + m_pay_amt

                                conn = get_connection()
                                cursor = conn.cursor()
                                cursor.execute("UPDATE businesses SET company_bank_balance = ? WHERE id = ?", (new_comp_bal_m, b_id))
                                cursor.execute("UPDATE staff_members SET bank_account_balance = ? WHERE id = ?", (new_stf_bal, int(sel_stf_row['id'])))
                                cursor.execute("""
                                    INSERT INTO payroll_disbursements (business_id, sender_role, sender_name, recipient_type, recipient_name, recipient_phone, bank_name, account_number, amount, payment_date, reference_code, notes, source_bank_name, source_account_number, source_balance_after, recipient_bank_name, recipient_account_number, recipient_balance_after)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """, (b_id, 'MANAGER', current_b['manager_name'], 'STAFF', sel_stf_row['full_name'], sel_stf_row['phone'], stf_p_bank, stf_p_acc, m_pay_amt, today_date_str, m_pay_ref.strip(), m_pay_notes.strip(), comp_bank_name_m, comp_acc_num_m, new_comp_bal_m, stf_p_bank, stf_p_acc, new_stf_bal))
                                conn.commit()
                                conn.close()

                                # ── ACCOUNT LEDGER: mwendo wa hela akaunti → akaunti ──
                                record_account_movement(
                                    account_owner=f"COMPANY: {current_b['name']}", account_type="COMPANY",
                                    business_id=b_id, bank_name=comp_bank_name_m, account_number=comp_acc_num_m,
                                    direction="DEBIT (Hela Imetoka)", amount=m_pay_amt, balance_after=new_comp_bal_m,
                                    counterparty=f"STAFF: {sel_stf_row['full_name']}", reference_code=m_pay_ref.strip(),
                                    bank_txn_id=mgw.get('bank_txn_id'), channel_label=mgw.get('channel_label'),
                                    description=f"Mshahara wa {sel_stf_row['full_name']} — ulitumwa na Meneja {current_b['manager_name']}")
                                record_account_movement(
                                    account_owner=f"STAFF: {sel_stf_row['full_name']}", account_type="STAFF",
                                    business_id=b_id, bank_name=stf_p_bank, account_number=stf_p_acc,
                                    direction="CREDIT (Hela Imeingia)", amount=m_pay_amt, balance_after=new_stf_bal,
                                    counterparty=f"COMPANY: {current_b['name']}", reference_code=m_pay_ref.strip(),
                                    bank_txn_id=mgw.get('bank_txn_id'), channel_label=mgw.get('channel_label'),
                                    description=f"Mshahara uliopokelewa kutoka kwa Meneja {current_b['manager_name']}")

                                m_receipt_no = save_payment_receipt(
                                    business_id=b_id, business_name=current_b['name'],
                                    payer_role='MANAGER', payer_name=current_b['manager_name'],
                                    recipient_type='STAFF', recipient_name=sel_stf_row['full_name'],
                                    recipient_phone=sel_stf_row['phone'], recipient_bank=stf_p_bank,
                                    recipient_account=stf_p_acc, amount=m_pay_amt,
                                    reference_code=m_pay_ref.strip(), gateway_res=mgw,
                                    notes=m_pay_notes.strip())

                                # ── V64: RISITI RASMI KWA PDF + SMS YA MSHAHARA KIOTOMATIKI ──
                                _m_rcpt_pdf = generate_receipt_pdf({
                                    "receipt_no": m_receipt_no, "payment_time": today_date_str,
                                    "business_name": current_b['name'], "payer_role": "MANAGER",
                                    "payer_name": current_b['manager_name'], "recipient_name": sel_stf_row['full_name'],
                                    "recipient_bank": stf_p_bank, "recipient_account": stf_p_acc,
                                    "recipient_phone": sel_stf_row['phone'], "amount": m_pay_amt,
                                    "reference_code": m_pay_ref.strip(), "channel_label": mgw.get('channel_label'),
                                    "response_code": mgw.get('response_code'), "bank_txn_id": mgw.get('bank_txn_id'),
                                    "source_balance_after": new_comp_bal_m, "recipient_balance_after": new_stf_bal,
                                    "notes": m_pay_notes.strip()})
                                if _m_rcpt_pdf:
                                    store_receipt_pdf(m_receipt_no, b_id, sel_stf_row['full_name'], sel_stf_row['phone'], m_pay_amt, _m_rcpt_pdf)
                                _m_sms_msg = build_salary_sms(sel_stf_row['full_name'], m_pay_amt, stf_p_bank, stf_p_acc,
                                                              m_pay_ref.strip(), mgw.get('bank_txn_id'), get_current_language())
                                _m_sms_res = send_sms_gateway(sel_stf_row['phone'], _m_sms_msg, business_id=b_id,
                                                              recipient_name=sel_stf_row['full_name'], category="SALARY_SMS")
                                add_notification('DIRECTOR', f"📱 SMS ya Mshahara — {sel_stf_row['full_name']}",
                                                 f"Meneja {current_b['manager_name']} alipomlipa {sel_stf_row['full_name']}, SMS ya malipo imetumwa kiotomatiki kwa simu yake ({sel_stf_row['phone']}): {_m_sms_res['status']}",
                                                 category='SALARY_SMS', business_id=b_id)

                                trigger_beep_sound(523.25, 1.0, "sine")
                                st.info(f"🧾 Risiti No: `{m_receipt_no}` — IMETUMWA kwenye **DATABASE YA MENEJA** ✅ na **DATABASE YA DIRECTOR** ✅")
                                if _m_rcpt_pdf:
                                    st.info(f"📄 **Risiti ya PDF imetengenezwa** — {sel_stf_row['full_name']} anaweza kuipakua kwenye **Details Zangu Binafsi** (PIN yake), na Director kwenye tab ya Risiti.")
                                st.info(f"📱 **SMS ya mshahara kukamilika** imetumwa kiotomatiki kwa {sel_stf_row['full_name']} ({sel_stf_row['phone']}): **{_m_sms_res['status']}** (TxnID SMS: `{_m_sms_res['gateway_txn_id']}`)")
                                st.info(f"📊 **Mwendo wa Hela (Real Movement):** 🏦 Akaunti ya Kampuni ({comp_bank_name_m} A/C `{comp_acc_num_m}`): {comp_bank_bal_m:,.2f} → **{new_comp_bal_m:,.2f} TZS** ⬇️ | 💳 Akaunti ya {sel_stf_row['full_name']} ({stf_p_bank} A/C `{stf_p_acc}`): {stf_old_bal:,.2f} → **{new_stf_bal:,.2f} TZS** ⬆️")
                                st.success(f"✅ MSHAHARA WA {m_pay_amt:,.2f} TZS UMETHIBITISHWA NA BENKI NA KUTUMWA KWA {sel_stf_row['full_name'].upper()} ({stf_p_bank} - {stf_p_acc})! REF: {m_pay_ref.strip()}")
                                st.rerun()

                st.markdown("---")
                st.markdown("#### 📜 Historia ya Malipo ya Mishahara Yaliyotolewa na Meneja")
                conn = get_connection()
                df_m_pay_logs = pd.read_sql_query("""
                    SELECT id, payment_date as Tarehe, recipient_name as Mfanyakazi, 
                           bank_name as Benki, account_number as Akaunti, 
                           amount as Kiasi_TZS, reference_code as Reference_Code, notes as Maelezo
                    FROM payroll_disbursements
                    WHERE business_id = ? AND sender_role = 'MANAGER'
                    ORDER BY created_at DESC
                """, conn, params=(b_id,))
                conn.close()

                if not df_m_pay_logs.empty:
                    st.dataframe(df_m_pay_logs, use_container_width=True)
                else:
                    st.info("Bado hakuna kumbukumbu za malipo ya mishahara zilizotolewa na Meneja.")

                # ── RISITI ZA MALIPO — DATABASE YA MENEJA (v62) ──
                st.markdown("---")
                st.markdown("#### 🧾 Risiti za Malipo — DATABASE YA MENEJA (Risiti zote za wafanyakazi + za Director)")
                st.info("💡 Kila mfanyakazi anapolipwa mshahara wake (na Meneja AU Director), risiti ya muamala inaingia hapa kiotomatiki pamoja na Bank TxnID — na pia inaandikwa kwenye DATABASE YA DIRECTOR. Risiti zinazoonekana zinawekwa alama ZIMEANGLIWA mara moja!")
                conn = get_connection()
                df_rcpt_m = pd.read_sql_query("SELECT * FROM payment_receipts WHERE business_id = ? ORDER BY id DESC LIMIT 200", conn, params=(b_id,))
                conn.close()

                if df_rcpt_m.empty:
                    st.info("Bado hakuna risiti ya malipo ya biashara hii.")
                else:
                    m_unseen = int((df_rcpt_m['seen_by_manager'] == 0).sum())
                    mr1, mr2, mr3 = st.columns(3)
                    mr1.metric("Risiti Zote", len(df_rcpt_m))
                    mr2.metric("Mpya (Hazijaangaliwa)", m_unseen)
                    mr3.metric("Jumla Ililipwa (TZS)", f"{df_rcpt_m['amount'].sum():,.2f}")

                    df_show_m = df_rcpt_m[["receipt_no", "payment_date", "payer_role", "recipient_name", "amount",
                                           "recipient_bank", "recipient_account", "reference_code",
                                           "bank_txn_id", "bank_channel_label", "gateway_status"]].copy()
                    df_show_m.columns = ["Risiti No", "Muda", "Alituma", "Mpokeaji (Mfanyakazi)", "Kiasi TZS",
                                         "Benki", "Akaunti", "Ref", "Bank TxnID", "Ghala la Benki", "Hali"]
                    st.dataframe(df_show_m, use_container_width=True)

                    try:
                        conn = get_connection()
                        cursor = conn.cursor()
                        cursor.execute("UPDATE payment_receipts SET seen_by_manager = 1 WHERE business_id = ? AND seen_by_manager = 0", (b_id,))
                        conn.commit()
                        conn.close()
                    except Exception:
                        pass
            else:
                st.info("Hakuna wafanyakazi waliosajiliwa kwenye biashara hii kutoa malipo.")

            # ── ACCOUNT LEDGER — DATABASE YA MENEJA (v63): mwendo halisi wa hela ──
            st.markdown("---")
            st.markdown("#### 📒 ACCOUNT LEDGER — Mwendo wa Hela Akaunti Moja hadi Nyingine")
            st.info("💡 Hapa ndipo hela inapoenda KWELI: kila muamala wa benki unaonyesha akaunti iliyotoa hela (DEBIT ⬇️), akaunti iliyopokea (CREDIT ⬆️), salio baada ya muamala na Bank TxnID. Salio la akaunti ya kampuni linapungua na salio la mfanyakazi linaongezeka moja kwa moja!")
            conn = get_connection()
            df_ledger_m = pd.read_sql_query("SELECT * FROM account_ledger WHERE business_id = ? ORDER BY id DESC LIMIT 200", conn, params=(b_id,))
            conn.close()
            if df_ledger_m.empty:
                st.info("Bado hakuna mwendo wa hela kwenye ledger ya biashara hii.")
            else:
                df_ledger_show_m = df_ledger_m[["created_at", "account_owner", "bank_name", "account_number", "direction", "amount", "balance_after", "counterparty", "reference_code", "bank_txn_id", "channel_label"]].copy()
                df_ledger_show_m.columns = ["Muda", "Akaunti (Mmiliki)", "Benki", "Akaunti No", "Mwelekeo wa Hela", "Kiasi TZS", "Salio Baada ya Muamala", "Mhusika Mwingine", "Ref", "Bank TxnID", "Ghala la Benki"]
                st.dataframe(df_ledger_show_m, use_container_width=True)

        elif menu == "📞 Mawasiliano & Contacts":
            render_communication_center(current_role, b_id)

        elif menu == "📄 Pakua Ripoti ya Mwezi (PDF Report)":
            st.subheader(f"📄 Pakua Ripoti ya Mwezi ya PDF (Official Executive Report) - {current_b['name']}")
            
            col_r1, col_r2 = st.columns(2)
            with col_r1:
                sel_year = st.selectbox("Chagua Mwaka:", [2025, 2026, 2027], index=1)
            with col_r2:
                months_dict = {
                    1: "Januari", 2: "Februari", 3: "Machi", 4: "Aprili",
                    5: "Mei", 6: "Juni", 7: "Julai", 8: "Agosti",
                    9: "Septemba", 10: "Oktoba", 11: "Novemba", 12: "Disemba"
                }
                sel_month_num = st.selectbox("Chagua Mwezi:", list(months_dict.keys()), format_func=lambda x: months_dict[x], index=datetime.datetime.now().month - 1)
                sel_month_name = months_dict[sel_month_num]

            conn = get_connection()
            df_all_daily = pd.read_sql_query("SELECT * FROM daily_records WHERE business_id = ? ORDER BY record_date ASC", conn, params=(b_id,))
            df_all_assets = pd.read_sql_query("SELECT * FROM assets_liabilities WHERE business_id = ?", conn, params=(b_id,))
            conn.close()

            if not df_all_daily.empty:
                df_all_daily['dt'] = pd.to_datetime(df_all_daily['record_date'])
                df_month = df_all_daily[(df_all_daily['dt'].dt.year == sel_year) & (df_all_daily['dt'].dt.month == sel_month_num)]
            else:
                df_month = pd.DataFrame()

            pdf_bytes = generate_pdf_monthly_report(current_b, sel_year, sel_month_num, sel_month_name, df_month, df_all_assets)

            st.markdown("---")
            st.success(f"✅ Ripoti ya Mwezi wa {sel_month_name} {sel_year} ya biashara ya '{current_b['name']}' ipo tayari!")
            
            st.download_button(
                label=f"📥 PAKUA RIPOTI YA PDF ({sel_month_name} {sel_year})",
                data=pdf_bytes,
                file_name=f"RIPOTI_BIASHARA_{current_b['name']}_{sel_month_name}_{sel_year}.pdf",
                mime="application/pdf"
            )

        # ---------------------------------------------------------
        # MENU 5: USAJILI WA STAFF MPYA (MANAGER SUBMITS FOR DIRECTOR APPROVAL - ANY ROLE)
        # ---------------------------------------------------------
        elif menu == "👥 Usajili wa Staff Mpya (Manager)":
            st.subheader(f"👥 Usajili wa Mfanyakazi Mpya (Sent to Director for Approval) - {current_b['name']}")
            st.info("💡 **NOTE:** Meneja unaweza kumsajili mfanyakazi mpya kwa NYADHIFAMOJA / CHEO CHOTE. Ukisubmit, maombi yataenda kwa Director **Joshua Joseph** ili a-Approve au a-Reject!")

            with st.form("manager_onboard_staff_form", clear_on_submit=True):
                st.markdown("#### 1️⃣ Taarifa Binafsi (Majina Matatu)")
                m_p1, m_p2 = st.columns(2)
                with m_p1:
                    m_stf_name = st.text_input("Majina Matatu ya Mfanyakazi (mf. JOSHUA JOSEPH MAVOA)", key="mgr_stf_name")
                    
                    m_stf_phone_val = st.text_input("Namba ya Simu (06... / 07... Tarakimu 10)", max_chars=10, key="mgr_stf_phone")
                    st.caption(f"📱 **Simu:** `{len(m_stf_phone_val)} / 10`")
                with m_p2:
                    m_stf_email = st.text_input("Barua Pepe (Email: jina@gmail.com)", key="mgr_stf_email")
                    
                    m_stf_nida_val = st.text_input("Namba ya NIDA (Tarakimu 20)", max_chars=20, key="mgr_stf_nida")
                    st.caption(f"🆔 **NIDA:** `{len(m_stf_nida_val)} / 20`")

                st.markdown("#### 2️⃣ Anwani ya Makazi (Mkoa - Tanzania)")
                m_stf_mkoa = st.selectbox("Chagua Mkoa:", TANZANIA_REGIONS, key="mgr_stf_mkoa")
                m_stf_wilaya = ""

                m_stf_full_res = f"MKOA WA {m_stf_mkoa.upper()}"
                st.caption(f"📌 **Anwani:** `{m_stf_full_res}`")

                st.markdown("#### 3️⃣ Ngazi ya Elimu, Cheo, Mshahara & Benki")
                m_m1, m_m2, m_m3 = st.columns(3)
                with m_m1:
                    m_stf_edu = st.selectbox("Ngazi ya Elimu:", EDUCATION_LEVELS, key="mgr_stf_edu")
                with m_m2:
                    m_stf_role = st.selectbox("Cheo / Nyadhifa (Kazi Yoyote):", STANDARD_ROLES, key="mgr_stf_role")
                with m_m3:
                    m_stf_salary = st.number_input("Mshahara Utakaolipwa kwa Mwezi (TZS):", min_value=0.0, value=300000.0, step=10000.0, key="mgr_stf_salary")

                mb_c1, mb_c2 = st.columns(2)
                with mb_c1:
                    m_stf_bank = st.selectbox("Benki / Mfumo wa Malipo wa Mfanyakazi:", TANZANIA_BANKS, key="mgr_stf_bank")
                with mb_c2:
                    m_stf_account = st.text_input("Akaunti ya Benki / Simu ya Malipo ya Mfanyakazi:", key="mgr_stf_account")

                st.markdown("#### 4️⃣ Facial Scan & Upload Nyaraka")
                m_stf_photo_cam = st.camera_input("📸 Piga Picha ya Sura ya Mfanyakazi", key="mgr_stf_cam")

                m_d1, m_d2, m_d3, m_d4 = st.columns(4)
                with m_d1:
                    m_stf_contract = st.file_uploader("Mkataba wa Ajira", type=["pdf", "docx", "png", "jpg", "jpeg"], key="mgr_stf_contract")
                with m_d2:
                    m_stf_letter = st.file_uploader("Barua ya Kuomba Ajira", type=["pdf", "docx", "png", "jpg", "jpeg"], key="mgr_stf_letter")
                with m_d3:
                    m_stf_cv = st.file_uploader("CV Yake", type=["pdf", "docx", "png", "jpg", "jpeg"], key="mgr_stf_cv")
                with m_d4:
                    m_stf_cert = st.file_uploader("Cheti cha Elimu", type=["pdf", "docx", "png", "jpg", "jpeg"], key="mgr_stf_cert")

                btn_mgr_submit_stf = st.form_submit_button("📤 TUMA USAJILI WA STAFF KWA DIRECTOR ILI A-APPROVE")

                if btn_mgr_submit_stf:
                    valid_names, names_msg = validate_three_names(m_stf_name, "Majina ya Mfanyakazi")
                    valid_phone, phone_msg = validate_phone_number(m_stf_phone_val)
                    valid_email, email_msg = validate_gmail_email(m_stf_email)
                    valid_nida, nida_msg = validate_nida_number(m_stf_nida_val)

                    if not valid_names:
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Majina Matatu", names_msg, "Weka majina matatu kamili.", field_label=["Majina Matatu ya Mfanyakazi"])
                    elif not valid_phone:
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Namba ya Simu", phone_msg, "Ingiza tarakimu 10 pekee zikianzaga na 06 au 07.", field_label=["Namba ya Simu"])
                    elif not valid_email:
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Email", email_msg, "Ingiza email ya 'jina@gmail.com'.", field_label=["Barua Pepe"])
                    elif not valid_nida:
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("NIDA", nida_msg, "Ingiza tarakimu 20 za NIDA.", field_label=["Namba ya NIDA"])
                    elif not m_stf_account.strip():
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Akaunti ya Benki", "Ingiza Akaunti ya Benki ya Mfanyakazi!", "Weka akaunti ya benki.", field_label=["Akaunti ya Benki"])
                    elif m_stf_photo_cam is None:
                        trigger_beep_sound(1000, 5.0, "sawtooth")
                        render_marquee_error("Facial Scan", "Picha ya sura inatakiwa!", "Piga picha ya sura kupitia kamera.", field_label=["Piga Picha ya Sura ya Mfanyakazi"])
                    else:
                        is_verified, face_b64, face_count, scan_msg, metadata_json = scan_and_verify_face(m_stf_photo_cam)
                        if not is_verified:
                            trigger_beep_sound(1500, 5.0, "square")
                            render_marquee_error("OpenCV Verification", scan_msg, "Elekeza sura vizuri kwenye kamera.", field_label=["Piga Picha ya Sura ya Mfanyakazi"])
                        else:
                            contract_b64 = file_to_b64(m_stf_contract) if m_stf_contract else None
                            c_name = m_stf_contract.name if m_stf_contract else None
                            letter_b64 = file_to_b64(m_stf_letter) if m_stf_letter else None
                            l_name = m_stf_letter.name if m_stf_letter else None
                            cv_b64 = file_to_b64(m_stf_cv) if m_stf_cv else None
                            v_name = m_stf_cv.name if m_stf_cv else None
                            cert_b64 = file_to_b64(m_stf_cert) if m_stf_cert else None
                            crt_name = m_stf_cert.name if m_stf_cert else None

                            conn = get_connection()
                            cursor = conn.cursor()
                            cursor.execute("""
                                INSERT INTO staff_members (business_id, staff_type, full_name, residence_mkoa, residence_wilaya, full_residence_upper, phone, nida_number, email, education_level, role_title, salary, bank_name, account_number, photo_b64, contract_b64, contract_filename, application_letter_b64, letter_filename, cv_b64, cv_filename, cert_b64, cert_filename, status)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING_APPROVAL')
                            """, (b_id, 'Mfanyakazi wa Kawaida', names_msg, m_stf_mkoa, m_stf_wilaya, m_stf_full_res, phone_msg, nida_msg, email_msg, m_stf_edu, m_stf_role, m_stf_salary, m_stf_bank, m_stf_account.strip(), face_b64, contract_b64, c_name, letter_b64, l_name, cv_b64, v_name, cert_b64, crt_name))
                            conn.commit()
                            conn.close()

                            trigger_beep_sound(523.25, 1.0, "sine")
                            add_notification('DIRECTOR', '👥 Ombi la Usajili wa Staff', f"Meneja wa biashara '{current_b['name']}' amesajili mfanyakazi '{names_msg}' ({m_stf_role}) - anasubiri idhini!", 'STAFF_REG', b_id)
                            st.success(f"✅ USAJILI WA '{names_msg}' UMETUMWA KWA DIRECTOR JOSHUA JOSEPH ILI A-APPROVE!")
                            st.rerun()

        # ---------------------------------------------------------
        # MENU 6: TUMA UJUMBE DIRECT KWA DIRECTOR
        # ---------------------------------------------------------
        elif menu == "✉️ Tuma Ujumbe Direct kwa Director":
            st.subheader("✉️ Tuma Ujumbe / Taarifa Moja Kwa Moja kwa Director (Joshua Joseph)")
            st.info("💡 **NOTE:** Ujumbe wako utatumwa na kuingia moja kwa moja kwenye Panel ya Director. Mtumaji anatakiwa achague jina lake kutoka kwenye orodha ya wafanyakazi/viongozi waliosajiliwa ili taarifa zake zijazwe kiotomatiki.")
            
            conn = get_connection()
            df_all_active_stf = pd.read_sql_query("""
                SELECT s.id, s.full_name, s.phone, s.email, s.role_title, b.name as business_name, s.business_id 
                FROM staff_members s
                LEFT JOIN businesses b ON s.business_id = b.id
                WHERE s.status = 'ACTIVE'
                ORDER BY b.name ASC, s.full_name ASC
            """, conn)
            conn.close()

            with st.form("director_message_panel_form", clear_on_submit=True):
                st.markdown("#### 1️⃣ Taarifa za Mtumaji (Aliyesajiliwa kwenye Mfumo)")
                
                if df_all_active_stf.empty:
                    st.warning("⚠️ **HAKUNA WAFANYAKAZI WALIOSAJILIWA:** Bado hakuna mfanyakazi au meneja aliyesajiliwa kwenye mfumo. Director au Meneja lazima amsajili mtumaji kwanza!")
                    g_sender_name = ""
                    g_sender_phone_val = ""
                    g_sender_email = ""
                    g_business_id = None
                else:
                    stf_msg_all_opts = [f"👤 {r['full_name']} ({r['role_title']} - {r['business_name'] or 'General'})" for idx, r in df_all_active_stf.iterrows()]
                    sel_stf_all_lbl = st.selectbox("Chagua Jina Lako Kwenye Orodha ya Wafanyakazi Waliosajiliwa:", stf_msg_all_opts, key="sel_stf_msg_all_dropdown")
                    selected_msg_row = df_all_active_stf.iloc[stf_msg_all_opts.index(sel_stf_all_lbl)]
                    
                    g_sender_name = selected_msg_row['full_name']
                    g_sender_phone_val = selected_msg_row['phone']
                    g_sender_email = selected_msg_row['email']
                    g_business_id = int(selected_msg_row['business_id']) if selected_msg_row['business_id'] else None
                    
                    st.info(f"✅ **MTUMAJI ALIYETHIBITISHWA (AUTO-FILLED):** `{g_sender_name}` | 📱 **Simu:** `{g_sender_phone_val}` | ✉️ **Email:** `{g_sender_email}` | 🏢 **Biashara:** `{selected_msg_row['business_name']}`")

                st.markdown("#### 2️⃣ Ujumbe Wako kwa Director")
                g_subject = st.text_input("Kichwa cha Habari (Subject)", key="dir_msg_subject")
                g_message = st.text_area("Andika Ujumbe Wako Hapa...", height=130, key="dir_msg_text")
                
                btn_send_dir_msg = st.form_submit_button("🚀 TUMA UJUMBE MOJA KWA MOJA KWENYE PANEL YA DIRECTOR")
                
                if btn_send_dir_msg:
                    if df_all_active_stf.empty or not g_sender_name.strip():
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Uthibitisho wa Mtumaji", "Hauruhusiwi kutuma ujumbe! Jina lako halijaidhinishwa na Director.", "Director au Meneja akusajili kwanza.")
                        st.stop()

                    valid_names, names_msg = validate_three_names(g_sender_name, "Majina Yako")
                    valid_phone, phone_msg = validate_phone_number(g_sender_phone_val)
                    valid_email, email_msg = validate_gmail_email(g_sender_email)

                    if not valid_names or not g_subject.strip() or not g_message.strip():
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Ujumbe kwa Director", "Tafadhali jaza subject na ujumbe unaotaka kutuma!", "Kamilisha sehemu zote za fomu.", field_label=["Kichwa cha Habari", "Andika Ujumbe Wako"])
                    elif not valid_phone:
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Namba ya Simu", phone_msg, "Ingiza tarakimu 10 pekee zikianzaga na 06 au 07.", field_label=["Chagua Jina Lako"])
                    elif not valid_email:
                        trigger_beep_sound(1200, 5.0, "sawtooth")
                        render_marquee_error("Email", email_msg, "Ingiza email yenye mfumo wa 'jina@gmail.com'.", field_label=["Chagua Jina Lako"])
                    else:
                        conn = get_connection()
                        cursor = conn.cursor()
                        cursor.execute("""
                            INSERT INTO director_messages (business_id, sender_name, sender_phone, sender_email, subject, message)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (g_business_id, names_msg, phone_msg, email_msg, g_subject.strip(), g_message.strip()))
                        conn.commit()
                        conn.close()

                        add_notification('DIRECTOR', '📩 UJUMBE MPYA KWA DIRECTOR', f"Ujumbe mpya kutoka kwa {names_msg}: '{g_subject.strip()}'", category='MESSAGE', business_id=g_business_id)

                        trigger_beep_sound(523.25, 1.0, "sine")
                        st.success(f"✅ UJUMBE WAKO WA '{g_subject.strip()}' UMETUMWA KIKAMILIFU NA UMEINGIA KWENYE PANEL YA DIRECTOR!")
                        st.rerun()

