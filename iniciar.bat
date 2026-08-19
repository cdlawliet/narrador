@echo off
setlocal
cd /d "%~dp0"

echo.
echo Narrador - iniciando interface web...
echo.

if exist ".venv\Scripts\python.exe" goto :check_dependencies

where python >nul 2>&1
if not errorlevel 1 goto :create_with_python

where py >nul 2>&1
if not errorlevel 1 goto :create_with_py

goto :python_missing

:create_with_python
echo Criando ambiente virtual...
python -m venv .venv
if errorlevel 1 goto :error
goto :check_dependencies

:create_with_py
echo Criando ambiente virtual...
py -3 -m venv .venv
if errorlevel 1 goto :error
goto :check_dependencies

:check_dependencies
".venv\Scripts\python.exe" -c "import edge_tts, flask, gtts, pydub, srt" >nul 2>&1
if not errorlevel 1 goto :run

echo Instalando dependencias...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

:run
if "%NARRADOR_CHECK_ONLY%"=="1" goto :check_ok
echo O navegador sera aberto em http://127.0.0.1:5000
echo Para encerrar, volte a esta janela e pressione Ctrl+C.
echo.
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto :error
exit /b 0

:check_ok
echo Inicializador validado com sucesso.
exit /b 0

:python_missing
echo Python nao foi encontrado no PATH.
echo Instale o Python e tente novamente.
pause
exit /b 1

:error
echo.
echo Nao foi possivel iniciar o Narrador.
pause
exit /b 1
