"""
Lo que espera a que pulses un botón en Telegram, guardado en disco.

Antes vivía en memoria y cada reinicio del bot (un `git pull` que lo relanza, un corte de
luz, systemd levantándolo tras un fallo) lo borraba: el botón contestaba «ha caducado».
Ahora está en `esperando.json`, al lado del bot (fuera de git), y dura lo que diga
`DIAS`: puedes dejar un apunte sin confirmar hoy y darle a ✅ el fin de semana.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

#  Pasado esto, lo que no se ha confirmado ni descartado se olvida.
DIAS = 14


class Esperando:
    def __init__(self, ruta: Path, dias: int = DIAS) -> None:
        self.ruta = ruta
        self.segundos = dias * 86400
        self._datos: dict[str, dict[str, Any]] = {}
        try:
            self._datos = json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._datos = {}
        self._limpiar()

    def _limpiar(self) -> None:
        limite = time.time() - self.segundos
        viejos = [k for k, v in self._datos.items() if float(v.get("_cuando", 0)) < limite]
        for k in viejos:
            self._datos.pop(k, None)
        if viejos:
            self.guardar()

    def guardar(self) -> None:
        """Llamar también después de cambiar algo por dentro (la categoría, el signo…)."""
        temporal = self.ruta.with_name(self.ruta.name + ".tmp")
        temporal.write_text(json.dumps(self._datos, ensure_ascii=False, indent=1), encoding="utf-8")
        temporal.replace(self.ruta)

    def get(self, clave: str) -> dict[str, Any] | None:
        return self._datos.get(clave)

    def __setitem__(self, clave: str, valor: dict[str, Any]) -> None:
        self._datos[clave] = {**valor, "_cuando": time.time()}
        self.guardar()

    def pop(self, clave: str, defecto: Any = None) -> Any:
        valor = self._datos.pop(clave, defecto)
        self.guardar()
        return valor

    def __len__(self) -> int:
        return len(self._datos)

    def reciente(self, perfil: str, segundos: float) -> tuple[str, dict[str, Any]] | None:
        """
        El último gasto que esa persona dejó sin confirmar, si es de hace poco. Es al que se
        refiere un «ponle de concepto brocas» que llega justo después.
        """
        limite = time.time() - segundos
        candidatos = [
            (k, v) for k, v in self._datos.items()
            if v.get("perfil") == perfil and float(v.get("_cuando", 0)) >= limite
            and (v.get("apunte") or {}).get("clase") == "gasto"
        ]
        return max(candidatos, key=lambda kv: float(kv[1].get("_cuando", 0)), default=None)
