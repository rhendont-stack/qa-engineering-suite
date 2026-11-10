"""The Spanish requirements file of the Spanish scenario, and the ambiguous words it holds (read by the setup and by the grader).

Neither this file nor the setup script is copied into the agent's working copy: the agent sees only ``requisitos.md`` itself,
which carries no hint of what is expected of it. ``EXPECTED`` lists, per line of ``TEXT``, the vague or open-ended words a
requirements review must flag, as a pattern over accent-folded lower-case text (so ``adecuada`` also matches ``adecuado``).
"""
from __future__ import annotations

from typing import List, Tuple

NAME = "requisitos.md"

TEXT = """# Requisitos de precios de la tienda

Versión 0.4, mantenida por el equipo de precios.

## Pedidos y descuentos

P1. Un pedido de 100,00 o más recibe un descuento por volumen del 10 por ciento.

P2. El cálculo del total debe ser eficiente y el recibo debe resultar intuitivo para el cliente.

P3. El impuesto se redondea de forma adecuada y el recibo muestra el desglose, entre otros datos.

## Cupones

P4. Un cupón descuenta un importe fijo del pedido, después de aplicar los descuentos porcentuales.

P5. Los cupones caducados se rechazan en la medida de lo posible, con un mensaje que indica la fecha de caducidad.

## Pagos

P6. El total se envía a la pasarela de pago en céntimos enteros y normalmente en menos de 2 segundos.
"""

# (line, label, pattern over folded text)
EXPECTED: List[Tuple[int, str, str]] = [
    (9, "eficiente", r"eficient"),
    (9, "intuitivo", r"intuitiv"),
    (11, "adecuada", r"adecuad"),
    (11, "entre otros", r"entre otr"),
    (17, "en la medida de lo posible", r"medida de lo posible"),
    (21, "normalmente", r"normalmente"),
]
