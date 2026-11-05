#!/usr/bin/env python3
"""Orto Merge Tool: une las teselas de una ortofoto en un GeoTIFF liviano.

El script toma una carpeta con teselas georreferenciadas (por ejemplo
ECW con su archivo .prj), las une en un mosaico virtual y escribe un
único Cloud Optimized GeoTIFF (COG) con:

* resolución reducida según un factor configurable,
* compresión JPEG con calidad configurable,
* bloques internos y pirámides (overviews) para carga rápida.

El resultado se abre directamente en Global Mapper.

Modo interactivo (pide los parámetros por consola)::

    python orto_merge_tool.py

Modo por argumentos (útil para automatizar)::

    python orto_merge_tool.py D:/ortofoto/teselas D:/salida/orto.tif
    python orto_merge_tool.py teselas orto.tif --factor 3 --calidad 70
"""

__title__ = "Orto Merge Tool"
__version__ = "1.0.0"

import argparse
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

try:
    from osgeo import gdal, osr
except ImportError:
    print(
        "No se encontró GDAL para Python. Ejecuta el script con "
        "ejecutar.bat o desde OSGeo4W Shell (ver README.md)."
    )
    input("Presiona Enter para cerrar...")
    sys.exit(1)

gdal.UseExceptions()
osr.UseExceptions()

LOGGER = logging.getLogger("orto_merge_tool")

# Niveles predefinidos: nombre -> (factor de reducción, calidad JPEG).
QUALITY_PRESETS: Dict[str, Tuple[float, int]] = {
    "alto": (2.0, 85),
    "medio": (4.0, 75),
    "bajo": (8.0, 65),
}
DEFAULT_PRESET = "medio"
CUSTOM_PRESET = "personalizado"
DEFAULT_PATTERN = "*.ecw"
DEFAULT_BLOCK_SIZE = 512
DEFAULT_CACHE_MB = 1024
RESAMPLING_METHODS = ("average", "bilinear", "cubic", "lanczos", "nearest")
BLOCK_SIZES = (256, 512, 1024)

# El driver COG existe desde GDAL 3.1.
MIN_GDAL_VERSION = 3010000

# Valores aproximados para estimar el tamaño de salida.
ESTIMATED_JPEG_RATIO = 10.0
OVERVIEW_OVERHEAD = 1.34
DISK_SAFETY_MARGIN = 2.0

PIXEL_SIZE_TOLERANCE = 1e-6
VRT_PATH = "/vsimem/mosaico_ortofoto.vrt"

# Atributos de Windows que indican archivos "solo en línea" (OneDrive).
WINDOWS_OFFLINE_ATTRIBUTES = 0x00001000 | 0x00400000


class OrthophotoError(Exception):
    """Error esperado durante el proceso, con mensaje para el usuario."""


@dataclass
class TileInfo:
    """Metadatos relevantes de una tesela."""

    path: Path
    srs: Optional[osr.SpatialReference]
    pixel_size: Tuple[float, float]
    band_count: int
    data_type: int
    width: int
    height: int


@dataclass
class MosaicSettings:
    """Parámetros finales con los que se escribe la salida."""

    resolution: Tuple[float, float]
    quality: int
    resampling: str
    block_size: int
    overviews: bool


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Lee y valida los argumentos de la línea de comandos."""
    parser = argparse.ArgumentParser(
        description=(
            f"{__title__}: unifica las teselas de una ortofoto en un "
            "único GeoTIFF comprimido y optimizado para carga rápida."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{__title__} {__version__}",
    )
    parser.add_argument(
        "entrada",
        type=Path,
        help="Carpeta que contiene las teselas.",
    )
    parser.add_argument(
        "salida",
        type=Path,
        help="Ruta del GeoTIFF de salida (.tif).",
    )
    parser.add_argument(
        "--patron",
        default=DEFAULT_PATTERN,
        help=f"Patrón de archivos a unir (por defecto {DEFAULT_PATTERN}).",
    )
    parser.add_argument(
        "--nivel",
        choices=sorted(QUALITY_PRESETS),
        default=DEFAULT_PRESET,
        help=(
            "Nivel predefinido de calidad: alto (factor 2, JPEG 85), "
            "medio (factor 4, JPEG 75) o bajo (factor 8, JPEG 65). "
            f"Por defecto {DEFAULT_PRESET}."
        ),
    )
    parser.add_argument(
        "--factor",
        type=_positive_factor,
        help=(
            "Factor de reducción de resolución. Reemplaza el del nivel. "
            "1 mantiene la resolución, 2 la reduce a la mitad."
        ),
    )
    parser.add_argument(
        "--resolucion",
        type=_positive_float,
        help=(
            "Tamaño de píxel final en unidades del sistema de coordenadas "
            "(normalmente metros). Reemplaza al factor."
        ),
    )
    parser.add_argument(
        "--calidad",
        type=_jpeg_quality,
        help="Calidad JPEG de 1 a 100. Reemplaza la del nivel.",
    )
    parser.add_argument(
        "--remuestreo",
        choices=RESAMPLING_METHODS,
        default="average",
        help="Método de remuestreo al reducir (por defecto average).",
    )
    parser.add_argument(
        "--bloque",
        type=int,
        choices=BLOCK_SIZES,
        default=DEFAULT_BLOCK_SIZE,
        help=f"Tamaño de bloque interno (por defecto {DEFAULT_BLOCK_SIZE}).",
    )
    parser.add_argument(
        "--sin-piramides",
        action="store_true",
        help="No generar pirámides internas (no recomendado).",
    )
    parser.add_argument(
        "--nodata",
        type=float,
        help=(
            "Valor de los píxeles sin datos en las teselas (por ejemplo 0 "
            "para bordes negros o 255 para bordes blancos). Esos píxeles "
            "quedan transparentes en la salida."
        ),
    )
    parser.add_argument(
        "--cache-mb",
        type=int,
        default=DEFAULT_CACHE_MB,
        help=(
            "Memoria de caché de GDAL en MB "
            f"(por defecto {DEFAULT_CACHE_MB})."
        ),
    )
    parser.add_argument(
        "--sobrescribir",
        action="store_true",
        help="Reemplaza el archivo de salida si ya existe.",
    )
    parser.add_argument(
        "--detalle",
        action="store_true",
        help="Muestra mensajes de depuración.",
    )
    return parser.parse_args(argv)


def _positive_float(value: str) -> float:
    """Convierte un argumento a número positivo."""
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("debe ser mayor que 0")
    return number


def _positive_factor(value: str) -> float:
    """Convierte un argumento a factor de reducción válido (>= 1)."""
    number = float(value)
    if number < 1:
        raise argparse.ArgumentTypeError("debe ser mayor o igual a 1")
    return number


def _jpeg_quality(value: str) -> int:
    """Convierte un argumento a calidad JPEG entre 1 y 100."""
    number = int(value)
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("debe estar entre 1 y 100")
    return number


def _ask(prompt: str, default: Optional[str] = None) -> str:
    """Pide un texto por consola; Enter devuelve el valor por defecto."""
    suffix = f" [{default}]" if default is not None else ""
    answer = input(f"{prompt}{suffix}: ").strip().strip('"').strip("'")
    if not answer and default is not None:
        return default
    return answer


def _ask_yes_no(prompt: str, default: bool) -> bool:
    """Pide una respuesta sí/no por consola."""
    hint = "S/n" if default else "s/N"
    while True:
        answer = input(f"{prompt} ({hint}): ").strip().lower()
        if not answer:
            return default
        if answer in ("s", "si", "sí", "y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Responde 's' o 'n'.")


def _ask_value(
    prompt: str,
    converter: Callable[[str], Any],
    default: Optional[str] = None,
    optional: bool = False,
) -> Any:
    """Pide un valor y lo convierte, repitiendo hasta que sea válido.

    Si ``optional`` es verdadero, Enter sin texto devuelve ``None``.
    """
    while True:
        answer = _ask(prompt, default)
        if not answer and optional:
            return None
        try:
            return converter(answer)
        except argparse.ArgumentTypeError as error:
            print(f"  Valor no válido: {error}.")
        except ValueError:
            print("  Valor no válido: ingresa un número.")


def _ask_choice(prompt: str, options: Sequence[str], default: str) -> str:
    """Pide elegir una opción de una lista."""
    while True:
        answer = _ask(f"{prompt} ({' / '.join(options)})", default).lower()
        if answer in options:
            return answer
        print(f"  Elige una de: {', '.join(options)}.")


def _ask_input_dir() -> Path:
    """Pide la carpeta de teselas hasta que exista."""
    while True:
        folder = Path(_ask("Carpeta de las teselas"))
        if str(folder) != "." and folder.is_dir():
            return folder
        print("  La carpeta no existe. Revisa la ruta.")


def _ask_output_path() -> Tuple[Path, bool]:
    """Pide la ruta de salida y si se puede sobrescribir."""
    while True:
        answer = _ask("Archivo de salida (por ejemplo D:\\salida\\orto.tif)")
        if not answer:
            continue
        output = Path(answer)
        if output.suffix.lower() not in (".tif", ".tiff"):
            output = output.with_suffix(".tif")
        if not output.exists():
            return output, False
        if _ask_yes_no(f"  {output.name} ya existe. ¿Reemplazarlo?", False):
            return output, True


def ask_parameters() -> argparse.Namespace:
    """Pide todos los parámetros por consola (modo interactivo)."""
    print("=" * 60)
    print(f" {__title__} v{__version__} - modo interactivo")
    print(" Presiona Enter para aceptar el valor entre corchetes.")
    print("=" * 60)

    input_dir = _ask_input_dir()
    pattern = _ask("Tipo de archivo a unir", DEFAULT_PATTERN)
    output, overwrite = _ask_output_path()

    print("\nNiveles de reducción:")
    for name, (factor, quality) in QUALITY_PRESETS.items():
        print(f"  {name:<6} factor {factor:g}, calidad JPEG {quality}")
    print("  personalizado  defines el factor o la resolución y la calidad")
    level = _ask_choice(
        "Nivel", [*QUALITY_PRESETS, CUSTOM_PRESET], DEFAULT_PRESET
    )

    factor = resolution = quality = None
    if level == CUSTOM_PRESET:
        resolution = _ask_value(
            "Tamaño de píxel final en metros (Enter para usar un factor)",
            _positive_float,
            optional=True,
        )
        if resolution is None:
            factor = _ask_value("Factor de reducción", _positive_factor, "4")
        quality = _ask_value("Calidad JPEG (1-100)", _jpeg_quality, "75")
        level = DEFAULT_PRESET

    nodata = _ask_value(
        "Valor sin datos en los bordes, ej. 0 negro o 255 blanco "
        "(Enter si no hay)",
        float,
        optional=True,
    )

    resampling = "average"
    block_size = DEFAULT_BLOCK_SIZE
    overviews = True
    cache_mb = DEFAULT_CACHE_MB
    if _ask_yes_no("¿Configurar opciones avanzadas?", False):
        resampling = _ask_choice(
            "Método de remuestreo", RESAMPLING_METHODS, resampling
        )
        block_size = int(_ask_choice(
            "Tamaño de bloque",
            [str(size) for size in BLOCK_SIZES],
            str(block_size),
        ))
        overviews = _ask_yes_no("¿Generar pirámides internas?", True)
        cache_mb = _ask_value("Caché de GDAL en MB", int, str(cache_mb))

    print()
    return argparse.Namespace(
        entrada=input_dir,
        salida=output,
        patron=pattern,
        nivel=level,
        factor=factor,
        resolucion=resolution,
        calidad=quality,
        remuestreo=resampling,
        bloque=block_size,
        sin_piramides=not overviews,
        nodata=nodata,
        cache_mb=cache_mb,
        sobrescribir=overwrite,
        detalle=False,
    )


def configure_logging(verbose: bool) -> None:
    """Configura el formato y nivel de los mensajes."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def check_gdal_environment(pattern: str) -> None:
    """Verifica la versión de GDAL y los drivers necesarios."""
    version = int(gdal.VersionInfo("VERSION_NUM"))
    if version < MIN_GDAL_VERSION:
        raise OrthophotoError(
            f"Se requiere GDAL 3.1 o superior; la versión instalada es "
            f"{gdal.__version__}."
        )
    if gdal.GetDriverByName("COG") is None:
        raise OrthophotoError("El driver COG de GDAL no está disponible.")
    needs_ecw = pattern.lower().endswith(".ecw")
    if needs_ecw and gdal.GetDriverByName("ECW") is None:
        raise OrthophotoError(
            "Esta instalación de GDAL no puede leer archivos ECW. "
            "Ejecuta el script desde OSGeo4W Shell (incluido con QGIS) "
            "o instala el paquete gdal-ecw desde el instalador de OSGeo4W."
        )
    LOGGER.info("%s v%s | GDAL %s", __title__, __version__,
                gdal.__version__)


def _natural_sort_key(path: Path) -> List[object]:
    """Ordena nombres con números de forma natural (2 antes que 10)."""
    parts = re.split(r"(\d+)", path.name.lower())
    return [int(part) if part.isdigit() else part for part in parts]


def find_tiles(input_dir: Path, pattern: str) -> List[Path]:
    """Busca las teselas en la carpeta de entrada."""
    if not input_dir.is_dir():
        raise OrthophotoError(f"La carpeta de entrada no existe: {input_dir}")
    tiles = sorted(input_dir.glob(pattern), key=_natural_sort_key)
    if not tiles:
        raise OrthophotoError(
            f"No se encontraron archivos '{pattern}' en {input_dir}"
        )
    LOGGER.info("Teselas encontradas: %d", len(tiles))
    return tiles


def warn_cloud_only_files(tiles: Sequence[Path]) -> None:
    """Advierte si hay archivos de OneDrive no descargados (Windows)."""
    cloud_only = []
    for tile in tiles:
        attributes = getattr(os.stat(tile), "st_file_attributes", 0)
        if attributes & WINDOWS_OFFLINE_ATTRIBUTES:
            cloud_only.append(tile.name)
    if cloud_only:
        LOGGER.warning(
            "%d teselas están solo en la nube (OneDrive). Se descargarán "
            "durante el proceso, lo que puede ser lento o fallar. Marca la "
            "carpeta como 'Mantener siempre en este dispositivo'.",
            len(cloud_only),
        )


def read_prj(tile: Path) -> Optional[osr.SpatialReference]:
    """Lee el sistema de coordenadas del archivo .prj de una tesela."""
    prj_path = tile.with_suffix(".prj")
    if not prj_path.is_file():
        return None
    srs = osr.SpatialReference()
    wkt = prj_path.read_text(encoding="utf-8", errors="ignore")
    srs.SetFromUserInput(wkt)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def inspect_tile(tile: Path) -> TileInfo:
    """Obtiene georreferencia y estructura de bandas de una tesela."""
    dataset = gdal.Open(str(tile))
    try:
        geotransform = dataset.GetGeoTransform()
        srs = read_prj(tile) or dataset.GetSpatialRef()
        band = dataset.GetRasterBand(1)
        return TileInfo(
            path=tile,
            srs=srs,
            pixel_size=(abs(geotransform[1]), abs(geotransform[5])),
            band_count=dataset.RasterCount,
            data_type=band.DataType,
            width=dataset.RasterXSize,
            height=dataset.RasterYSize,
        )
    finally:
        dataset = None


def validate_tiles(
    tiles: Sequence[TileInfo],
) -> Tuple[Optional[osr.SpatialReference], Tuple[float, float]]:
    """Comprueba que las teselas sean compatibles entre sí.

    Devuelve el sistema de coordenadas común y el tamaño de píxel más
    fino encontrado.
    """
    reference = tiles[0]

    if reference.srs is None:
        LOGGER.warning(
            "%s no tiene sistema de coordenadas (.prj). El resultado "
            "quedará sin proyección definida.",
            reference.path.name,
        )

    for tile in tiles[1:]:
        if tile.band_count != reference.band_count:
            raise OrthophotoError(
                f"{tile.path.name} tiene {tile.band_count} bandas y "
                f"{reference.path.name} tiene {reference.band_count}."
            )
        if (
            reference.srs is not None
            and tile.srs is not None
            and not tile.srs.IsSame(reference.srs)
        ):
            raise OrthophotoError(
                f"{tile.path.name} tiene un sistema de coordenadas distinto "
                f"al de {reference.path.name}."
            )

    if reference.band_count not in (1, 3):
        raise OrthophotoError(
            f"Las teselas tienen {reference.band_count} bandas; la "
            "compresión JPEG requiere 1 (gris) o 3 (RGB)."
        )
    if reference.data_type != gdal.GDT_Byte:
        type_name = gdal.GetDataTypeName(reference.data_type)
        raise OrthophotoError(
            f"Las teselas son de tipo {type_name}; la compresión JPEG "
            "requiere 8 bits (Byte)."
        )

    sizes_x = {round(tile.pixel_size[0], 9) for tile in tiles}
    sizes_y = {round(tile.pixel_size[1], 9) for tile in tiles}
    finest = (min(sizes_x), min(sizes_y))
    if (
        max(sizes_x) - finest[0] > PIXEL_SIZE_TOLERANCE * finest[0]
        or max(sizes_y) - finest[1] > PIXEL_SIZE_TOLERANCE * finest[1]
    ):
        LOGGER.warning(
            "Las teselas tienen tamaños de píxel distintos; se usará el "
            "más fino (%g x %g) como base.",
            *finest,
        )

    srs_name = reference.srs.GetName() if reference.srs else "sin definir"
    LOGGER.info("Sistema de coordenadas: %s", srs_name)
    LOGGER.info("Tamaño de píxel original: %g x %g", *finest)
    return reference.srs, finest


def resolve_settings(
    args: argparse.Namespace,
    source_resolution: Tuple[float, float],
) -> MosaicSettings:
    """Combina nivel, factor, resolución y calidad en valores finales."""
    preset_factor, preset_quality = QUALITY_PRESETS[args.nivel]
    quality = args.calidad if args.calidad is not None else preset_quality

    if args.resolucion is not None:
        resolution = (args.resolucion, args.resolucion)
    else:
        factor = args.factor if args.factor is not None else preset_factor
        resolution = (
            source_resolution[0] * factor,
            source_resolution[1] * factor,
        )

    LOGGER.info(
        "Tamaño de píxel de salida: %g x %g | calidad JPEG: %d",
        resolution[0],
        resolution[1],
        quality,
    )
    return MosaicSettings(
        resolution=resolution,
        quality=quality,
        resampling=args.remuestreo,
        block_size=args.bloque,
        overviews=not args.sin_piramides,
    )


def build_mosaic(
    tiles: Sequence[Path],
    srs: Optional[osr.SpatialReference],
    nodata: Optional[float],
) -> gdal.Dataset:
    """Crea un mosaico virtual (VRT en memoria) con todas las teselas."""
    options = gdal.BuildVRTOptions(
        resolution="highest",
        outputSRS=srs.ExportToWkt() if srs is not None else None,
        allowProjectionDifference=True,
        srcNodata=nodata,
        addAlpha=nodata is not None,
    )
    mosaic = gdal.BuildVRT(
        VRT_PATH, [str(tile) for tile in tiles], options=options
    )
    if mosaic is None:
        raise OrthophotoError("No fue posible construir el mosaico virtual.")
    LOGGER.info(
        "Mosaico virtual: %d x %d píxeles",
        mosaic.RasterXSize,
        mosaic.RasterYSize,
    )
    return mosaic


def estimate_output_bytes(
    mosaic: gdal.Dataset,
    source_resolution: Tuple[float, float],
    settings: MosaicSettings,
) -> float:
    """Estima el tamaño aproximado del archivo de salida en bytes."""
    scale_x = source_resolution[0] / settings.resolution[0]
    scale_y = source_resolution[1] / settings.resolution[1]
    pixels = mosaic.RasterXSize * scale_x * mosaic.RasterYSize * scale_y
    color_bands = min(mosaic.RasterCount, 3)
    raw_bytes = pixels * color_bands
    overhead = OVERVIEW_OVERHEAD if settings.overviews else 1.0
    return raw_bytes / ESTIMATED_JPEG_RATIO * overhead


def format_size(size_bytes: float) -> str:
    """Devuelve un tamaño en bytes como texto legible (MB o GB)."""
    if size_bytes >= 1e9:
        return f"{size_bytes / 1e9:.2f} GB"
    return f"{size_bytes / 1e6:.1f} MB"


def check_disk_space(output: Path, estimated_bytes: float) -> None:
    """Advierte si el disco de salida podría no tener espacio suficiente."""
    free_bytes = shutil.disk_usage(output.parent).free
    LOGGER.info(
        "Tamaño estimado de salida: %s (espacio libre: %s)",
        format_size(estimated_bytes),
        format_size(free_bytes),
    )
    if free_bytes < estimated_bytes * DISK_SAFETY_MARGIN:
        LOGGER.warning(
            "El espacio libre podría ser insuficiente. El proceso usa "
            "archivos temporales junto a la salida."
        )


def prepare_output(output: Path, overwrite: bool) -> None:
    """Valida la ruta de salida y elimina un archivo previo si se pide."""
    if output.suffix.lower() not in (".tif", ".tiff"):
        raise OrthophotoError("La salida debe tener extensión .tif o .tiff.")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        if not overwrite:
            raise OrthophotoError(
                f"{output} ya existe. Usa --sobrescribir para reemplazarlo."
            )
        gdal.GetDriverByName("GTiff").Delete(str(output))


def write_output(
    mosaic: gdal.Dataset,
    output: Path,
    settings: MosaicSettings,
) -> None:
    """Escribe el mosaico como Cloud Optimized GeoTIFF con JPEG."""
    creation_options = [
        "COMPRESS=JPEG",
        f"QUALITY={settings.quality}",
        f"BLOCKSIZE={settings.block_size}",
        f"RESAMPLING={settings.resampling.upper()}",
        f"OVERVIEWS={'AUTO' if settings.overviews else 'NONE'}",
        "BIGTIFF=IF_SAFER",
        "NUM_THREADS=ALL_CPUS",
    ]
    options = gdal.TranslateOptions(
        format="COG",
        xRes=settings.resolution[0],
        yRes=settings.resolution[1],
        resampleAlg=settings.resampling,
        creationOptions=creation_options,
        callback=gdal.TermProgress_nocb,
    )
    LOGGER.info("Escribiendo %s ...", output)
    result = gdal.Translate(str(output), mosaic, options=options)
    if result is None:
        raise OrthophotoError("GDAL no pudo escribir el archivo de salida.")
    result = None


def run(args: argparse.Namespace, confirm: bool = False) -> None:
    """Ejecuta el proceso completo de unificación.

    Si ``confirm`` es verdadero, pide confirmación después de mostrar
    el tamaño estimado y antes de escribir la salida.
    """
    gdal.SetCacheMax(args.cache_mb * 1024 * 1024)
    check_gdal_environment(args.patron)

    tile_paths = find_tiles(args.entrada, args.patron)
    warn_cloud_only_files(tile_paths)

    LOGGER.info("Leyendo metadatos de las teselas ...")
    tile_infos = [inspect_tile(path) for path in tile_paths]
    srs, source_resolution = validate_tiles(tile_infos)

    settings = resolve_settings(args, source_resolution)
    prepare_output(args.salida, args.sobrescribir)

    mosaic = build_mosaic(tile_paths, srs, args.nodata)
    try:
        estimated = estimate_output_bytes(mosaic, source_resolution, settings)
        check_disk_space(args.salida, estimated)
        if confirm and not _ask_yes_no("¿Iniciar el proceso?", True):
            raise OrthophotoError("Proceso cancelado por el usuario.")
        write_output(mosaic, args.salida, settings)
    finally:
        mosaic = None
        gdal.Unlink(VRT_PATH)

    final_size = format_size(args.salida.stat().st_size)
    LOGGER.info("Listo: %s (%s)", args.salida, final_size)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Punto de entrada del script. Devuelve el código de salida.

    Sin argumentos se usa el modo interactivo; con argumentos, el modo
    de línea de comandos.
    """
    arguments = sys.argv[1:] if argv is None else list(argv)
    interactive = not arguments
    exit_code = 0
    try:
        args = ask_parameters() if interactive else parse_args(arguments)
        configure_logging(args.detalle)
        run(args, confirm=interactive)
    except KeyboardInterrupt:
        print("\nProceso cancelado.")
        exit_code = 1
    except OrthophotoError as error:
        LOGGER.error("%s", error)
        exit_code = 1
    except RuntimeError as error:
        LOGGER.error("Error de GDAL: %s", error)
        exit_code = 1
    if interactive:
        input("\nPresiona Enter para cerrar...")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
