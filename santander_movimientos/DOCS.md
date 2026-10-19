# Santander Movimientos

Descarga los movimientos de cuenta corriente y tarjetas de crédito de Santander Chile
(banca personas) con fintself + Playwright, los acumula sin duplicar en
`/config/santander/historico.csv` y los publica en Home Assistant por MQTT.

## Configuración
- **rut / clave**: credenciales de banca en línea. La clave queda como campo `password` del Supervisor.
- **intervalo_horas**: cada cuántas horas descarga (2–48).
- **ejecutar_al_iniciar**: descargar apenas arranca la App.
- **incluir_tarjetas**: también tarjetas de crédito (no facturado, facturado y períodos anteriores).
- **capturar_api**: guarda las respuestas internas del sitio (con tokens redactados) en
  `/config/santander/raw/` para analizar su API. Se conservan las últimas 5 corridas.

## Seguridad
Si el login falla, la App no reintenta sola hasta apretar el botón **Actualizar** o cambiar la clave,
para no bloquear la clave en el banco.
