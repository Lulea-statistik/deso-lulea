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
DOCS = ROOT / "docs"
DATA = DOCS / "data"
DOCS.mkdir(exist_ok=True)
DATA.mkdir(exist_ok=True)

POP_URL = "https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6574_sv.zip"
AREA_URL = "https://www.statistikdatabasen.scb.se/Resources/PX/bulk/ssd/sv/TAB6420_sv.zip"
POP_API = "https://api.scb.se/OV0104/v1/doris/sv/ssd/START/BE/BE0101/BE0101Y/FolkmDesoAldKon"
AREA_API = "https://api.scb.se/OV0104/v1/doris/sv/ssd/START/MI/MI0802/Areal2025"
GPKG_URL = (
    "https://geodata.scb.se/geoserver/stat/wfs?"
    "REQUEST=GetFeature&TYPENAMES=stat%3ADeSO_2025&outputFormat=geopackage"
    "&service=WFS&version=1.1.0"
)
MUNICIPALITY = "2580"
POP_YEAR = "2025"
AREA_YEAR = "2026"
MUNICIPAL_POPULATION_2025 = 80304
POPULATION_TARGET = 100000
POPULATION_GAP = POPULATION_TARGET - MUNICIPAL_POPULATION_2025
MUNICIPAL_POPULATION_SOURCE = "https://www.lulea.se/kommun--politik/fakta-och-statistik/befolkningsstatistik.html"
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


def api_metadata(url: str) -> dict:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.json()


def find_variable(meta: dict, text_contains: str) -> dict:
    needle = norm(text_contains)
    for v in meta.get("variables", []):
        if needle in norm(v.get("text", "")) or needle in norm(v.get("code", "")):
            return v
    raise KeyError(
        f"Hittar inte variabel {text_contains!r}. "
        f"Variabler={[(v.get('code'), v.get('text')) for v in meta.get('variables', [])]}"
    )


def select_value(var: dict, text_contains: str) -> str:
    needle = norm(text_contains)
    vals = var.get("values", [])
    texts = var.get("valueTexts", vals)
    for value, text in zip(vals, texts):
        if needle in norm(text):
            return value
    for value in vals:
        if needle == norm(value):
            return value
    raise KeyError(
        f"Hittar inte värde {text_contains!r} i {var.get('text')}: "
        f"{list(zip(vals, texts))[:30]}"
    )


def lulea_region_values(var: dict) -> list[str]:
    vals = var.get("values", [])
    texts = var.get("valueTexts", vals)
    out = []
    for value, text in zip(vals, texts):
        code = extract_deso(value) or extract_deso(text)
        if code and code.startswith(MUNICIPALITY):
            out.append(value)
    if not out:
        raise RuntimeError(
            f"Inga DeSO för kommunkod {MUNICIPALITY} i API-regionvariabeln. "
            f"Exempel={list(zip(vals, texts))[:20]}"
        )
    return out


def query_jsonstat(url: str, selections: dict[str, list[str]]) -> dict:
    body = {
        "query": [
            {"code": code, "selection": {"filter": "item", "values": values}}
            for code, values in selections.items()
        ],
        "response": {"format": "json-stat2"},
    }
    r = requests.post(url, json=body, timeout=120)
    r.raise_for_status()
    return r.json()


def region_values_from_jsonstat(js: dict, region_code: str) -> dict[str, float | None]:
    ids = js["id"]
    sizes = js["size"]
    if region_code not in ids:
        raise RuntimeError(f"Regiondimension {region_code} saknas i JSON-stat: {ids}")
    ridx = ids.index(region_code)
    if any(size != 1 for i, size in enumerate(sizes) if i != ridx):
        raise RuntimeError(f"Förväntade fasta övriga dimensioner, fick id={ids}, size={sizes}")

    dim = js["dimension"][region_code]
    index = dim["category"]["index"]
    if isinstance(index, dict):
        ordered = [None] * len(index)
        for key, pos in index.items():
            ordered[pos] = key
    else:
        ordered = list(index)

    vals = js["value"]
    if len(vals) != len(ordered):
        raise RuntimeError(f"Oväntad JSON-stat-längd: {len(vals)} värden för {len(ordered)} regioner")
    return dict(zip(ordered, vals))


def population() -> pd.DataFrame:
    meta = api_metadata(POP_API)
    v_region = find_variable(meta, "region")
    v_age = find_variable(meta, "ålder")
    v_sex = find_variable(meta, "kön")
    v_time = find_variable(meta, "år")

    regions = lulea_region_values(v_region)
    selections = {
        v_region["code"]: regions,
        v_age["code"]: [select_value(v_age, "totalt")],
        v_sex["code"]: [select_value(v_sex, "totalt")],
        v_time["code"]: [select_value(v_time, POP_YEAR)],
    }
    js = query_jsonstat(POP_API, selections)
    values = region_values_from_jsonstat(js, v_region["code"])

    rows = []
    text_by_value = dict(zip(v_region.get("values", []), v_region.get("valueTexts", [])))
    for region_value, value in values.items():
        code = extract_deso(region_value) or extract_deso(text_by_value.get(region_value, ""))
        if code and code.startswith(MUNICIPALITY):
            rows.append({"deso": code, "population_2025": value})
    df = pd.DataFrame(rows)
    df["population_2025"] = pd.to_numeric(df["population_2025"], errors="coerce").astype("Int64")
    # Regionvariabeln innehåller även historiska DeSO-koder. Behåll bara koder
    # som har ett faktiskt publicerat värde för referensåret 2025.
    df = df[df["population_2025"].notna()].copy()
    return df.drop_duplicates("deso")


def land_area() -> pd.DataFrame:
    meta = api_metadata(AREA_API)
    v_region = find_variable(meta, "region")
    v_type = find_variable(meta, "arealtyp")
    v_time = find_variable(meta, "år")
    v_content = find_variable(meta, "tabellinnehåll")

    regions = lulea_region_values(v_region)
    selections = {
        v_content["code"]: [select_value(v_content, "kvadratkilometer")],
        v_region["code"]: regions,
        v_type["code"]: [select_value(v_type, "landareal")],
        v_time["code"]: [select_value(v_time, AREA_YEAR)],
    }
    js = query_jsonstat(AREA_API, selections)
    values = region_values_from_jsonstat(js, v_region["code"])

    rows = []
    text_by_value = dict(zip(v_region.get("values", []), v_region.get("valueTexts", [])))
    for region_value, value in values.items():
        code = extract_deso(region_value) or extract_deso(text_by_value.get(region_value, ""))
        if code and code.startswith(MUNICIPALITY):
            rows.append({"deso": code, "land_area_km2_2026": value})
    df = pd.DataFrame(rows)
    df["land_area_km2_2026"] = pd.to_numeric(df["land_area_km2_2026"], errors="coerce")
    # Regionvariabeln innehåller även historiska DeSO-koder. Behåll bara koder
    # som har ett faktiskt publicerat arealvärde för 2026.
    df = df[df["land_area_km2_2026"].notna()].copy()
    return df.drop_duplicates("deso")


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
            "target_equivalent_deso": round(float(row["target_equivalent_deso"]), 1) if pd.notna(row["target_equivalent_deso"]) else None,
            "target_area_km2": round(float(row["target_area_km2"]), 2) if pd.notna(row["target_area_km2"]) else None,
            "target_area_ha": round(float(row["target_area_ha"]), 0) if pd.notna(row["target_area_ha"]) else None,
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
        "population_source": POP_API,
        "area_source": AREA_API,
        "geometry_source": GPKG_URL,
        "municipal_population_2025": MUNICIPAL_POPULATION_2025,
        "population_target": POPULATION_TARGET,
        "population_gap": POPULATION_GAP,
        "municipal_population_source": MUNICIPAL_POPULATION_SOURCE,
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
<div class="muted">SCB DeSO 2025 · folkmängd 2025-12-31 · landareal 2026-01-01</div>
<div class="muted">Luleå 2025: 80 304 invånare · mål: 100 000 · gap: 19 696</div></div>
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
fetch('data/deso_lulea.geojson?v='+Date.now(), {cache:'no-store'}).then(r=>r.json()).then(g=>{
 let b=breaks(g);
 function style(f){return {weight:1,color:'#555',fillOpacity:.72,fillColor:color(+f.properties[metric],b)}}
 function draw(){
  if(layer)map.removeLayer(layer);b=breaks(g);
  layer=L.geoJSON(g,{style,onEachFeature:(f,l)=>l.bindPopup(
   '<b>'+f.properties.deso+'</b><br>Befolkning: '+fmt(f.properties.population_2025)+
   '<br>Landareal: '+fmt(f.properties.land_area_km2_2026)+' km²'+
   '<br>Täthet: '+fmt(f.properties.population_per_km2)+' inv./km²'+
   '<br><b>Behövs för 100 000:</b> '+fmt(f.properties.target_equivalent_deso)+' × detta DeSO'+
   '<br><b>Yta som behövs för nya stadsdelar:</b> '+fmt(f.properties.target_area_km2)+' km² ('+fmt(f.properties.target_area_ha)+' ha)'
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
    geoms, source_crs = boundaries()

    df = pop.merge(area, on="deso", how="outer")
    current_codes = set(geoms)
    extra_historical = sorted(set(df["deso"]) - current_codes)
    if extra_historical:
        print(
            "Ignorerar statistik-koder som inte finns i aktuell DeSO 2025-geometri: "
            + ", ".join(extra_historical)
        )

    # Den officiella DeSO 2025-geometrin definierar den aktuella områdesmängden.
    df = df[df["deso"].isin(current_codes)].copy()
    missing_stats = sorted(current_codes - set(df["deso"]))
    if missing_stats:
        raise RuntimeError(f"Aktuell DeSO-geometri saknar statistik: {missing_stats}")

    missing_population = sorted(df.loc[df["population_2025"].isna(), "deso"].tolist())
    missing_area = sorted(df.loc[df["land_area_km2_2026"].isna(), "deso"].tolist())
    if missing_population or missing_area:
        raise RuntimeError(
            f"Ofullständig statistik. Befolkning saknas={missing_population}; "
            f"landareal saknas={missing_area}"
        )

    df["population_per_km2"] = df["population_2025"] / df["land_area_km2_2026"]
    df["target_equivalent_deso"] = POPULATION_GAP / df["population_2025"]
    df["target_area_km2"] = df["land_area_km2_2026"] * df["target_equivalent_deso"]
    df["target_area_ha"] = df["target_area_km2"] * 100
    df = df.sort_values("deso")

    write_outputs(df, geoms, source_crs)
    (DOCS / "index.html").write_text(map_html(), encoding="utf-8")
    print(
        f"Klart: {len(df)} aktuella DeSO i Luleå. "
        f"Käll-CRS: {source_crs}; karta: EPSG:4326"
    )


if __name__ == "__main__":
    main()
