"""
El lado del bot de la bandeja de Folio (ver backend/core/bandeja.py en Folio).

En la carpeta de Drive, cada perfil tiene la suya:

    bandeja/<perfil>/contexto.json     lo escribe Folio: categorías, cuentas y memoria
    bandeja/<perfil>/ultimos.json      lo escribe Folio: los últimos movimientos apuntados
    bandeja/<perfil>/entrantes/*.json  lo escribe el bot: un fichero por apunte

La Raspberry llega a Drive de una de dos maneras, y las dos valen:

  · Carpeta: Drive montado con `rclone mount` (o cualquier carpeta sincronizada).
    FOLIO_BANDEJA=/home/pi/drive/Folio/bandeja
  · rclone a secas, sin montar nada: el bot llama a `rclone cat` y `rclone copyto`.
    FOLIO_RCLONE=gdrive:Folio/bandeja
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

#  El contexto cambia poco (cuando tocas categorías en Folio): se relee como mucho cada 5 min.
_CACHE_SEGUNDOS = 300


def _seguro(texto: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "", texto.lower()) or "perfil"


class Bandeja:
    def __init__(self, carpeta: str | None = None, remoto: str | None = None) -> None:
        if not carpeta and not remoto:
            raise ValueError("Falta FOLIO_BANDEJA (una carpeta) o FOLIO_RCLONE (un remoto de rclone).")
        self.carpeta = Path(carpeta) if carpeta else None
        self.remoto = remoto.rstrip("/") if remoto else None
        self._cache: dict[str, tuple[float, dict]] = {}

    # ── Leer lo que publica Folio ──────────────────────────────────────────

    def olvidar(self, perfil: str | None = None) -> None:
        """Tira la copia guardada para releer Drive en el siguiente mensaje (/recargar)."""
        if perfil is None:
            self._cache.clear()
        else:
            self._cache.pop(perfil, None)

    def ultimos(self, perfil: str) -> dict[str, Any]:
        """Lo último apuntado en Folio. Cambia a menudo, así que no se guarda copia."""
        texto = self._leer(f"{_seguro(perfil)}/ultimos.json")
        if texto is None:
            raise FileNotFoundError(
                "Todavía no sé lo que tienes apuntado. Abre Folio en el ordenador una vez y vuelve a preguntar."
            )
        return json.loads(texto)

    def contexto(self, perfil: str) -> dict[str, Any]:
        guardado = self._cache.get(perfil)
        if guardado and time.monotonic() - guardado[0] < _CACHE_SEGUNDOS:
            return guardado[1]
        texto = self._leer(f"{_seguro(perfil)}/contexto.json")
        if texto is None:
            if guardado:
                return guardado[1]  # Drive no responde: se sigue con lo último conocido
            raise FileNotFoundError(
                f"No encuentro el contexto de «{perfil}». Abre Folio en el ordenador con la carpeta "
                "de sincronización puesta: lo publica solo."
            )
        datos = json.loads(texto)
        self._cache[perfil] = (time.monotonic(), datos)
        return datos

    def _leer(self, relativo: str) -> str | None:
        if self.carpeta is not None:
            ruta = self.carpeta / relativo
            try:
                return ruta.read_text(encoding="utf-8")
            except OSError:
                return None
        r = subprocess.run(["rclone", "cat", f"{self.remoto}/{relativo}"], capture_output=True, timeout=60)
        return r.stdout.decode("utf-8") if r.returncode == 0 else None

    # ── Dejar un apunte ────────────────────────────────────────────────────

    def dejar_varios(self, perfil: str, apuntes: list[dict[str, Any]]) -> list[str]:
        """
        Varios de una vez. Con rclone es **una sola** subida para todos (cada `rclone`
        tarda unos segundos en arrancar: ocho de uno en uno eran casi un minuto).
        """
        if self.carpeta is not None or len(apuntes) <= 1:
            return [self.dejar(perfil, a) for a in apuntes]
        with tempfile.TemporaryDirectory() as tmp:
            nombres = []
            for a in apuntes:
                nombre = f"{a['fecha']}-{a['id']}.json"
                (Path(tmp) / nombre).write_text(json.dumps(a, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
                nombres.append(nombre)
            subprocess.run(["rclone", "copy", tmp, f"{self.remoto}/{_seguro(perfil)}/entrantes"], check=True,
                           capture_output=True, timeout=180)
        return nombres

    def dejar(self, perfil: str, apunte: dict[str, Any]) -> str:
        """Deja el apunte en `entrantes/`. Devuelve el nombre del fichero."""
        nombre = f"{apunte['fecha']}-{apunte['id']}.json"
        relativo = f"{_seguro(perfil)}/entrantes/{nombre}"
        texto = json.dumps(apunte, ensure_ascii=False, indent=1) + "\n"
        if self.carpeta is not None:
            destino = self.carpeta / relativo
            destino.parent.mkdir(parents=True, exist_ok=True)
            # Primero con otro nombre y luego se renombra: Folio nunca ve uno a medias.
            temporal = destino.with_name("." + destino.name + ".tmp")
            temporal.write_text(texto, encoding="utf-8")
            os.replace(temporal, destino)
            return nombre
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
            fh.write(texto)
            local = fh.name
        try:
            subprocess.run(["rclone", "copyto", local, f"{self.remoto}/{relativo}"], check=True,
                           capture_output=True, timeout=120)
        finally:
            os.unlink(local)
        return nombre
