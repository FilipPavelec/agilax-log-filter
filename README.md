# Agilax Log Filter

Desktopová aplikace pro filtrování a analýzu log souborů ze zařízení Agilox.

![Model Group](MODEL_Logo_M.png)

## Funkce

- **Lazy loading** — soubor se načítá až po kliknutí na Filtrovat, pouze pro zadaný rozsah dat
- **Filtrování podle data a času** — výběr rozsahu s grafickým kalendářem
- **Filtrování podle modulů** — přepínatelné tlačítko pro každý modul
- **Filtrování podle úrovně** — ERROR, WARNING, INFO, DEBUG, ...
- **Fulltext vyhledávání** — podpora regulárních výrazů
- **Statistiky** — přehled výskytů podle modulů a úrovní
- **Export do TXT**
- **Světlý / tmavý režim**
- Podpora velkých souborů (400 MB+) bez zpomalení

## Formát logu

```
2026.05.14 00:00:00.151│    service    │[level] zpráva
```

## Instalace

```bash
pip install -r requirements.txt
python agilax_filter.py
```

## Sestavení EXE (Windows/Linux)

```bash
bash build_exe.sh
```

## Závislosti

- Python 3.10+
- ttkbootstrap >= 1.10.0
- pyinstaller >= 5.0 (pouze pro sestavení EXE)

## Autor

Model Group
