@echo off
rem Ejecuta orto_merge_tool.py con el Python y GDAL de OSGeo4W.
rem Busca OSGeo4W en las carpetas de instalacion habituales.
rem Si esta en otra carpeta, escribe la ruta despues del signo = en
rem la linea siguiente, por ejemplo: set "OSGEO4W_ROOT=D:\OSGeo4W"
set "OSGEO4W_ROOT="

for %%D in ("C:\OSGeo4W" "%LOCALAPPDATA%\Programs\OSGeo4W" "%ProgramFiles%\OSGeo4W") do (
    if not defined OSGEO4W_ROOT if exist "%%~D\bin\o4w_env.bat" set "OSGEO4W_ROOT=%%~D"
)

if not defined OSGEO4W_ROOT (
    echo No se encontro OSGeo4W en las carpetas habituales.
    echo Edita ejecutar.bat y escribe la ruta de instalacion en OSGEO4W_ROOT.
    pause
    exit /b 1
)

echo Usando OSGeo4W en: %OSGEO4W_ROOT%
call "%OSGEO4W_ROOT%\bin\o4w_env.bat"

rem En instalaciones fuera de C:\OSGeo4W, GDAL no encuentra solo sus
rem complementos (entre ellos el de ECW). Se le indica la carpeta.
if not defined GDAL_DRIVER_PATH if exist "%OSGEO4W_ROOT%\apps\gdal\lib\gdalplugins" set "GDAL_DRIVER_PATH=%OSGEO4W_ROOT%\apps\gdal\lib\gdalplugins"

python "%~dp0orto_merge_tool.py" %*
