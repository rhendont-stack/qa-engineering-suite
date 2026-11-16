---
tags: [trigger, es]
runs: 3
max_turns: 3
timeout_seconds: 180
allowed_tools: [Skill, Read, Glob, Grep]
---
Verifica que la API de pedidos cumpla con su contrato OpenAPI: códigos de estado, esquemas de respuesta y rutas que no estén documentadas.
