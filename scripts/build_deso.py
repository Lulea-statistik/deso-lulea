from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from pathlib import Path

import fiona
import pandas as pd
import requests
from pyproj import CRS, Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

POP_URL = "https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6574_sv.zip"
AREA_URL = "https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6420_sv.zip"
GPKG_URL = (
    "https://geodata.scb.se/geoserver/stat/wfs?"
    "REQUEST=GetFeature&TYPENAMES=stat%3ADeSO_2025&outputFormat=geopackage"
    "&service=WFS&version=1.1.0"
)
MUNICIPALITY = "2580"
POP_YEAR = "2025"
AREA_YEAR = "2026"
DESO_RE = re.compile(r"\b(\d{4}[A-Z]\d{4})\b")


def download(url: str) -> bytes:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.content


def read_bulk_zip(url: str) -> pd.DataFrame:
    raw = download(url)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise RuntimeError(f"Ingen CSV i {url}")
        b = z.read(names[0])
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return pd.read_csv(io.BytesIO(b), sep=";", dtype=str, encoding=enc)
        except UnicodeDecodeError:
            pass
    raise RuntimeError("Kunde inte avkoda SCB CSV")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


def find_col(df: pd.DataFrame, needle: str) -> str:
    for c in df.columns:
        if needle in norm(c):
            return c
    raise KeyError(f"Hittar inte kolumn som innehåller {needle!r}: {list(df.columns)}")


def extract_deso(value: str) -> str | None:
    m = DESO_RE.search(str(value))
    return m.group(1) if m else None


def numeric_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(
        s.astype(str)
         .str.replace("\u00a0", "", regex=False)
         .str.replace(" ", "", regex=False)
         .str.replace(",", ".", regex=False),
        errors="coerce",
    )


def pick_value_col(df: pd.DataFrame, excluded: set[str], preferred: tuple[str, ...]) -> str:
    for p in preferred:
        for c in df.columns:
            if c not in excluded and p in norm(c):
                return c
    best, best_count = None, -1
    for c in df.columns:
        if c in excluded:
            continue
        count = int(numeric_series(df[c]).notna().sum())
        if count > best_count:
            best, best_count = c, count
    if best is None:
        raise RuntimeError("Ingen numerisk värdekolumn hittades")
    return best


def population() -> pd.DataFrame:
    df = read_bulk_zip(POP_URL)
    c_region = find_col(df, "region")
    c_age = find_col(df, "ålder")
    c_sex = find_col(df, "kön")
    c_year = find_col(df, "år")

    x = df[
        df[c_region].astype(str).str.contains(MUNICIPALITY, na=False)
        & df[c_age].map(norm).eq("totalt")
        & df[c_sex].map(norm).eq("totalt")
        & df[c_year].astype(str).eq(POP_YEAR)
    ].copy()
    x["deso"] = x[c_region].map(extract_deso)
    x = x[x["deso"].notna() & x["deso"].str.startswith(MUNICIPALITY)]
    v = pick_value_col(x, {c_region, c_age, c_sex, c_year, "deso"}, ("antal",))
    x["population_2025"] = numeric_series(x[v]).astype("Int64")
    return x[["deso", "population_2025"]].drop_duplicates("deso")


def land_area() -> pd.DataFrame:
    df = read_bulk_zip(AREA_URL)
    c_region = find_col(df, "region")
    c_type = find_col(df, "arealtyp")
    c_year = find_col(df, "år")

    x = df[
        df[c_region].astype(str).str.contains(MUNICIPALITY, na=False)
        & df[c_type].map(norm).str.contains("landareal", na=False)
        & df[c_year].astype(str).eq(AREA_YEAR)
    ].copy()
    x["deso"] = x[c_region].map(extract_deso)
    x = x[x["deso"].notna() & x["deso"].str.startswith(MUNICIPALITY)]
    v = pick_value_col(x, {c_region, c_type, c_year, "deso"}, ("kvadratkilometer", "km"))
    x["land_area_km2_2026"] = numeric_series(x[v])
    return x[["deso", "land_area_km2_2026"]].drop_duplicates("deso")


def find_code(props: dict) -> str | None:
    for v in props.values():
        code = extract_deso(v)
        if code:
            return code
    return None


def boundaries() -> tuple[dict[str, dict], str]:
    gpkg = DATA / "DeSO_2025.gpkg"
    gpkg.write_bytes(download(GPKG_URL))
    layers = fiona.listlayers(gpkg)
    if not layers:
        raise RuntimeError("SCB GeoPackage saknar lager")

    geoms: dict[str, dict] = {}
    source_crs = None
    with fiona.open(gpkg, layer=layers[0]) as src:
        source_crs = src.crs_wkt or src.crs
        if not source_crs:
            raise RuntimeError("Käll-CRS saknas i SCB GeoPackage")
        src_crs_obj = CRS.from_user_input(source_crs)
        transformer = Transformer.from_crs(src_crs_obj, CRS.from_epsg(4326), always_xy=True)

        for feat in src:
            props = dict(feat["properties"])
            code = find_code(props)
            if not code or not code.startswith(MUNICIPALITY):
                continue
            geom = shape(feat["geometry"])
            geom4326 = transform(transformer.transform, geom)
            geoms[code] = mapping(geom4326)

    return geoms, CRS.from_user_input(source_crs).to_string()


def write_outputs(df: pd.DataFrame, geoms: dict[str, dict], source_crs: str) -> None:
    out_csv = DATA / "deso_lulea.csv"
    df.to_csv(out_csv, index=False, encoding="utf-8")

    features = []
    for row in df.to_dict("records"):
        code = row["deso"]
        if code not in geoms:
            continue
        props = {
            "deso": code,
            "population_2025": int(row["population_2025"]) if pd.notna(row["population_2025"]) else None,
            "land_area_km2_2026": round(float(row["land_area_km2_2026"]), 4) if pd.notna(row["land_area_km2_2026"]) else None,
            "population_per_km2": round(float(row["population_per_km2"]), 1) if pd.notna(row["population_per_km2"]) else None,
        }
        features.append({"type": "Feature", "properties": props, "geometry": geoms[code]})

    geo = {"type": "FeatureCollection", "features": features}
    (DATA / "deso_lulea.geojson").write_text(json.dumps(geo, ensure_ascii=False), encoding="utf-8")

    meta = {
        "municipality_code": MUNICIPALITY,
        "deso_version": "DeSO 2025",
        "population_reference": "2025-12-31",
        "area_reference": "2026-01-01",
        "source_geometry_crs": source_crs,
        "output_geometry_crs": "EPSG:4326",
        "population_source": POP_URL,
        "area_source": AREA_URL,
        "geometry_source": GPKG_URL,
    }
    (DATA / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def map_html() -> str:
    return """<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DeSO Luleå – befolkning, yta och täthet</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
html,body{margin:0;font-family:system-ui,Segoe UI,sans-serif;background:#f7f7f5;color:#1f2937}
.wrap{max-width:1200px;margin:auto;padding:20px}.top{display:flex;gap:16px;align-items:end;flex-wrap:wrap}
h1{margin:0 0 4px}.muted{color:#6b7280}.card{background:white;border:1px solid #e5e7eb;border-radius:12px;padding:14px}
#map{height:72vh;min-height:520px;border-radius:12px;margin-top:14px}
select{padding:8px 10px;border:1px solid #cbd5e1;border-radius:8px;background:white}
.legend{background:white;padding:8px 10px;border-radius:8px;line-height:1.5}
.info{background:white;padding:8px 10px;border-radius:8px;box-shadow:0 1px 5px #999}
</style></head>
<body><main class="wrap">
<div class="top"><div style="flex:1"><h1>DeSO i Luleå kommun</h1>
<div class="muted">SCB DeSO 2025 · folkmängd 2025-12-31 · landareal 2026-01-01</div></div>
<div class="card"><label for="metric">Visa </label><select id="metric">
<option value="population_per_km2">Befolkningstäthet (inv./km²)</option>
<option value="population_2025">Befolkning</option>
<option value="land_area_km2_2026">Landareal (km²)</option>
</select></div></div>
<div id="map"></div>
<p class="muted">Källa: SCB Statistikdatabasen och SCB öppna geodata. Klicka på ett område för värden.</p>
</main>
<script>
const map=L.map('map').setView([65.58,22.15],9);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:18,attribution:'© OpenStreetMap'}).addTo(map);
let layer,metric='population_per_km2';
const labels={population_per_km2:'inv./km²',population_2025:'invånare',land_area_km2_2026:'km²'};
function vals(g){return g.features.map(f=>+f.properties[metric]).filter(Number.isFinite).sort((a,b)=>a-b)}
function breaks(g){const a=vals(g);return [0,.2,.4,.6,.8,1].map(q=>a[Math.min(a.length-1,Math.floor(q*(a.length-1)))])}
function color(v,b){const cs=['#eff3ff','#bdd7e7','#6baed6','#3182bd','#08519c'];for(let i=1;i<b.length;i++)if(v<=b[i])return cs[i-1];return cs[4]}
function fmt(v){return v==null?'–':Number(v).toLocaleString('sv-SE',{maximumFractionDigits:1})}
fetch('data/deso_lulea.geojson').then(r=>r.json()).then(g=>{
 let b=breaks(g);
 function style(f){return {weight:1,color:'#555',fillOpacity:.72,fillColor:color(+f.properties[metric],b)}}
 function draw(){
  if(layer)map.removeLayer(layer);b=breaks(g);
  layer=L.geoJSON(g,{style,onEachFeature:(f,l)=>l.bindPopup(
   '<b>'+f.properties.deso+'</b><br>Befolkning: '+fmt(f.properties.population_2025)+
   '<br>Landareal: '+fmt(f.properties.land_area_km2_2026)+' km²'+
   '<br>Täthet: '+fmt(f.properties.population_per_km2)+' inv./km²'
  )}).addTo(map);
  map.fitBounds(layer.getBounds());
  updateLegend();
 }
 const legend=L.control({position:'bottomright'});legend.onAdd=()=>{const d=L.DomUtil.create('div','legend');d.id='legend';return d};legend.addTo(map);
 function updateLegend(){const d=document.getElementById('legend');d.innerHTML='<b>'+document.querySelector('#metric option:checked').textContent+'</b><br>'+
  b.slice(0,-1).map((x,i)=>'<span style="display:inline-block;width:12px;height:12px;background:'+color((b[i]+b[i+1])/2,b)+'"></span> '+fmt(b[i])+'–'+fmt(b[i+1])+'<br>').join('')}
 document.getElementById('metric').addEventListener('change',e=>{metric=e.target.value;draw()});draw();
});
</script></body></html>"""
    

def main() -> None:
    pop = population()
    area = land_area()
    df = pop.merge(area, on="deso", how="outer")
    df["population_per_km2"] = df["population_2025"] / df["land_area_km2_2026"]
    df = df.sort_values("deso")
    geoms, source_crs = boundaries()

    missing_geom = sorted(set(df["deso"]) - set(geoms))
    if missing_geom:
        raise RuntimeError(f"Saknar geometri för DeSO: {missing_geom}")

    write_outputs(df, geoms, source_crs)
    (ROOT / "index.html").write_text(map_html(), encoding="utf-8")
    print(f"Klart: {len(df)} DeSO i Luleå. Käll-CRS: {source_crs}; karta: EPSG:4326")


if __name__ == "__main__":
    main()
