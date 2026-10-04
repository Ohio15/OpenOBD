@echo off
REM Build the J2534 logging proxy DLL with MSVC (Developer Command Prompt).
REM Build x86 if the factory tool is 32-bit (usual), x64 if it is 64-bit.
REM
REM Open "x86 Native Tools Command Prompt for VS" (or x64) and run this.
cl /nologo /LD /O2 j2534_proxy.c /Fej2534_proxy.dll /link /DEF:j2534_proxy.def
if %errorlevel%==0 (echo built j2534_proxy.dll) else (echo BUILD FAILED)
