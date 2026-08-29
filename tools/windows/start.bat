@echo off
rem Тонер-фарм — запуск одной кнопкой.
rem Работает с любым Python 3.8+: если venv отсутствует или сломан
rem (например, папку проекта перенесли с машины с другой версией Python),
rem окружение пересоздаётся автоматически.
rem Переходим в корень проекта (этот файл лежит в tools/windows).
cd /d "%~dp0\..\.."

if not exist ".venv\Scripts\python.exe" goto create
".venv\Scripts\python.exe" --version >nul 2>&1
if errorlevel 1 goto recreate
goto run

:recreate
echo Venv создан под другую версию Python — пересоздаю...
rmdir /s /q .venv

:create
echo Создаю виртуальное окружение...
python -m venv .venv
if errorlevel 1 (
    echo.
    echo ОШИБКА: python не найден. Установите Python 3.8+ с python.org
    echo и при установке отметьте галочку "Add python.exe to PATH".
    pause
    exit /b 1
)
echo Ставлю зависимости...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo ОШИБКА: не удалось установить зависимости.
    pause
    exit /b 1
)

:run
".venv\Scripts\python.exe" app.py
pause
