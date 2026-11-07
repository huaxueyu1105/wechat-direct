#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
#include "bridge_target.h"

static HMODULE own_module;
static volatile LONG busy, callback_count;
static unsigned char *last_object;
static char last_job[33];
static uintptr_t native_base;
static struct { uintptr_t vtable, cache; char name[32]; } callback_type;

typedef struct { uintptr_t *vtable; } Function;
static Function * __fastcall copy_function(Function *self, void *destination) {
    Function *result = destination ? destination : HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, 64);
    if (result) result->vtable = self->vtable;
    return result;
}
static uintptr_t __fastcall callback(Function *self, ...) {
    (void)self; InterlockedIncrement(&callback_count); return 0;
}
static void * __fastcall target_type(Function *self) { (void)self; return &callback_type; }
static void __fastcall delete_function(Function *self, unsigned char deallocate) {
    if (deallocate) HeapFree(GetProcessHeap(), 0, self);
}
static void * __fastcall get_target(Function *self) { return (unsigned char *)self + 8; }
static uintptr_t callback_vtable[] = {(uintptr_t)copy_function, (uintptr_t)copy_function,
    (uintptr_t)callback, (uintptr_t)target_type, (uintptr_t)delete_function, (uintptr_t)get_target};

static BOOL local_path(wchar_t *path, const wchar_t *name) {
    if (!GetModuleFileNameW(own_module, path, 32768)) return FALSE;
    wchar_t *end = wcsrchr(path, L'\\'); if (!end) return FALSE;
    wcscpy(end + 1, name); return TRUE;
}
static void write_report(const char *body) {
    wchar_t path[32768], temp[32768];
    if (!local_path(path, L"bridge-result.json") || !local_path(temp, L"bridge-result.tmp")) return;
    HANDLE file = CreateFileW(temp, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) return;
    DWORD count; WriteFile(file, body, (DWORD)strlen(body), &count, NULL);
    FlushFileBuffers(file); CloseHandle(file);
    MoveFileExW(temp, path, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH);
}
static void *allocate(size_t n) { return HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, n); }
static BOOL set_string(unsigned char *where, const char *text, size_t n) {
    memset(where, 0, 32);
    *(uint64_t *)(where + 16) = n;
    if (n < 16) { memcpy(where, text, n); *(uint64_t *)(where + 24) = 15; return TRUE; }
    size_t capacity = ((n + 16) & ~(size_t)15) - 1;
    char *buffer = allocate(capacity + 1); if (!buffer) return FALSE;
    memcpy(buffer, text, n); *(void **)where = buffer;
    *(uint64_t *)(where + 24) = capacity; return TRUE;
}
static BOOL set_wstring(unsigned char *where, const wchar_t *text, size_t n) {
    memset(where, 0, 32);
    *(uint64_t *)(where + 16) = n;
    if (n < 8) { memcpy(where, text, n * sizeof(wchar_t)); *(uint64_t *)(where + 24) = 7; return TRUE; }
    size_t capacity = ((n + 8) & ~(size_t)7) - 1;
    wchar_t *buffer = allocate((capacity + 1) * sizeof(wchar_t)); if (!buffer) return FALSE;
    memcpy(buffer, text, n * sizeof(wchar_t)); *(void **)where = buffer;
    *(uint64_t *)(where + 24) = capacity; return TRUE;
}
static void get_uuid(unsigned char *object, char *output) {
    unsigned char *s = object + 0x6f8;
    uint64_t length = *(uint64_t *)(s + 16), capacity = *(uint64_t *)(s + 24);
    if (length > 64) { output[0]=0; return; }
    char *data = capacity < 16 ? (char *)s : *(char **)s;
    if (!data) { output[0]=0; return; }
    for (uint64_t i=0;i<length;i++) {
        char c=data[i];
        if (!((c>='0'&&c<='9')||(c>='a'&&c<='f')||(c>='A'&&c<='F')||c=='-')) {output[0]=0;return;}
        output[i]=c;
    }
    output[length]=0;
}

#pragma pack(push, 1)
typedef struct { char magic[8]; uint32_t action, recipient_length, text_length; char job[32]; } Request;
#pragma pack(pop)

__declspec(dllexport) DWORD WINAPI ExecuteJob(void *unused) {
    (void)unused;
    if (InterlockedCompareExchange(&busy,1,0)) return 10;
    const char *stage="invalid_request"; DWORD exception=0; BOOL submitted=FALSE; uint64_t request_id=0;
    char result[1800], job[33]={0}, uuid[80]={0};
    unsigned char *payload=NULL, *block=NULL, *context=NULL;
    BOOL sticker=FALSE, will_send=FALSE; wchar_t media_path[16384]; int path_length=0; uint64_t file_size=0;
    Request request={0}; wchar_t path[32768]; DWORD bytes=0;
    if (!local_path(path,L"bridge-request.bin")) goto finish;
    HANDLE input=CreateFileW(path,GENERIC_READ,FILE_SHARE_READ,NULL,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,NULL);
    if (input==INVALID_HANDLE_VALUE) goto finish;
    DWORD size=GetFileSize(input,NULL);
    if (size<sizeof(Request)||size>sizeof(Request)+256+16000) {CloseHandle(input);goto finish;}
    payload=allocate(size+1);
    if (!payload||!ReadFile(input,payload,size,&bytes,NULL)||bytes!=size) {CloseHandle(input);goto finish;}
    CloseHandle(input); memcpy(&request,payload,sizeof(Request)); memcpy(job,request.job,32);
    if (memcmp(request.magic,"WXDR01\0\0",8)||request.recipient_length==0||request.recipient_length>256||
        request.text_length==0||request.text_length>16000||sizeof(Request)+request.recipient_length+request.text_length!=size) goto finish;
    for (int i=0;i<32;i++) if (!((job[i]>='0'&&job[i]<='9')||(job[i]>='a'&&job[i]<='f'))) goto finish;
    unsigned char *core=(unsigned char *)GetModuleHandleW(L"Weixin.dll");
    native_base=(uintptr_t)core;
    stage="core_validation_failed";
    if (!core||memcmp(core+BRIDGE_CONSTRUCTOR_RVA,BRIDGE_CONSTRUCTOR_PREFIX,sizeof(BRIDGE_CONSTRUCTOR_PREFIX))||
        memcmp(core+BRIDGE_CONTEXT_RVA,BRIDGE_CONTEXT_PREFIX,sizeof(BRIDGE_CONTEXT_PREFIX))||
        memcmp(core+BRIDGE_SEND_RVA,BRIDGE_SEND_PREFIX,sizeof(BRIDGE_SEND_PREFIX))||
        *(uintptr_t *)(core+BRIDGE_VECTOR_VTABLE+16)!=(uintptr_t)(core+BRIDGE_SEND_RVA)||
        *(uint64_t *)(core+BRIDGE_REQUEST_COUNTER)==0||
        *(uint64_t *)(core+BRIDGE_REQUEST_COUNTER)>0x7ffffffffffffffeull) goto finish;
    callback_type.vtable=*(uintptr_t *)(core+BRIDGE_TYPE_DESCRIPTOR);
    strcpy(callback_type.name,".?AVCodexNoop@@");
    if (request.action==3) {
        stage="inspection";
        if (last_object&&strcmp(last_job,job)==0) get_uuid(last_object,uuid);
        goto finish;
    }
    sticker=request.action==4||request.action==5;
    will_send=request.action==2||request.action==5;
    if (request.action!=1&&request.action!=2&&!sticker) {stage="invalid_action";goto finish;}
    if (sticker) {
        stage="sticker_core_validation_failed";
        if (memcmp(core+BRIDGE_IMAGE_CONSTRUCTOR_RVA,BRIDGE_IMAGE_CONSTRUCTOR_PREFIX,sizeof(BRIDGE_IMAGE_CONSTRUCTOR_PREFIX))) goto finish;
        const char *raw=(char *)payload+sizeof(Request)+request.recipient_length;
        path_length=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,raw,(int)request.text_length,media_path,16383);
        if (!path_length) {stage="invalid_sticker_path";goto finish;}
        media_path[path_length]=0;
        for (int i=0;i<path_length;i++) if (!media_path[i]) {stage="invalid_sticker_path";goto finish;}
        HANDLE media=CreateFileW(media_path,GENERIC_READ,FILE_SHARE_READ,NULL,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,NULL);
        if (media==INVALID_HANDLE_VALUE) {stage="sticker_file_unavailable";goto finish;}
        LARGE_INTEGER length; unsigned char header[6]; DWORD read=0;
        BOOL valid=GetFileSizeEx(media,&length)&&length.QuadPart>0&&length.QuadPart<=10*1024*1024&&
            ReadFile(media,header,6,&read,NULL)&&read==6&&(!memcmp(header,"GIF87a",6)||!memcmp(header,"GIF89a",6));
        CloseHandle(media);
        if (!valid) {stage="invalid_sticker_file";goto finish;}
        file_size=(uint64_t)length.QuadPart;
    }
    block=allocate(32768);context=allocate(512);
    if (!block||!context) {stage="allocation_failed";goto finish;}
    stage="constructing";
    __try {
        typedef void *(__fastcall *Constructor)(void *);
        typedef void (__fastcall *ContextConstructor)(void *,void *,void *,void *,void *,uint64_t);
        typedef void (__fastcall *Send)(void *,void *);
        *(uintptr_t *)block=(uintptr_t)(core+(sticker?BRIDGE_IMAGE_CONTROL_VTABLE:BRIDGE_MESSAGE_CONTROL_VTABLE));
        *(uint64_t *)(block+8)=0x200000005ull;
        unsigned char *object=block+16;
        ((Constructor)(core+(sticker?BRIDGE_IMAGE_CONSTRUCTOR_RVA:BRIDGE_CONSTRUCTOR_RVA)))(object);
        if (sticker && *(uintptr_t *)object!=(uintptr_t)(core+BRIDGE_IMAGE_VTABLE)) {
            stage="sticker_object_type_mismatch";goto guarded_done;
        }
        if (!set_string(object+0xb0,(char *)payload+sizeof(Request),request.recipient_length)||
            !(sticker?set_wstring(object+0x120,media_path,(size_t)path_length):
                set_string(object+0x758,(char *)payload+sizeof(Request)+request.recipient_length,request.text_length))) {
            stage="string_allocation_failed"; goto guarded_done;
        }
        *(uint64_t *)(object+0x118)=sticker?47:1;
        *(uint64_t *)(object+0x1c8)=sticker?file_size:request.text_length;
        get_uuid(object,uuid);
        // 三个回调签名均为 void；本实现不捕获 UI 对象，只记录回调次数。
        uint64_t functions[3][8]={{0}};
        for (int i=0;i<3;i++) { functions[i][0]=(uintptr_t)callback_vtable;functions[i][7]=(uintptr_t)functions[i]; }
        uint64_t empty_pair[2]={0};
        // 原生调用者在 0xf055..0xf066 对此 64 位计数器递增；它是请求编号，不是常量或指针。
        request_id=will_send?(uint64_t)InterlockedIncrement64((volatile LONG64 *)(core+BRIDGE_REQUEST_COUNTER)):
            *(uint64_t *)(core+BRIDGE_REQUEST_COUNTER);
        ((ContextConstructor)(core+BRIDGE_CONTEXT_RVA))(context,functions[0],functions[1],functions[2],empty_pair,
            request_id);
        if (*(uint64_t *)(object+0xc0)!=request.recipient_length||
            (sticker?*(uint64_t *)(object+0x130)!=(uint64_t)path_length:*(uint64_t *)(object+0x768)!=request.text_length)||
            !*(uintptr_t *)(context+0x48)||!*(uintptr_t *)(context+0x88)||!*(uintptr_t *)(context+0xc8)||!uuid[0]) {
            stage="constructed_layout_mismatch";goto guarded_done;
        }
        last_object=object;strcpy(last_job,job);
        stage="prepared";
        if (will_send) {
            wchar_t lock_name[90]; wcscpy(lock_name,L"submitted-");
            for (int j=0;j<32;j++) lock_name[10+j]=(wchar_t)job[j];
            wcscpy(lock_name+42,L".lock");
            if (!local_path(path,lock_name)) {stage="reservation_path_failed";goto guarded_done;}
            HANDLE lock=CreateFileW(path,GENERIC_WRITE,FILE_SHARE_READ,NULL,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,NULL);
            if (lock==INVALID_HANDLE_VALUE) {stage="operation_already_reserved";goto guarded_done;}
            DWORD count;WriteFile(lock,job,32,&count,NULL);FlushFileBuffers(lock);CloseHandle(lock);
            uint64_t pair[2]={(uintptr_t)object,(uintptr_t)block};
            uint64_t vector[5]={(uintptr_t)(core+BRIDGE_VECTOR_VTABLE),(uintptr_t)pair,
                (uintptr_t)(pair+2),(uintptr_t)(pair+2),1};
            stage="submission_started";
            ((Send)(core+BRIDGE_SEND_RVA))(vector,context);
            submitted=TRUE;stage="submitted";
        }
guarded_done:;
    } __except(EXCEPTION_EXECUTE_HANDLER) {exception=0xffffffffu;stage="native_exception_outcome_unknown";}
finish:
    snprintf(result,sizeof(result),"{\"stage\":\"%s\",\"pid\":%lu,\"job\":\"%s\","
        "\"exceptionCode\":%lu,\"nativeSubmissionReturned\":%s,\"clientUuid\":\"%s\","
        "\"callbackCount\":%ld,\"requestId\":\"%llu\",\"deliveredToRecipient\":\"unknown\",\"readByRecipient\":\"unknown\"}",
        stage,GetCurrentProcessId(),job,exception,submitted?"true":"false",uuid,callback_count,(unsigned long long)request_id);
    write_report(result); if(payload)HeapFree(GetProcessHeap(),0,payload);
    InterlockedExchange(&busy,0); return exception?20:0;
}

BOOL WINAPI DllMain(HINSTANCE instance,DWORD reason,LPVOID reserved) {
    (void)reserved;if(reason==DLL_PROCESS_ATTACH){own_module=instance;DisableThreadLibraryCalls(instance);}return TRUE;
}
