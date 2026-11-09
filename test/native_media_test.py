"""Offline native media contract tests; never load Weixin.dll or the bridge DLL."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class NativeMediaTests(unittest.TestCase):
    def test_compiled_media_input_contract(self):
        zig=os.environ.get('ZIG_EXE') or shutil.which('zig')
        if not zig:
            if os.environ.get('GITHUB_ACTIONS')=='true':
                self.fail('CI requires Zig for the standalone native validation test')
            self.skipTest('Set ZIG_EXE to run the standalone native validation test')
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as folder:
            temporary=Path(folder)
            source=temporary/'media_test.c'
            source.write_text(r'''
#include "media_validation.h"
#define assert(value) do { if (!(value)) return __LINE__; } while (0)
int main(void) {
    const unsigned char png[]={0x89,'P','N','G',13,10,26,10};
    const unsigned char jpeg[]={0xff,0xd8,0xff,0xe1};
    const unsigned char gif[]="GIF89a";
    const uint64_t mib=UINT64_C(1024)*1024;
    assert(bridge_media_kind(6)==BRIDGE_MEDIA_IMAGE);
    assert(bridge_media_kind(7)==BRIDGE_MEDIA_IMAGE);
    assert(bridge_message_type(bridge_media_kind(7))==3);
    assert(bridge_media_kind(8)==BRIDGE_MEDIA_FILE);
    assert(bridge_media_kind(9)==BRIDGE_MEDIA_FILE);
    assert(bridge_message_type(bridge_media_kind(9))==UINT64_C(0x600000031));
    assert(bridge_message_type(bridge_media_kind(5))==47);
    assert(bridge_media_kind(10)==BRIDGE_MEDIA_NONE);
    assert(bridge_media_valid(BRIDGE_MEDIA_IMAGE,20*mib,png,sizeof(png)));
    assert(bridge_media_valid(BRIDGE_MEDIA_IMAGE,20*mib,jpeg,sizeof(jpeg)));
    assert(!bridge_media_valid(BRIDGE_MEDIA_IMAGE,20*mib+1,png,sizeof(png)));
    assert(!bridge_media_valid(BRIDGE_MEDIA_IMAGE,100,png,sizeof(png)-1));
    assert(!bridge_media_valid(BRIDGE_MEDIA_IMAGE,100,jpeg,2));
    assert(!bridge_media_valid(BRIDGE_MEDIA_IMAGE,100,gif,6));
    assert(!bridge_media_valid(BRIDGE_MEDIA_IMAGE,0,png,sizeof(png)));
    assert(bridge_media_valid(BRIDGE_MEDIA_STICKER,10*mib,gif,6));
    assert(!bridge_media_valid(BRIDGE_MEDIA_STICKER,10*mib+1,gif,6));
    assert(!bridge_media_valid(BRIDGE_MEDIA_STICKER,100,png,sizeof(png)));
    assert(bridge_media_valid(BRIDGE_MEDIA_FILE,1,NULL,0));
    assert(bridge_media_valid(BRIDGE_MEDIA_FILE,100*mib,NULL,0));
    assert(!bridge_media_valid(BRIDGE_MEDIA_FILE,100*mib+1,NULL,0));
    assert(!bridge_media_valid(BRIDGE_MEDIA_FILE,0,NULL,0));
    return 0;
}
''',encoding='ascii')
            executable=temporary/'media_test.exe'
            env={**os.environ,'ZIG_LOCAL_CACHE_DIR':str(temporary/'cache'),
                 'ZIG_GLOBAL_CACHE_DIR':str(temporary/'global-cache')}
            compiled=subprocess.run([zig,'cc','-O1','-Wall','-Werror','-I',str(ROOT/'native'),
                                     str(source),'-o',str(executable)],env=env,capture_output=True,text=True)
            self.assertEqual(compiled.returncode,0,(compiled.stdout+compiled.stderr)[:6000])
            result=subprocess.run([str(executable)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_exact_core_layout_and_wrong_file_vtable_rejection(self):
        core=os.environ.get('WECHAT_TEST_CORE')
        if not core:self.skipTest('Set WECHAT_TEST_CORE to a static 4.2.0.13 DLL file')
        profile_path=ROOT/'native/targets/4.2.0.13.json'
        command=[sys.executable,str(ROOT/'tools/build_bridge.py'),'--verify-only','--core',core,'--profile']
        result=subprocess.run(command+[str(profile_path)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        profile=json.loads(profile_path.read_text(encoding='utf8'))
        profile['constants']['FILE_CONTROL_VTABLE']=profile['constants']['IMAGE_CONTROL_VTABLE']
        with tempfile.TemporaryDirectory(dir=ROOT.parent) as folder:
            wrong=Path(folder)/'wrong-target.json'
            wrong.write_text(json.dumps(profile),encoding='utf8')
            result=subprocess.run(command+[str(wrong)],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('File source constructor or vtable mismatch',result.stderr)


if __name__=='__main__':unittest.main()
