@echo off
rem Ejecuta orto_merge_tool.py con el Python y GDAL de OSGeo4W.
rem Si OSGeo4W esta instalado en otra carpeta, cambia la ruta siguiente.
set "OSGEO4W_ROOT=C:\OSGeo4W"

if not exist "%OSGEO4W_ROOT%\bin\o4w_env.bat" (
    echo No se encontro OSGeo4W en %OSGEO4W_ROOT%.
    echo Instala OSGeo4W o corrige la ruta dentro de ejecutar.bat.
    pause
    exit /b 1
)

call "%OSGEO4W_ROOT%\bin\o4w_env.bat"
python "%~dp0orto_merge_tool.py" %*
