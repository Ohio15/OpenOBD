/*
 * j2534_proxy.c -- a logging pass-through J2534 (v04.04) DLL.
 *
 * WHAT IT IS. A drop-in J2534 DLL that a GM factory tool (Techline Connect /
 * GDS2 / SPS) loads INSTEAD of the real OBDX Pro GT J2534 DLL. Every call is
 * forwarded verbatim to the real DLL; PassThruWriteMsgs / PassThruReadMsgs
 * additionally append each CAN frame to a trace file. The factory tool and the
 * truck behave exactly as they would without the proxy -- it is a read-only tap
 * on the pass-thru API, nothing more.
 *
 * WHY. The OBDX Pro GT speaks ELM327 over USB-CDC for OpenOBD's own reads, but
 * the factory tool drives it through J2534. Capturing that session is the only
 * way to learn which mode-22 DIDs the factory reads and how they scale -- the
 * one open item on OpenOBD's gauges (gt.DID_TABLE is empty on purpose). The
 * trace this proxy writes is decoded offline by tools/j2534_decode.py into a
 * candidate DID table.
 *
 * FENCE (DR-011). This file only forwards and logs. It originates no traffic of
 * its own, synthesises no security-access or flash sequence, and the offline
 * decoder deliberately refuses to turn the captured programming bytes into a
 * runnable write path. Ron registers and runs this; the capture is his action.
 *
 * CONFIG (no recompile needed to point it at the real DLL or move the log):
 *   env OBDX_REAL_J2534  full path to the REAL OBDX Pro GT J2534 DLL.
 *   env OBDX_TRACE_LOG   full path for the JSONL trace (default:
 *                        %TEMP%\obdx_j2534_trace.jsonl).
 * If OBDX_REAL_J2534 is unset, the proxy reads j2534_proxy.ini next to itself:
 *   real_dll=C:\path\to\real_OBDXProGT_J2534.dll
 *   trace_log=C:\path\to\trace.jsonl
 *
 * TRACE FORMAT (one JSON object per line; openobd.j2534log.parse_jsonl reads it):
 *   {"ts": <ms since load>, "dir": "tx"|"rx", "id": <can id int>, "data": "<hex>"}
 * For ISO15765/CAN the first 4 Data bytes are the arbitration id (big-endian)
 * and the remainder is the CAN data field, per J2534.
 *
 * BUILD: see build.sh (mingw cross-compile) / build.bat (MSVC) and README.md.
 */

#include <windows.h>
#include <stdio.h>
#include <stdint.h>
#include <time.h>

/* ---- J2534 v04.04 message struct (must match the spec exactly) ---------- */
typedef struct {
    unsigned long ProtocolID;
    unsigned long RxStatus;
    unsigned long TxFlags;
    unsigned long Timestamp;
    unsigned long DataSize;
    unsigned long ExtraDataIndex;
    unsigned char Data[4128];
} PASSTHRU_MSG;

/* ---- real-DLL function pointer typedefs --------------------------------- */
typedef long (WINAPI *pfOpen)(void*, unsigned long*);
typedef long (WINAPI *pfClose)(unsigned long);
typedef long (WINAPI *pfConnect)(unsigned long, unsigned long, unsigned long, unsigned long, unsigned long*);
typedef long (WINAPI *pfDisconnect)(unsigned long);
typedef long (WINAPI *pfReadMsgs)(unsigned long, PASSTHRU_MSG*, unsigned long*, unsigned long);
typedef long (WINAPI *pfWriteMsgs)(unsigned long, PASSTHRU_MSG*, unsigned long*, unsigned long);
typedef long (WINAPI *pfStartPeriodic)(unsigned long, PASSTHRU_MSG*, unsigned long*, unsigned long);
typedef long (WINAPI *pfStopPeriodic)(unsigned long, unsigned long);
typedef long (WINAPI *pfStartFilter)(unsigned long, unsigned long, PASSTHRU_MSG*, PASSTHRU_MSG*, PASSTHRU_MSG*, unsigned long*);
typedef long (WINAPI *pfStopFilter)(unsigned long, unsigned long);
typedef long (WINAPI *pfSetVoltage)(unsigned long, unsigned long, unsigned long);
typedef long (WINAPI *pfReadVersion)(unsigned long, char*, char*, char*);
typedef long (WINAPI *pfGetLastError)(char*);
typedef long (WINAPI *pfIoctl)(unsigned long, unsigned long, void*, void*);

static HMODULE g_real = NULL;
static FILE*   g_log  = NULL;
static CRITICAL_SECTION g_lock;
static LARGE_INTEGER g_freq, g_start;
static int g_init_done = 0;

static pfOpen         r_Open;
static pfClose        r_Close;
static pfConnect      r_Connect;
static pfDisconnect   r_Disconnect;
static pfReadMsgs     r_ReadMsgs;
static pfWriteMsgs    r_WriteMsgs;
static pfStartPeriodic r_StartPeriodic;
static pfStopPeriodic  r_StopPeriodic;
static pfStartFilter  r_StartFilter;
static pfStopFilter   r_StopFilter;
static pfSetVoltage   r_SetVoltage;
static pfReadVersion  r_ReadVersion;
static pfGetLastError r_GetLastError;
static pfIoctl        r_Ioctl;

#define ERR_DEVICE_NOT_CONNECTED 0x08

/* read a key from j2534_proxy.ini next to this DLL into buf */
static int ini_lookup(const char* key, char* buf, DWORD buflen) {
    char path[MAX_PATH];
    HMODULE self = NULL;
    GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                       GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       (LPCSTR)&ini_lookup, &self);
    GetModuleFileNameA(self, path, MAX_PATH);
    char* slash = strrchr(path, '\\');
    if (slash) slash[1] = 0;
    strncat(path, "j2534_proxy.ini", MAX_PATH - strlen(path) - 1);
    FILE* f = fopen(path, "r");
    if (!f) return 0;
    char line[1024];
    int found = 0;
    size_t klen = strlen(key);
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, key, klen) == 0 && line[klen] == '=') {
            char* v = line + klen + 1;
            char* nl = strpbrk(v, "\r\n");
            if (nl) *nl = 0;
            strncpy(buf, v, buflen - 1);
            buf[buflen - 1] = 0;
            found = 1;
            break;
        }
    }
    fclose(f);
    return found;
}

static void resolve(const char* name, FARPROC* slot) {
    *slot = GetProcAddress(g_real, name);
}

static void proxy_init(void) {
    EnterCriticalSection(&g_lock);
    if (g_init_done) { LeaveCriticalSection(&g_lock); return; }

    char real_path[MAX_PATH] = {0};
    char log_path[MAX_PATH]  = {0};
    DWORD n = GetEnvironmentVariableA("OBDX_REAL_J2534", real_path, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) ini_lookup("real_dll", real_path, MAX_PATH);
    n = GetEnvironmentVariableA("OBDX_TRACE_LOG", log_path, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) {
        if (!ini_lookup("trace_log", log_path, MAX_PATH)) {
            char tmp[MAX_PATH];
            GetTempPathA(MAX_PATH, tmp);
            snprintf(log_path, MAX_PATH, "%sobdx_j2534_trace.jsonl", tmp);
        }
    }

    if (real_path[0]) g_real = LoadLibraryA(real_path);
    if (g_real) {
        resolve("PassThruOpen",               (FARPROC*)&r_Open);
        resolve("PassThruClose",              (FARPROC*)&r_Close);
        resolve("PassThruConnect",            (FARPROC*)&r_Connect);
        resolve("PassThruDisconnect",         (FARPROC*)&r_Disconnect);
        resolve("PassThruReadMsgs",           (FARPROC*)&r_ReadMsgs);
        resolve("PassThruWriteMsgs",          (FARPROC*)&r_WriteMsgs);
        resolve("PassThruStartPeriodicMsg",   (FARPROC*)&r_StartPeriodic);
        resolve("PassThruStopPeriodicMsg",    (FARPROC*)&r_StopPeriodic);
        resolve("PassThruStartMsgFilter",     (FARPROC*)&r_StartFilter);
        resolve("PassThruStopMsgFilter",      (FARPROC*)&r_StopFilter);
        resolve("PassThruSetProgrammingVoltage",(FARPROC*)&r_SetVoltage);
        resolve("PassThruReadVersion",        (FARPROC*)&r_ReadVersion);
        resolve("PassThruGetLastError",       (FARPROC*)&r_GetLastError);
        resolve("PassThruIoctl",              (FARPROC*)&r_Ioctl);
    }
    g_log = fopen(log_path, "a");
    QueryPerformanceFrequency(&g_freq);
    QueryPerformanceCounter(&g_start);
    g_init_done = 1;
    LeaveCriticalSection(&g_lock);
}

static double now_ms(void) {
    LARGE_INTEGER c; QueryPerformanceCounter(&c);
    return (double)(c.QuadPart - g_start.QuadPart) * 1000.0 / (double)g_freq.QuadPart;
}

/* log one PASSTHRU_MSG as a frame. For ISO15765/CAN the first 4 Data bytes are
 * the arbitration id (big-endian); the rest is the CAN data field. */
static void log_msg(const char* dir, const PASSTHRU_MSG* m) {
    if (!g_log || m->DataSize < 4) return;
    unsigned long id = ((unsigned long)m->Data[0] << 24) |
                       ((unsigned long)m->Data[1] << 16) |
                       ((unsigned long)m->Data[2] << 8)  |
                       ((unsigned long)m->Data[3]);
    EnterCriticalSection(&g_lock);
    fprintf(g_log, "{\"ts\": %.3f, \"dir\": \"%s\", \"id\": %lu, \"data\": \"",
            now_ms(), dir, id);
    for (unsigned long i = 4; i < m->DataSize; i++)
        fprintf(g_log, "%02X", m->Data[i]);
    fprintf(g_log, "\"}\n");
    fflush(g_log);
    LeaveCriticalSection(&g_lock);
}

/* ---- exported J2534 entry points ---------------------------------------- */
__declspec(dllexport) long WINAPI PassThruOpen(void* name, unsigned long* devid) {
    proxy_init();
    if (!r_Open) return ERR_DEVICE_NOT_CONNECTED;
    return r_Open(name, devid);
}
__declspec(dllexport) long WINAPI PassThruClose(unsigned long devid) {
    if (!r_Close) return ERR_DEVICE_NOT_CONNECTED;
    return r_Close(devid);
}
__declspec(dllexport) long WINAPI PassThruConnect(unsigned long devid, unsigned long proto,
        unsigned long flags, unsigned long baud, unsigned long* chid) {
    if (!r_Connect) return ERR_DEVICE_NOT_CONNECTED;
    return r_Connect(devid, proto, flags, baud, chid);
}
__declspec(dllexport) long WINAPI PassThruDisconnect(unsigned long chid) {
    if (!r_Disconnect) return ERR_DEVICE_NOT_CONNECTED;
    return r_Disconnect(chid);
}
__declspec(dllexport) long WINAPI PassThruReadMsgs(unsigned long chid, PASSTHRU_MSG* msgs,
        unsigned long* num, unsigned long timeout) {
    if (!r_ReadMsgs) return ERR_DEVICE_NOT_CONNECTED;
    long rc = r_ReadMsgs(chid, msgs, num, timeout);
    if (rc == 0 && msgs && num)
        for (unsigned long i = 0; i < *num; i++) log_msg("rx", &msgs[i]);
    return rc;
}
__declspec(dllexport) long WINAPI PassThruWriteMsgs(unsigned long chid, PASSTHRU_MSG* msgs,
        unsigned long* num, unsigned long timeout) {
    if (!r_WriteMsgs) return ERR_DEVICE_NOT_CONNECTED;
    if (msgs && num)
        for (unsigned long i = 0; i < *num; i++) log_msg("tx", &msgs[i]);
    return r_WriteMsgs(chid, msgs, num, timeout);
}
__declspec(dllexport) long WINAPI PassThruStartPeriodicMsg(unsigned long chid, PASSTHRU_MSG* msg,
        unsigned long* mid, unsigned long interval) {
    if (!r_StartPeriodic) return ERR_DEVICE_NOT_CONNECTED;
    if (msg) log_msg("tx", msg);
    return r_StartPeriodic(chid, msg, mid, interval);
}
__declspec(dllexport) long WINAPI PassThruStopPeriodicMsg(unsigned long chid, unsigned long mid) {
    if (!r_StopPeriodic) return ERR_DEVICE_NOT_CONNECTED;
    return r_StopPeriodic(chid, mid);
}
__declspec(dllexport) long WINAPI PassThruStartMsgFilter(unsigned long chid, unsigned long type,
        PASSTHRU_MSG* mask, PASSTHRU_MSG* pattern, PASSTHRU_MSG* flow, unsigned long* fid) {
    if (!r_StartFilter) return ERR_DEVICE_NOT_CONNECTED;
    return r_StartFilter(chid, type, mask, pattern, flow, fid);
}
__declspec(dllexport) long WINAPI PassThruStopMsgFilter(unsigned long chid, unsigned long fid) {
    if (!r_StopFilter) return ERR_DEVICE_NOT_CONNECTED;
    return r_StopFilter(chid, fid);
}
__declspec(dllexport) long WINAPI PassThruSetProgrammingVoltage(unsigned long devid,
        unsigned long pin, unsigned long voltage) {
    if (!r_SetVoltage) return ERR_DEVICE_NOT_CONNECTED;
    return r_SetVoltage(devid, pin, voltage);
}
__declspec(dllexport) long WINAPI PassThruReadVersion(unsigned long devid,
        char* fw, char* dll, char* api) {
    if (!r_ReadVersion) return ERR_DEVICE_NOT_CONNECTED;
    return r_ReadVersion(devid, fw, dll, api);
}
__declspec(dllexport) long WINAPI PassThruGetLastError(char* msg) {
    if (!r_GetLastError) return ERR_DEVICE_NOT_CONNECTED;
    return r_GetLastError(msg);
}
__declspec(dllexport) long WINAPI PassThruIoctl(unsigned long handle, unsigned long id,
        void* in, void* out) {
    if (!r_Ioctl) return ERR_DEVICE_NOT_CONNECTED;
    return r_Ioctl(handle, id, in, out);
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID reserved) {
    (void)h; (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        InitializeCriticalSection(&g_lock);
    } else if (reason == DLL_PROCESS_DETACH) {
        if (g_log) { fclose(g_log); g_log = NULL; }
        if (g_real) { FreeLibrary(g_real); g_real = NULL; }
        DeleteCriticalSection(&g_lock);
    }
    return TRUE;
}
