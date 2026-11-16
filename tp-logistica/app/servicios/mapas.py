"""Visualización geográfica de las posiciones GPS (fase 9).

Arma una página HTML con un mapa (Leaflet + OpenStreetMap, se cargan de internet al abrirla) y el
recorrido de cada contenedor dibujado como una línea, con el primer y el último punto marcados.
Las posiciones salen de Cassandra (tabla posiciones_por_envio). La función es pura: recibe los
puntos y devuelve el texto HTML; quién la guarda y la abre en el navegador es el servicio y el menú.
"""
from __future__ import annotations

import html
import json

COLORES = ["#1f6feb", "#d73a49", "#2da44e", "#bf8700", "#8250df", "#0e7490"]


def html_mapa(titulo: str, puntos: list[dict]) -> str:
    """puntos: [{fecha_hora, id_contenedor, latitud, longitud, envio?}] en cualquier orden."""
    lineas: dict[str, list] = {}
    for p in sorted(puntos, key=lambda x: x["fecha_hora"]):
        clave = f"Contenedor {p['id_contenedor']}" + (f" · envío {p['envio']}" if p.get("envio") else "")
        lineas.setdefault(clave, []).append([round(p["latitud"], 5), round(p["longitud"], 5),
                                             p["fecha_hora"].strftime("%d/%m/%Y %H:%M UTC")])
    datos = json.dumps([{"nombre": k, "color": COLORES[i % len(COLORES)], "puntos": v}
                        for i, (k, v) in enumerate(lineas.items())], ensure_ascii=False)
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>{html.escape(titulo)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>body{{margin:0;font-family:sans-serif}} #mapa{{height:100vh}}
h1{{position:absolute;z-index:1000;top:8px;left:60px;margin:0;padding:6px 10px;font-size:16px;
background:#fff;border-radius:6px;box-shadow:0 1px 4px #0004}}</style></head>
<body><h1>{html.escape(titulo)}</h1><div id="mapa"></div>
<script>
const lineas = {datos};
const mapa = L.map('mapa');
L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
  {{maxZoom: 18, attribution: '&copy; OpenStreetMap'}}).addTo(mapa);
const limites = [];
for (const l of lineas) {{
  const latlng = l.puntos.map(p => [p[0], p[1]]);
  limites.push(...latlng);
  L.polyline(latlng, {{color: l.color, weight: 4}}).addTo(mapa).bindPopup(l.nombre);
  const a = l.puntos[0], b = l.puntos[l.puntos.length - 1];
  L.circleMarker([a[0], a[1]], {{color: l.color, radius: 7}}).addTo(mapa).bindPopup(l.nombre + '<br>Primera: ' + a[2]);
  L.marker([b[0], b[1]]).addTo(mapa).bindPopup(l.nombre + '<br>Última: ' + b[2]);
}}
mapa.fitBounds(limites, {{padding: [30, 30]}});
</script></body></html>
"""
