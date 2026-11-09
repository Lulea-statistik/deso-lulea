# DeSO – Luleå kommun

Interaktiv karta och tabellunderlag för **SCB:s Demografiska statistikområden (DeSO)** inom Luleå kommun (kommunkod 2580).

## Innehåll

- **Befolkning per DeSO:** folkmängd 31 december 2025.
- **Yta per DeSO:** landareal 1 januari 2026, km².
- **Befolkning/yta:** invånare per km², beräknat som folkmängd / landareal.
- **Befolkningsmål 100 000:** Luleås officiella folkmängd 2025 är 80 304 invånare, vilket ger ett gap på 19 696. För varje DeSO beräknas `19 696 / DeSO-befolkning` för att visa hur många områden av samma befolkningsstorlek som motsvarar gapet.
- **Karta:** interaktiv DeSO-karta med val mellan befolkning, landareal och befolkningstäthet. Popupen visar även målkvoten mot 100 000 invånare.

När arbetsflödet har körts finns:

- [`docs/data/deso_lulea.csv`](docs/data/deso_lulea.csv)
- [`docs/data/deso_lulea.geojson`](docs/data/deso_lulea.geojson)
- [`docs/data/metadata.json`](docs/data/metadata.json)
- [`docs/index.html`](docs/index.html) – interaktiv karta, publicerad via GitHub Pages från `main/docs`.

## Källor

SCB Statistikdatabasen:

- **Folkmängden per region efter ålder och kön. År 2010–2025**, tabell 000007Y7.
- **Land- och vattenareal per den 1 januari efter region och arealtyp. År 2025–2026**, tabell 000007DY.
- SCB öppna geodata: **DeSO 2025**.

## Geodata och CRS

Flödet hämtar SCB:s officiella DeSO 2025-geometri som GeoPackage och läser **källans CRS direkt ur filen**. Det filtrerar DeSO med kommunkod 2580 och transformerar därefter **hela geometrin** till **EPSG:4326** för Leaflet-kartan.

Ingen transformering av enbart bounds/hörn används.

SCB anger att arealberäkningen från 2025 görs ortogonalt i **SWEREF 99 TM**. Arealen i tabellen används som officiell landareal; den räknas alltså inte om från kartgeometrin.

## Versionsnot

SCB införde en ny version, **DeSO 2025**, under 2025. Statistik på den nya indelningen ska inte utan vidare jämföras med äldre DeSO 2018, eftersom flera områdesgränser har reviderats.

Från referensår 2025 använder SCB dessutom Cell Key Method (CKM) i befolkningsstatistiken. Små slumpmässiga avvikelser kan därför förekomma och summerade delområden behöver inte exakt motsvara publicerade totaler.

## Uppdatering

GitHub Actions-flödet **Uppdatera SCB DeSO-data och karta** körs manuellt och månadsvis. Om SCB inte har publicerat en ny årsserie blir data oförändrade.


## GitHub Pages

Publiceringskälla: **branch `main`, mapp `/docs`**.

Webbadress: https://lulea-statistik.github.io/deso-lulea/
