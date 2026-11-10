# Orto Merge Tool

Script en Python que une las teselas de una ortofoto (por ejemplo ECW con
su `.prj`) en un único GeoTIFF liviano, pensado para cargar rápido en
Global Mapper.

El archivo resultante es un Cloud Optimized GeoTIFF (COG) con:

- **resolución reducida** según un factor configurable,
- **compresión JPEG** con calidad configurable,
- **bloques internos y pirámides**, para que Global Mapper lea solo la zona
  y el nivel de detalle que se está mirando.

## Requisitos

Global Mapper no incluye Python ni GDAL, así que se necesita instalar
**OSGeo4W** (gratuito). No hace falta instalar QGIS.

1. Descarga el instalador de red `osgeo4w-setup.exe` desde
   <https://trac.osgeo.org/osgeo4w/>.
2. Elige **Advanced Install** → **Install from Internet**.
3. En la lista de paquetes busca y marca:
   - `python3-gdal` (GDAL para Python)
   - `gdal-ecw` (lectura de archivos ECW)
4. Termina la instalación.

> La instalación de GDAL por `pip` o por Conda normalmente **no** lee ECW,
> porque es un formato propietario. Por eso se usa OSGeo4W.

`ejecutar.bat` busca OSGeo4W automáticamente en estas carpetas:

- `C:\OSGeo4W`
- `%LOCALAPPDATA%\Programs\OSGeo4W` (instalación sin permisos de
  administrador, por ejemplo `C:\Users\usuario\AppData\Local\Programs\OSGeo4W`)
- `C:\Program Files\OSGeo4W`

Si quedó en otra carpeta, escribe la ruta en la línea
`set "OSGEO4W_ROOT="` dentro de `ejecutar.bat`.

`ejecutar.bat` también configura `GDAL_DRIVER_PATH` y `GDAL_DATA`, necesarios
para que GDAL encuentre el complemento ECW y sus archivos de datos cuando
OSGeo4W no está en `C:\OSGeo4W`.

Para verificar la instalación desde OSGeo4W Shell:

```bat
python -c "from osgeo import gdal; print(gdal.__version__, gdal.GetDriverByName('ECW') is not None)"
```

Si responde `False`, ejecuta antes
`set GDAL_DRIVER_PATH=%OSGEO4W_ROOT%\apps\gdal\lib\gdalplugins` y repite.

## Antes de ejecutar

1. Si las teselas están en OneDrive, haz clic derecho en la carpeta y elige
   **"Mantener siempre en este dispositivo"**. El script avisa si detecta
   archivos que siguen solo en la nube.
2. Guarda la salida **fuera de OneDrive** para que no se sincronice un
   archivo de varios GB.
3. Verifica que haya espacio libre suficiente. El script muestra una
   estimación y pide confirmación antes de empezar.

## Uso

### Modo interactivo (recomendado)

Haz **doble clic en `ejecutar.bat`**. El script pregunta, en orden:

1. Carpeta de las teselas (puedes pegar la ruta con "Copiar como ruta").
2. Tipo de archivo a unir (`*.ecw` por defecto).
3. Archivo de salida (si no pones `.tif`, se agrega solo).
4. Nivel de reducción: `alto`, `medio`, `bajo` o `personalizado`.
   En `personalizado` se pide el tamaño de píxel o el factor, y la
   calidad JPEG.
5. Valor sin datos en los bordes (Enter si no hay).
6. Si quieres configurar opciones avanzadas (remuestreo, bloque,
   pirámides y caché).

Luego revisa las teselas, muestra el tamaño de píxel resultante y el peso
estimado, y pregunta si quieres iniciar. Al terminar indica el peso final
del archivo.

### Modo por argumentos

También se puede ejecutar sin preguntas, útil para automatizar. Desde
**OSGeo4W Shell**:

```bat
python orto_merge_tool.py "D:\ortofoto\teselas" "D:\salida\ortofoto.tif"
python orto_merge_tool.py teselas ortofoto.tif --nivel bajo
python orto_merge_tool.py teselas ortofoto.tif --factor 3 --calidad 70
python orto_merge_tool.py teselas ortofoto.tif --resolucion 0.25
python orto_merge_tool.py teselas ortofoto.tif --nodata 0
```

## Parámetros del modo por argumentos

| Parámetro | Por defecto | Descripción |
|---|---|---|
| `entrada` | (obligatorio) | Carpeta con las teselas |
| `salida` | (obligatorio) | Ruta del GeoTIFF de salida (`.tif`) |
| `--patron` | `*.ecw` | Archivos a unir dentro de la carpeta |
| `--nivel` | `medio` | `alto`, `medio` o `bajo` (ver tabla siguiente) |
| `--factor` | según nivel | Factor de reducción de resolución (1 = sin reducir) |
| `--resolucion` | — | Tamaño de píxel final; reemplaza al factor |
| `--calidad` | según nivel | Calidad JPEG de 1 a 100 |
| `--remuestreo` | `average` | `average`, `bilinear`, `cubic`, `lanczos` o `nearest` |
| `--bloque` | `512` | Tamaño de bloque interno: 256, 512 o 1024 |
| `--sin-piramides` | — | No generar pirámides internas (no recomendado) |
| `--nodata` | — | Valor de píxel sin datos, que quedará transparente |
| `--cache-mb` | `1024` | Memoria de caché de GDAL en MB |
| `--sobrescribir` | — | Reemplaza la salida si ya existe |
| `--detalle` | — | Muestra mensajes de depuración |

### Niveles predefinidos

| Nivel | Factor | Calidad JPEG | Píxeles respecto al original |
|---|---|---|---|
| `alto` | 2 | 85 | 1/4 |
| `medio` | 4 | 75 | 1/16 |
| `bajo` | 8 | 65 | 1/64 |

`--factor`, `--calidad` y `--resolucion` reemplazan los valores del nivel.

## Qué hace el script

1. Busca las teselas y revisa que tengan el mismo sistema de coordenadas
   (desde cada `.prj`), el mismo número de bandas y 8 bits por banda.
2. Construye un mosaico virtual en memoria, sin copiar píxeles.
3. Escribe un único COG con la resolución reducida, compresión JPEG en
   YCbCr, bloques internos y pirámides.

## Desarrollo

El código sigue PEP 8. Para verificarlo:

```bash
pip install -r requirements-dev.txt
flake8 orto_merge_tool.py
```
