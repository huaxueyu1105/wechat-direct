import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {coreDigest} from './adapter.mjs';

const target = JSON.parse(await readFile(fileURLToPath(new URL('../config/native-target.json', import.meta.url)), 'utf8'));
const sha256 = await coreDigest(target.corePath);
const version = JSON.parse(await readFile(fileURLToPath(new URL('../plugin/plugin.json', import.meta.url)), 'utf8')).version;
const pluginRoot = process.env.LOCALAPPDATA
  ? `${process.env.USERPROFILE}\\.codex\\plugins\\cache\\local-wechat-direct\\wechat-desktop\\${version}`
  : fileURLToPath(new URL('../plugin', import.meta.url));
const native = spawnSync(process.execPath, [`${pluginRoot}/scripts/wechat.mjs`, 'status'],
  {encoding:'utf8',windowsHide:true,timeout:35000});
let backend;
try { const value=JSON.parse(native.stdout); backend={reachable:native.status===0,...value}; }
catch { backend={reachable:false,error:native.error?.message??native.stderr??'Invalid status response'}; }
console.log(JSON.stringify({clientVersion: target.clientVersion, sha256,
  matchesSupportedCore: sha256 === target.coreSha256, installedPluginBackend: backend,
  nativeRuntimeValidated: target.nativeRuntimeValidated, sendEnabled: target.sendEnabled,
  validationScope: target.validationScope,
  validationStatus: target.status,
  missing: target.runtimeUnvalidatedParts}, null, 2));
