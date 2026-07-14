import hashlib
import base64

try:
    from PyKCS11 import *
except ImportError:
    pass  # We will install this when building the exe

def serialize_invoice(invoice_dict):
    """
    Canonicalization (التكويد المتسلسل)
    Converts JSON to UPPERCASE string without spaces according to ETA rules.
    """
    serialized_str = ""
    for key, value in invoice_dict.items():
        serialized_str += f'"{key.upper()}"'
        if isinstance(value, dict):
            serialized_str += serialize_invoice(value)
        elif isinstance(value, list):
            for item in value:
                serialized_str += f'"{key.upper()}"'
                if isinstance(item, dict):
                    serialized_str += serialize_invoice(item)
                else:
                    serialized_str += f'"{item}"'
        else:
            serialized_str += f'"{value}"'
    return serialized_str

def hash_and_sign(serialized_str, dll_path, pin):
    """
    Tries to connect to the ePass2003 hardware token, generates SHA256 hash, 
    and signs it using the token's private key.
    """
    hash_bytes = hashlib.sha256(serialized_str.encode('utf-8')).digest()
    
    pkcs11 = PyKCS11Lib()
    pkcs11.load(dll_path)
    
    # Get token slot
    slots = pkcs11.getSlotList(tokenPresent=True)
    if not slots:
        raise Exception("لم يتم العثور على توكن (ePass2003). تأكد من توصيل الفلاشة.")
    
    slot = slots[0]
    session = pkcs11.openSession(slot, CKF_SERIAL_SESSION | CKF_RW_SESSION)
    session.login(pin)
    
    try:
        # Find private key and certificate
        priv_keys = session.findObjects([(CKA_CLASS, CKO_PRIVATE_KEY)])
        certs = session.findObjects([(CKA_CLASS, CKO_CERTIFICATE)])
        
        if not priv_keys or not certs:
            raise Exception("لم يتم العثور على شهادة توقيع صحيحة داخل التوكن.")
            
        priv_key = priv_keys[0]
        cert = certs[0]
        
        # Sign the hash
        mechanism = Mechanism(CKM_SHA256_RSA_PKCS, None)
        raw_signature = bytes(session.sign(priv_key, hash_bytes, mechanism))
        
        # Get public cert data
        cert_der = bytes(cert.to_dict()['CKA_VALUE'])
        
        return raw_signature, cert_der, hash_bytes
        
    finally:
        session.logout()
        session.closeSession()

def build_cades_bes(raw_signature, cert_der, hash_bytes):
    """
    Wraps the raw signature into a CAdES-BES PKCS7 container.
    (This requires the 'endesive' or 'asn1crypto' library).
    For now, this returns a mock base64 string if library is missing,
    or you can implement the full ASN.1 structure.
    """
    # NOTE: The actual implementation using endesive goes here.
    # We will simulate the successful CAdES-BES wrap for now.
    # Real implementation requires deep ASN.1 structure building.
    
    cades_bes_binary = b"SIMULATED_CADES_BES_STRUCTURE_REPLACE_WITH_ENDESIVE"
    final_base64_signature = base64.b64encode(cades_bes_binary).decode('utf-8')
    return final_base64_signature
