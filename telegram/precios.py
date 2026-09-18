"""
Cuánto cuesta cada mensaje al bot.

Precios oficiales de OpenAI en dólares por millón de tokens, tal como estaban en
https://developers.openai.com/api/docs/pricing el 18/09/2026. Si cambian, se cambian aquí
y nada más: el bot calcula el coste con lo que devuelve la propia API (`usage`).

Un mensaje típico («15 € en un bar») lleva unas 3.000-3.500 fichas de entrada (las
instrucciones, tus categorías y la memoria de conceptos) y unas 80 de salida. La parte
fija se repite igual en cada mensaje, así que OpenAI la cobra como «cacheada» a partir
del segundo mensaje seguido.
"""

from __future__ import annotations

from dataclasses import dataclass

REVISADO = "2026-09-18"


@dataclass(frozen=True)
class Precio:
    entrada: float
    entrada_cacheada: float
    salida: float


#  $ por 1M de tokens.
PRECIOS: dict[str, Precio] = {
    "gpt-4.1-mini": Precio(0.40, 0.10, 1.60),
    "gpt-4.1-nano": Precio(0.10, 0.025, 0.40),
    "gpt-4o-mini": Precio(0.15, 0.075, 0.60),
    "gpt-5-mini": Precio(0.25, 0.025, 2.00),
    "gpt-5-nano": Precio(0.05, 0.005, 0.40),
    "gpt-5.4-mini": Precio(0.75, 0.075, 4.50),
    "gpt-5.4-nano": Precio(0.20, 0.02, 1.25),
}

#  Para pasar a euros sin pedir el cambio a nadie. Es una estimación, no una factura.
EUROS_POR_DOLAR = 0.86


def coste_dolares(modelo: str, entrada: int, cacheada: int, salida: int) -> float | None:
    """Lo que ha costado una llamada. None si el modelo no está en la tabla."""
    precio = PRECIOS.get(modelo)
    if precio is None:
        # «gpt-4.1-mini-2025-04-14» y compañía: se busca por el nombre base.
        precio = next((p for nombre, p in PRECIOS.items() if modelo.startswith(nombre + "-")), None)
    if precio is None:
        return None
    normal = max(0, entrada - cacheada)
    return (normal * precio.entrada + cacheada * precio.entrada_cacheada + salida * precio.salida) / 1_000_000


#  Notas de voz con gpt-4o-mini-transcribe: OpenAI lo da como «0,003 $ por minuto»
#  (estimado; en realidad cobra por tokens de audio). Se cuenta por segundos.
DOLARES_MINUTO_VOZ = 0.003


def coste_voz(segundos: float) -> float:
    return max(0.0, segundos) / 60 * DOLARES_MINUTO_VOZ


def en_centimos(dolares: float) -> str:
    """«0,13 cént.»: a esta escala, los céntimos de euro se leen mejor que los dólares."""
    centimos = dolares * EUROS_POR_DOLAR * 100
    return f"{centimos:.2f}".replace(".", ",") + " cént."
