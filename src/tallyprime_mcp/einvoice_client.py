"""
E-Invoice (IRN) generation against the NIC IRP sandbox/production.

The NIC IRP Public-API uses an asymmetric+symmetric handshake:

  1. Client generates a random 32-byte AppKey (the AES-256 key for this session).
  2. Auth request body is RSA-encrypted with NIC's public key (PKCS#1 v1.5)
     and POST'ed to /eivital/v1.04/auth.
  3. The auth response contains an `AuthToken` (plaintext) and a `Sek` (a
     fresh server-side AES-256 key encrypted with the AppKey using
     AES-256-ECB-PKCS5).  Decrypt Sek with AppKey → that's the SEK
     (Session Encryption Key) used for everything after auth.
  4. Every later request payload is AES-256-ECB-PKCS5 encrypted with SEK,
     wrapped as `{"Data": "<base64-ct>"}`.  Responses are encrypted the
     same way and decrypted with SEK.

Reference: https://einv-apisandbox.nic.in/

Environment variables (loaded by server_http via python-dotenv):
  IRP_BASE_URL          e.g. https://einv-apisandbox.nic.in
  IRP_CLIENT_ID         from sandbox dashboard
  IRP_CLIENT_SECRET     from sandbox dashboard
  IRP_USERNAME          GSTIN-bound API user (per-GSTIN credential)
  IRP_PASSWORD          password for that user
  IRP_GSTIN             the seller GSTIN this user is authorised for
  IRP_PUBLIC_KEY_PATH   path to NIC's public key PEM
                        (default: ./nic_irp/sandbox_public_key.pem)
  IRP_AUTH_PATH         (optional) override; default /eivital/v1.04/auth
  IRP_GENERATE_PATH     (optional) override; default /eicore/v1.04/Invoice
  IRP_CANCEL_PATH       (optional) override; default /eicore/v1.04/Invoice/Cancel
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

import httpx

from cryptography.hazmat.primitives import hashes, padding as sym_padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────

class EInvoiceError(Exception):
    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body   = body

    def to_dict(self) -> dict[str, Any]:
        return {"error": str(self), "status": self.status, "body": self.body}


class EInvoiceConfigError(EInvoiceError):
    """Missing env-var configuration."""


class EInvoiceAuthError(EInvoiceError):
    """Authentication failed (bad creds, expired token, etc.)."""


# ─────────────────────────────────────────────────────────────────────
# Crypto helpers — exact algorithms NIC mandates
# ─────────────────────────────────────────────────────────────────────

def _rsa_encrypt(plaintext: bytes, public_key, scheme: str = "pkcs1v15") -> str:
    """RSA encrypt → base64.

    scheme choices:
      "pkcs1v15"           RSA/ECB/PKCS1Padding (NIC docs say this — try first)
      "oaep-sha1"          OAEP, hash+MGF1 both SHA-1
      "oaep-sha256"        OAEP, hash+MGF1 both SHA-256
      "oaep-sha256-mgf1"   OAEP, hash=SHA-256 with MGF1=SHA-1 (BouncyCastle default,
                           common in Indian fintech)
      "oaep-sha1-mgf256"   OAEP, hash=SHA-1 with MGF1=SHA-256 (rare)
    """
    if scheme == "oaep-sha1":
        pad = asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA1()),
            algorithm=hashes.SHA1(),
            label=None,
        )
    elif scheme == "oaep-sha256":
        pad = asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        )
    elif scheme == "oaep-sha256-mgf1":
        # BouncyCastle's default when you ask for SHA-256 OAEP — MGF1 stays SHA-1.
        pad = asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA1()),
            algorithm=hashes.SHA256(),
            label=None,
        )
    elif scheme == "oaep-sha1-mgf256":
        pad = asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA1(),
            label=None,
        )
    else:
        pad = asym_padding.PKCS1v15()
    ct = public_key.encrypt(plaintext, pad)
    return base64.b64encode(ct).decode("ascii")


def _aes_encrypt(plaintext: bytes, key: bytes) -> str:
    """AES-256/ECB/PKCS5Padding → base64."""
    padder = sym_padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ct).decode("ascii")


def _aes_decrypt(ciphertext_b64: str, key: bytes) -> bytes:
    """Decrypt AES-256/ECB/PKCS5Padding base64 ciphertext → plaintext bytes."""
    ct = base64.b64decode(ciphertext_b64)
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ct) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(algorithms.AES.block_size).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


# ─────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────

class NicIrpClient:
    """NIC IRP e-Invoice API client (sandbox or production)."""

    # Default paths per NIC's current sandbox dashboard.
    # Note: eiVital is at v1.04 but eiCore stayed at v1.03 for IRN generate/cancel.
    DEFAULT_AUTH_PATH     = "/eivital/v1.04/auth"
    DEFAULT_GENERATE_PATH = "/eicore/v1.03/Invoice"
    DEFAULT_CANCEL_PATH   = "/eicore/v1.03/Invoice/Cancel"

    def __init__(self) -> None:
        self.base_url        = (os.environ.get("IRP_BASE_URL") or "https://einv-apisandbox.nic.in").rstrip("/")
        self.client_id       = os.environ.get("IRP_CLIENT_ID", "")
        self.client_secret   = os.environ.get("IRP_CLIENT_SECRET", "")
        self.username        = os.environ.get("IRP_USERNAME", "")
        self.password        = os.environ.get("IRP_PASSWORD", "")
        self.gstin           = os.environ.get("IRP_GSTIN", "")
        self.public_key_path = os.environ.get("IRP_PUBLIC_KEY_PATH") \
                                or str(Path(__file__).resolve().parent.parent.parent / "nic_irp" / "sandbox_public_key.pem")

        self.auth_path     = os.environ.get("IRP_AUTH_PATH",     self.DEFAULT_AUTH_PATH)
        self.generate_path = os.environ.get("IRP_GENERATE_PATH", self.DEFAULT_GENERATE_PATH)
        self.cancel_path   = os.environ.get("IRP_CANCEL_PATH",   self.DEFAULT_CANCEL_PATH)

        # RSA padding for the auth handshake. NIC docs say PKCS1Padding but
        # newer sandboxes have moved to OAEP-SHA1. Set IRP_RSA_PADDING to
        # one of: "pkcs1v15" (default), "oaep-sha1", "oaep-sha256".
        self.rsa_scheme = os.environ.get("IRP_RSA_PADDING", "pkcs1v15").lower()

        # Session state
        self._auth_token: str | None = None
        self._sek: bytes | None = None
        self._token_expiry: float = 0.0  # epoch seconds (from TokenExpiry minutes)
        self._public_key = None

    # ── Configuration ────────────────────────────────────────────
    def assert_configured(self) -> None:
        missing = []
        if not self.base_url:      missing.append("IRP_BASE_URL")
        if not self.client_id:     missing.append("IRP_CLIENT_ID")
        if not self.client_secret: missing.append("IRP_CLIENT_SECRET")
        if not self.username:      missing.append("IRP_USERNAME")
        if not self.password:      missing.append("IRP_PASSWORD")
        if not self.gstin:         missing.append("IRP_GSTIN")
        if missing:
            raise EInvoiceConfigError(
                f"Missing env vars: {', '.join(missing)}. "
                "Set them in .env and restart the MCP server."
            )
        if not Path(self.public_key_path).exists():
            raise EInvoiceConfigError(
                f"Public key file not found at: {self.public_key_path}. "
                "Download NIC's sandbox public key from the IRP developer "
                "portal and save it there (or set IRP_PUBLIC_KEY_PATH)."
            )

    def _load_public_key(self):
        if self._public_key is None:
            with open(self.public_key_path, "rb") as f:
                self._public_key = serialization.load_pem_public_key(f.read())
        return self._public_key

    # ── Authentication ───────────────────────────────────────────
    async def _authenticate(self) -> None:
        """Run the RSA+AES handshake and cache (auth_token, sek, expiry).

        NIC's auth requires PER-FIELD encryption (NOT a single RSA-encrypted
        JSON blob): the `Password` and `AppKey` fields are each encrypted
        individually with NIC's public key, base64'd, and dropped into the
        request body alongside the plaintext UserName + ForceRefreshAccessToken.
        """
        self.assert_configured()

        # Step 1: generate a fresh AppKey (32 random bytes = AES-256 key).
        # We keep raw bytes for our SEK decryption later; the JSON carries
        # the base64 form (NIC: "32-byte array used for encryption, base64
        # for display" — both directions, the wire JSON has the base64 string).
        app_key = secrets.token_bytes(32)
        app_key_b64 = base64.b64encode(app_key).decode("ascii")

        # Step 2: build plaintext auth JSON.
        plaintext_body = {
            "UserName":                self.username,
            "Password":                self.password,
            "AppKey":                  app_key_b64,
            "ForceRefreshAccessToken": False,
        }
        plaintext_json = json.dumps(plaintext_body, indent=2).replace("\n", "\r\n")

        # Step 3: per NIC docs — "Json containing the Credentials is encoded
        # using Base64 and then encrypted using e-Invoice public Key".
        # So we base64-encode the JSON STRING first, then RSA-encrypt that
        # base64 representation. (We've been skipping the base64 step, which
        # is why every padding scheme failed.)
        plaintext_bytes  = plaintext_json.encode("utf-8")
        plaintext_b64    = base64.b64encode(plaintext_bytes)
        public_key       = self._load_public_key()
        encrypted_blob   = _rsa_encrypt(
            plaintext_b64,                  # base64'd JSON, not raw JSON
            public_key,
            scheme=self.rsa_scheme,
        )

        # Step 4: wrap as {"Data": "<base64-ciphertext>"} — matches the
        # on-wire encrypted-payload form NIC's dashboard generates.
        request_body = {"Data": encrypted_blob}

        # Diagnostic — log payload size + first/last few chars so we can
        # compare to dashboard's encrypted payload byte-for-byte if needed.
        logger.info("NIC auth request: plaintext=%d bytes, ciphertext=%d chars (base64)",
                    len(plaintext_json), len(encrypted_blob))
        headers = {
            "client_id":     self.client_id,
            "client_secret": self.client_secret,
            "gstin":         self.gstin,
            "user_name":     self.username,
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        url = f"{self.base_url}{self.auth_path}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(url, json=request_body, headers=headers)
        except httpx.RequestError as e:
            raise EInvoiceAuthError(f"Auth request failed: {e}") from e

        try:
            response = r.json()
        except Exception:
            raise EInvoiceAuthError(
                f"Auth response was not JSON (HTTP {r.status_code})",
                status=r.status_code,
                body=r.text[:500],
            )

        if r.status_code >= 400 or response.get("Status") != 1:
            raise EInvoiceAuthError(
                "Authentication failed",
                status=r.status_code,
                body=response,
            )

        data = response.get("Data") or {}
        encrypted_sek = data.get("Sek") or data.get("SEK")
        auth_token    = data.get("AuthToken") or data.get("authToken")
        token_expiry  = data.get("TokenExpiry") or data.get("tokenExpiry") or 360  # minutes

        if not encrypted_sek or not auth_token:
            raise EInvoiceAuthError(
                "Auth response missing Sek or AuthToken",
                status=r.status_code,
                body=data,
            )

        # Step 3: decrypt SEK with our AppKey
        try:
            sek = _aes_decrypt(encrypted_sek, app_key)
        except Exception as e:
            raise EInvoiceAuthError(f"Failed to decrypt SEK: {e}", status=r.status_code) from e
        if len(sek) not in (16, 24, 32):
            raise EInvoiceAuthError(
                f"SEK has unexpected length {len(sek)} (expected 16/24/32)",
                status=r.status_code,
            )

        # Cache (with a small safety margin before token expiry)
        self._auth_token   = auth_token
        self._sek          = sek
        # TokenExpiry can be in minutes (NIC's spec) or as a yyyy-mm-dd HH:MM:SS string.
        # We expire 60s early to avoid edge-case clock skew.
        try:
            self._token_expiry = time.time() + max(60, int(token_expiry) * 60 - 60)
        except (TypeError, ValueError):
            self._token_expiry = time.time() + (360 * 60 - 60)  # default 6 hours
        logger.info("NIC IRP auth OK; token cached until %s",
                    time.strftime('%H:%M:%S', time.localtime(self._token_expiry)))

    async def _ensure_session(self) -> tuple[str, bytes]:
        """Authenticate if needed; return (auth_token, sek)."""
        if not self._auth_token or not self._sek or time.time() >= self._token_expiry:
            await self._authenticate()
        # mypy / runtime guarantee
        assert self._auth_token is not None and self._sek is not None
        return self._auth_token, self._sek

    # ── Common encrypted POST ────────────────────────────────────
    async def _encrypted_post(self, path: str, payload: dict) -> dict:
        """POST `payload` (JSON-serialisable) to `path`, encrypted with SEK.

        Returns the decrypted-then-parsed response Data, plus the raw envelope.
        """
        auth_token, sek = await self._ensure_session()
        url = f"{self.base_url}{path}"
        encrypted = _aes_encrypt(json.dumps(payload).encode("utf-8"), sek)

        # NIC's IRN endpoints expect AuthToken (PascalCase) — different from
        # the auth endpoint headers which use lowercase. Also pass via
        # multiple header-name variants since some intermediaries normalise.
        headers = {
            "client_id":     self.client_id,
            "client_secret": self.client_secret,
            "Gstin":         self.gstin,           # GSTIN can be PascalCase too
            "user_name":     self.username,
            "AuthToken":     auth_token,
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        body = {"Data": encrypted}

        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                r = await client.post(url, json=body, headers=headers)
        except httpx.RequestError as e:
            raise EInvoiceError(f"IRP request failed: {e}") from e

        try:
            envelope = r.json()
        except Exception:
            raise EInvoiceError(
                f"IRP response was not JSON (HTTP {r.status_code})",
                status=r.status_code,
                body=r.text[:500],
            )

        if r.status_code >= 400 or envelope.get("Status") != 1:
            # Errors typically come back un-encrypted in ErrorDetails / InfoDtls.
            raise EInvoiceError(
                f"IRP returned Status={envelope.get('Status')}",
                status=r.status_code,
                body=envelope,
            )

        # Successful responses can be either { Data: <encrypted-string> } OR
        # { Data: { ... } } depending on the endpoint.  Handle both.
        data_field = envelope.get("Data")
        if isinstance(data_field, str):
            try:
                decrypted = _aes_decrypt(data_field, sek).decode("utf-8")
                parsed = json.loads(decrypted)
            except Exception as e:
                raise EInvoiceError(f"Failed to decrypt/parse Data: {e}", body=envelope) from e
            return {"data": parsed, "raw": envelope}

        return {"data": data_field or {}, "raw": envelope}

    # ── Public API ───────────────────────────────────────────────
    async def generate_irn(self, payload: dict) -> dict:
        """Submit an NIC schema-1.1 invoice payload, return the decoded response.

        Returned dict has:
            {
              "data": { Irn, AckNo, AckDt, SignedInvoice, SignedQRCode, ... },
              "raw":  <full IRP envelope>,
            }
        """
        return await self._encrypted_post(self.generate_path, payload)

    async def cancel_irn(self, irn: str, reason_code: str, remarks: str) -> dict:
        """Cancel a previously-generated IRN."""
        body = {"Irn": irn, "CnlRsn": str(reason_code), "CnlRem": (remarks or "")[:100]}
        return await self._encrypted_post(self.cancel_path, body)


# ─────────────────────────────────────────────────────────────────────
# IRP schema-1.1 payload builder (provider-agnostic — same on every GSP)
# ─────────────────────────────────────────────────────────────────────

def build_irn_payload(form: dict[str, Any]) -> dict[str, Any]:
    """Compose an NIC IRP schema-1.1 IRN payload from a flat form dict.

    The PWA sends a `form` shaped like:

        {
          "doc_type":     "INV",                  # INV / CRN / DBN
          "doc_no":       "INV-001",
          "doc_date":     "21/04/2026",            # DD/MM/YYYY
          "supply_type":  "B2B",
          "seller": { gstin, legal_name, trade_name, addr1, addr2,
                      loc, pin, stcd, ph, em },
          "buyer":  { gstin, legal_name, trade_name, addr1, addr2,
                      loc, pin, stcd, pos, ph, em },
          "items": [ { desc, is_service, hsn, qty, unit, rate,
                       discount, gst_rate, cess_rate, other_charges } ]
        }

    Tax split: intrastate (CGST+SGST) when seller.stcd == buyer.pos,
               otherwise interstate (IGST).
    """
    # Helper: NIC's validator enforces "min length 3" on optional fields if
    # they're present. Send null instead of empty string for optional values.
    def _opt(v):
        s = (v or "").strip() if isinstance(v, str) else v
        return s if s else None

    seller = form.get("seller") or {}
    buyer  = form.get("buyer")  or {}
    items  = form.get("items")  or []

    intrastate = str(seller.get("stcd", "")).strip() == str(buyer.get("pos", "")).strip()

    item_list = []
    tot_ass = tot_cgst = tot_sgst = tot_igst = tot_cess = 0.0
    for i, it in enumerate(items, start=1):
        qty       = float(it.get("qty") or 0)
        rate      = float(it.get("rate") or 0)
        discount  = float(it.get("discount") or 0)
        gst_rate  = float(it.get("gst_rate") or 0)
        cess_rate = float(it.get("cess_rate") or 0)
        oth       = float(it.get("other_charges") or 0)

        tot_amt   = round(qty * rate, 2)
        ass_amt   = round(tot_amt - discount, 2)
        gst_amt   = round(ass_amt * gst_rate / 100.0, 2)
        cgst_amt  = round(gst_amt / 2, 2) if intrastate else 0.0
        sgst_amt  = round(gst_amt / 2, 2) if intrastate else 0.0
        igst_amt  = 0.0 if intrastate else gst_amt
        cess_amt  = round(ass_amt * cess_rate / 100.0, 2)
        item_val  = round(ass_amt + cgst_amt + sgst_amt + igst_amt + cess_amt + oth, 2)

        tot_ass  += ass_amt
        tot_cgst += cgst_amt
        tot_sgst += sgst_amt
        tot_igst += igst_amt
        tot_cess += cess_amt

        item_list.append({
            "SlNo":               str(i),
            "PrdDesc":            str(it.get("desc", ""))[:300],
            "IsServc":            "Y" if it.get("is_service") else "N",
            "HsnCd":              str(it.get("hsn", "")).strip(),
            "Qty":                qty,
            "FreeQty":            0,
            "Unit":               str(it.get("unit", "NOS")).upper(),
            "UnitPrice":          rate,
            "TotAmt":             tot_amt,
            "Discount":           discount,
            "PreTaxVal":          ass_amt,
            "AssAmt":             ass_amt,
            "GstRt":              gst_rate,
            "IgstAmt":            igst_amt,
            "CgstAmt":            cgst_amt,
            "SgstAmt":            sgst_amt,
            "CesRt":              cess_rate,
            "CesAmt":             cess_amt,
            "CesNonAdvlAmt":      0,
            "StateCesRt":         0,
            "StateCesAmt":        0,
            "StateCesNonAdvlAmt": 0,
            "OthChrg":            oth,
            "TotItemVal":         item_val,
        })

    tot_inv = round(tot_ass + tot_cgst + tot_sgst + tot_igst + tot_cess, 2)

    payload = {
        "Version": "1.1",
        "TranDtls": {
            "TaxSch":      "GST",
            "SupTyp":      str(form.get("supply_type", "B2B")).upper(),
            "RegRev":      "N",
            "EcmGstin":    None,
            "IgstOnIntra": "N",
        },
        "DocDtls": {
            "Typ": str(form.get("doc_type", "INV")).upper(),
            "No":  str(form.get("doc_no", ""))[:16],
            "Dt":  form.get("doc_date", ""),
        },
        "SellerDtls": {
            "Gstin":   seller.get("gstin", ""),
            "LglNm":   seller.get("legal_name", ""),
            "TrdNm":   _opt(seller.get("trade_name")),
            "Addr1":   seller.get("addr1", ""),
            "Addr2":   _opt(seller.get("addr2")),
            "Loc":     seller.get("loc", ""),
            "Pin":     int(seller.get("pin", 0) or 0),
            "Stcd":    str(seller.get("stcd", "")),
            "Ph":      _opt(seller.get("ph")),
            "Em":      _opt(seller.get("em")),
        },
        "BuyerDtls": {
            "Gstin":   buyer.get("gstin", ""),
            "LglNm":   buyer.get("legal_name", ""),
            "TrdNm":   _opt(buyer.get("trade_name")),
            "Pos":     str(buyer.get("pos", "")),
            "Addr1":   buyer.get("addr1", ""),
            "Addr2":   _opt(buyer.get("addr2")),
            "Loc":     buyer.get("loc", ""),
            "Pin":     int(buyer.get("pin", 0) or 0),
            "Stcd":    str(buyer.get("stcd", "")),
            "Ph":      _opt(buyer.get("ph")),
            "Em":      _opt(buyer.get("em")),
        },
        "ItemList": item_list,
        "ValDtls": {
            "AssVal":     round(tot_ass, 2),
            "CgstVal":    round(tot_cgst, 2),
            "SgstVal":    round(tot_sgst, 2),
            "IgstVal":    round(tot_igst, 2),
            "CesVal":     round(tot_cess, 2),
            "StCesVal":   0,
            "Discount":   0,
            "OthChrg":    0,
            "RndOffAmt":  0,
            "TotInvVal":  tot_inv,
        },
    }

    # ── E-Way Bill block (optional) ───────────────────────────────────────────
    # When the form supplies any transport detail we add EwbDtls so the IRP
    # generates the IRN and the EWB in a single call. NIC schema notes:
    #   TransId / TransName : optional. Min length 15 / 3 if present, so we
    #                         OMIT them rather than send "" (blank → null).
    #   Distance            : integer kilometres. 0 = "let IRP auto-calculate
    #                         from PIN codes" — always send (don't omit).
    #   VehType             : "R" Regular, "O" Over-Dimensional Cargo. Hard-coded "R".
    #   TransMode           : "1" Road, "2" Rail, "3" Air, "4" Ship. Hard-coded "1".
    ewb = form.get("ewb") or {}
    trans_doc_no = (ewb.get("trans_doc_no") or "").strip()
    trans_doc_dt = (ewb.get("trans_doc_dt") or "").strip()
    veh_no       = (ewb.get("veh_no") or "").strip()
    if trans_doc_no or trans_doc_dt or veh_no:
        try:
            distance = int(ewb.get("distance") or 0)
        except (TypeError, ValueError):
            distance = 0
        payload["EwbDtls"] = {
            "TransId":    _opt(ewb.get("trans_id")),
            "TransName":  _opt(ewb.get("trans_name")),
            "Distance":   distance,
            "TransDocNo": trans_doc_no,
            "TransDocDt": trans_doc_dt,
            "VehNo":      veh_no,
            "VehType":    "R",
            "TransMode":  "1",
        }

    return payload


# ─────────────────────────────────────────────────────────────────────
# Singleton helper used by server_http
# ─────────────────────────────────────────────────────────────────────
_default_client: NicIrpClient | None = None

def get_client() -> NicIrpClient:
    global _default_client
    if _default_client is None:
        _default_client = NicIrpClient()
    return _default_client
