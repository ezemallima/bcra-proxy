#!/usr/bin/env python3
"""
Generador de Certificate Signing Request (CSR) + clave privada para ARCA.

Uso:
  python generar_csr_arca.py

Genera:
  - arca_private.key  (SECRETO — nunca commitees, nunca compartas)
  - arca_request.csr  (sube esto al portal ARCA)

El CSR lo subís en: https://www.afip.gob.ar/administrador/
→ Certificados digitales → Solicitantes → Cargar solicitud de certificado
"""

import os
import sys
from pathlib import Path
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

def generar_csr_arca(cuit: str, razon_social: str = "Vende Seguro", empresa_local: str = ""):
    """
    Genera CSR + clave privada RSA 2048 para solicititud de certificado digital ARCA.

    Args:
        cuit: tu CUIT sin guiones (ej: 30710295022)
        razon_social: razón social o nombre (por defecto Vende Seguro)
        empresa_local: localidad/provincia (por defecto Buenos Aires)
    """

    cuit_limpio = str(cuit).replace('-', '').replace(' ', '').strip()
    if len(cuit_limpio) != 11 or not cuit_limpio.isdigit():
        print(f"❌ CUIT inválido: {cuit}. Debe ser 11 dígitos.", file=sys.stderr)
        sys.exit(1)

    if not empresa_local:
        empresa_local = "Buenos Aires"

    print(f"🔐 Generando CSR para ARCA...")
    print(f"   CUIT: {cuit_limpio}")
    print(f"   Razón Social: {razon_social}")
    print(f"   Localidad: {empresa_local}")
    print()

    # 1. Generar clave privada RSA 2048 (estándar ARCA)
    print("⏳ Generando clave privada RSA 2048 (esto tarda ~5s)...")
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    print("✓ Clave privada generada")

    # 2. Datos del Subject para el CSR
    # Nota: ARCA requiere CN (Common Name) con el CUIT; O (organización); C (país)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "AR"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, razon_social[:64]),
        x509.NameAttribute(NameOID.COMMON_NAME, cuit_limpio),
        x509.NameAttribute(NameOID.LOCALITY_NAME, empresa_local[:32]),
    ])

    # 3. Generar CSR
    print("⏳ Generando CSR (Certificate Signing Request)...")
    csr = x509.CertificateSigningRequestBuilder().subject_name(
        subject
    ).sign(private_key, hashes.SHA256())
    print("✓ CSR generado")

    # 4. Guardar clave privada a disco (PEM, sin contraseña — la app la lee directo)
    # ⚠️ CRÍTICO: permisos 600 (solo lectura del propietario)
    key_path = Path("arca_private.key")
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),  # sin contraseña; la encriptación viene del entorno
    )
    key_path.write_bytes(key_pem)
    os.chmod(key_path, 0o600)  # 🔒 Solo lectura del propietario
    print(f"✓ Clave privada guardada: {key_path.resolve()} [permisos 600]")

    # 5. Guardar CSR a disco (PEM, público)
    csr_path = Path("arca_request.csr")
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)
    csr_path.write_bytes(csr_pem)
    os.chmod(csr_path, 0o644)  # público
    print(f"✓ CSR generado: {csr_path.resolve()}")

    print()
    print("=" * 70)
    print("🎯 PRÓXIMO PASO: Subir el CSR a ARCA")
    print("=" * 70)
    print(f"""
1. Abrí https://www.afip.gob.ar/administrador/ (con tu Clave Fiscal)
2. Andá a: Certificados Digitales → Solicitantes → Cargar solicitud
3. Adjuntá el archivo: arca_request.csr
4. Completá:
   - Denominación: {razon_social}
   - CUIT: {cuit_limpio}
   - Actividad: Consultas vía Web Service (la que corresponda a tu trámite)
5. Hacé clic en "Solicitar Certificado"

⏳ El certificado se genera en 1-2 minutos. Volvés a descargar y guardás
   como: arca_certificate.crt

🔒 SEGURIDAD CRÍTICA:
   - arca_private.key: NUNCA commitees a git, NUNCA compartas, NUNCA publiques
   - Agregalo a .gitignore (ya está en nuestro)
   - Guardalo en Render como variable de entorno ARCA_PRIVATE_KEY
   - O subilo directo en /data/arca_private.key (solo en producción)

✓ Una vez descargado el certificado (.crt), pasame el archivo y armamos
  el módulo WSAA que renueva tokens automáticamente.
""")

    print("=" * 70)

if __name__ == "__main__":
    # Pedí CUIT al usuario si no está pasado por argumento
    if len(sys.argv) > 1:
        CUIT = sys.argv[1]
    else:
        print("📋 Generador de CSR + Clave Privada para ARCA")
        print()
        CUIT = input("Ingresá tu CUIT (ej: 30-71029502-2): ").strip()

    RAZON_SOCIAL = input("Razón social / Nombre (por defecto 'Vende Seguro'): ").strip() or "Vende Seguro"
    LOCALIDAD = input("Localidad (por defecto 'Buenos Aires'): ").strip() or "Buenos Aires"

    generar_csr_arca(CUIT, RAZON_SOCIAL, LOCALIDAD)
