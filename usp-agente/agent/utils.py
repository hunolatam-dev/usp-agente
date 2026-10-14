# agent/utils.py — Utilidades chilenas: RUT, montos, telefonos, fechas
import re
import unicodedata
from datetime import date, datetime, timedelta

_RE_RUT = re.compile(r"\b(\d{1,2}\.?\d{3}\.?\d{3})\s*-\s*([\dkK])\b")


def dv_rut(cuerpo: int) -> str:
    """Digito verificador (modulo 11)."""
    suma, factor = 0, 2
    for d in reversed(str(cuerpo)):
        suma += int(d) * factor
        factor = 2 if factor == 7 else factor + 1
    resto = 11 - (suma % 11)
    return {11: "0", 10: "K"}.get(resto, str(resto))


def normalizar_rut(texto: str | None) -> str | None:
    """'76.123.456-k' -> '76123456-K'. Devuelve None si no es un RUT valido."""
    if not texto:
        return None
    limpio = re.sub(r"[^\dkK]", "", str(texto)).upper()
    if len(limpio) < 2 or not limpio[:-1].isdigit():
        return None
    cuerpo, dv = int(limpio[:-1]), limpio[-1]
    if cuerpo < 1_000_000 or dv_rut(cuerpo) != dv:
        return None
    return f"{cuerpo}-{dv}"


def buscar_ruts(texto: str | None) -> list[str]:
    """Todos los RUT validos (con guion) que aparecen en un texto."""
    if not texto:
        return []
    vistos: list[str] = []
    for m in _RE_RUT.finditer(texto):
        rut = normalizar_rut(m.group(1) + m.group(2))
        if rut and rut not in vistos:
            vistos.append(rut)
    return vistos


def formatear_rut(rut: str) -> str:
    cuerpo, dv = rut.split("-")
    return f"{int(cuerpo):,}".replace(",", ".") + f"-{dv}"


def parsear_monto(valor) -> int | None:
    """
    Convierte montos chilenos a entero: '$1.234.567' -> 1234567, '1234567,00' -> 1234567.
    Numeros de Excel (float/int) se respetan.
    """
    if valor is None:
        return None
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return int(round(valor))
    s = str(valor).strip().replace("$", "").replace(" ", "").replace(" ", "")
    if not s or s in {"-", "--"}:
        return None
    negativo = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("-()")
    # En Chile el punto separa miles y la coma decimales
    if "," in s:
        s = s.split(",")[0]
    s = s.replace(".", "")
    if not s.isdigit():
        return None
    n = int(s)
    return -n if negativo else n


def formatear_monto(n: int) -> str:
    return "$" + f"{int(n):,}".replace(",", ".")


def normalizar_telefono(valor) -> str | None:
    """
    Solo digitos con codigo de pais. '+56 9 7870 1038' -> '56978701038'.
    Celulares de 9 digitos sin codigo (9XXXXXXXX) se asumen chilenos.
    """
    if valor is None:
        return None
    s = re.sub(r"\D", "", str(valor).split(".")[0] if isinstance(valor, float) else str(valor))
    if not s:
        return None
    if len(s) == 9 and s.startswith("9"):
        s = "56" + s
    if len(s) == 8:  # numeros antiguos sin el 9
        s = "569" + s
    return s


_FORMATOS_FECHA = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y", "%d.%m.%Y", "%Y/%m/%d")


def parsear_fecha(valor) -> date | None:
    if valor is None or valor == "":
        return None
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    if isinstance(valor, (int, float)) and 20000 < valor < 80000:
        # Numero de serie de Excel
        return date(1899, 12, 30) + timedelta(days=int(valor))
    s = str(valor).strip().split(" ")[0].split("T")[0]
    for fmt in _FORMATOS_FECHA:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def sin_tildes(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto or "") if unicodedata.category(c) != "Mn"
    ).lower()


def solo_alfanumerico(texto: str | None) -> str:
    return re.sub(r"[^0-9a-z]", "", sin_tildes(texto or ""))
