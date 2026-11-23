"""
ETA (Egyptian Tax Authority) e-invoice signing helpers for the Local Signer.

* serialize_invoice(): ETA canonical serialization.
* sign_document():     signs the canonical string with the PKCS#11 token and
                       returns a base64 detached CAdES-BES (CMS SignedData).

Dependencies (see requirements.txt): PyKCS11, asn1crypto.
There is NO simulated fallback: if anything is missing the call fails loudly.
"""
import base64
import datetime
import hashlib
import json

from PyKCS11 import (
    PyKCS11Lib, Mechanism, CKF_SERIAL_SESSION, CKF_RW_SESSION,
    CKA_CLASS, CKA_ID, CKA_VALUE, CKO_PRIVATE_KEY, CKO_CERTIFICATE,
    CKM_SHA256_RSA_PKCS,
)
from asn1crypto import cms, core, x509, algos
from asn1crypto import tsp  # noqa: F401  registers signing_certificate_v2 in cms


# ---------------------------------------------------------------------------
# ETA canonical serialization
# ---------------------------------------------------------------------------

def _serialize_scalar(value):
    if value is None:
        text = ''
    elif isinstance(value, bool):
        text = 'true' if value else 'false'
    elif isinstance(value, (int, float)):
        # Same textual form the number has in the JSON document we submit.
        text = json.dumps(value)
    else:
        text = str(value)
    # Escape like a JSON string body (backslash and double quote).
    text = text.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{text}"'


def serialize_invoice(document):
    """
    ETA canonicalization:
      - simple value  ->  "value"
      - object        ->  for each property: "KEY" + serialize(value)   (KEY upper-cased)
      - array property->  "KEY" once, then for every element "KEY" + serialize(element)
    The 'signatures' property is never part of the signed content.
    """
    if not isinstance(document, (dict, list)):
        return _serialize_scalar(document)
    if isinstance(document, list):
        raise ValueError('Top-level document must be a JSON object')

    out = []
    for key, value in document.items():
        if key == 'signatures':
            continue
        upper = '"' + str(key).upper() + '"'
        if isinstance(value, list):
            out.append(upper)
            for element in value:
                out.append(upper)
                out.append(serialize_invoice(element) if isinstance(element, dict) else _serialize_scalar(element))
        elif isinstance(value, dict):
            out.append(upper)
            out.append(serialize_invoice(value))
        else:
            out.append(upper)
            out.append(_serialize_scalar(value))
    return ''.join(out)


# ---------------------------------------------------------------------------
# CAdES-BES
# ---------------------------------------------------------------------------

def build_signed_attributes(content_digest, cert):
    """signedAttrs: content-type, signing-time, message-digest, signing-certificate-v2."""
    cert_hash = hashlib.sha256(cert.dump()).digest()
    ess_cert_id = tsp.ESSCertIDv2({
        'hash_algorithm': algos.DigestAlgorithm({'algorithm': 'sha256'}),
        'cert_hash': cert_hash,
        'issuer_serial': {
            'issuer': [x509.GeneralName(name='directory_name', value=cert.issuer)],
            'serial_number': cert.serial_number,
        },
    })
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    return cms.CMSAttributes([
        cms.CMSAttribute({'type': 'content_type', 'values': ['data']}),
        cms.CMSAttribute({'type': 'signing_time', 'values': [cms.Time({'utc_time': core.UTCTime(now)})]}),
        cms.CMSAttribute({'type': 'message_digest', 'values': [content_digest]}),
        cms.CMSAttribute({'type': 'signing_certificate_v2',
                          'values': [tsp.SigningCertificateV2({'certs': [ess_cert_id]})]}),
    ])


def assemble_cades_bes(cert, signed_attrs, signature):
    """Detached CMS SignedData (no encapsulated content) as DER bytes."""
    signer_info = cms.SignerInfo({
        'version': 'v1',
        'sid': cms.SignerIdentifier({
            'issuer_and_serial_number': cms.IssuerAndSerialNumber({
                'issuer': cert.issuer,
                'serial_number': cert.serial_number,
            }),
        }),
        'digest_algorithm': algos.DigestAlgorithm({'algorithm': 'sha256'}),
        'signed_attrs': signed_attrs,
        'signature_algorithm': algos.SignedDigestAlgorithm({'algorithm': 'sha256_rsa'}),
        'signature': signature,
    })
    signed_data = cms.SignedData({
        'version': 'v1',
        'digest_algorithms': [algos.DigestAlgorithm({'algorithm': 'sha256'})],
        'encap_content_info': {'content_type': 'data'},  # detached
        'certificates': [cert],
        'signer_infos': [signer_info],
    })
    return cms.ContentInfo({'content_type': 'signed_data', 'content': signed_data}).dump()


def _find_key_and_cert(session):
    certs = session.findObjects([(CKA_CLASS, CKO_CERTIFICATE)])
    keys = session.findObjects([(CKA_CLASS, CKO_PRIVATE_KEY)])
    if not certs or not keys:
        raise RuntimeError("لم يتم العثور على شهادة توقيع ومفتاح خاص داخل التوكن.")
    # Pair certificate and private key by CKA_ID.
    for cert_obj in certs:
        cert_id = session.getAttributeValue(cert_obj, [CKA_ID])[0]
        for key_obj in keys:
            if session.getAttributeValue(key_obj, [CKA_ID])[0] == cert_id:
                return key_obj, cert_obj
    if len(certs) == 1 and len(keys) == 1:
        return keys[0], certs[0]
    raise RuntimeError("تعذر مطابقة الشهادة مع المفتاح الخاص داخل التوكن.")


def sign_document(serialized_str, dll_path, pin):
    """
    Sign the ETA canonical string with the hardware token and return the
    base64 CAdES-BES value for {"signatureType": "I", "value": ...}.
    """
    content_digest = hashlib.sha256(serialized_str.encode('utf-8')).digest()

    pkcs11 = PyKCS11Lib()
    pkcs11.load(dll_path)
    slots = pkcs11.getSlotList(tokenPresent=True)
    if not slots:
        raise RuntimeError("لم يتم العثور على توكن (ePass2003). تأكد من توصيل الفلاشة.")

    session = pkcs11.openSession(slots[0], CKF_SERIAL_SESSION | CKF_RW_SESSION)
    session.login(pin)
    try:
        key_obj, cert_obj = _find_key_and_cert(session)
        cert_der = bytes(session.getAttributeValue(cert_obj, [CKA_VALUE], True)[0])
        cert = x509.Certificate.load(cert_der)

        signed_attrs = build_signed_attributes(content_digest, cert)
        # CKM_SHA256_RSA_PKCS hashes internally: pass the DER of the SET OF
        # signed attributes (universal SET tag 0x31), NOT a pre-computed hash.
        to_sign = signed_attrs.dump()
        signature = bytes(session.sign(key_obj, to_sign, Mechanism(CKM_SHA256_RSA_PKCS, None)))
    finally:
        try:
            session.logout()
        finally:
            session.closeSession()

    der = assemble_cades_bes(cert, signed_attrs, signature)
    return base64.b64encode(der).decode('ascii')
