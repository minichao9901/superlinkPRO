import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,writeFile,rm,mkdir,realpath} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join,dirname,basename} from 'node:path';
import {localImages} from '../node-transport/files.mjs';
async function removeFixture(path,prefix){
  const actual=await realpath(path),parent=await realpath(tmpdir());
  if(dirname(actual)!==parent||!basename(actual).startsWith(prefix))throw Error('Refuse deleting outside the named test fixture');
  await rm(actual,{recursive:true});
}
test('image dumps write all chunks and refuse to overwrite existing data',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'trace-cli-images-'));
  try{
    const files=await localImages(dir),writer=await files.openWrite('dump.bin');
    await writer.write(Uint8Array.of(1,2));await writer.write(Uint8Array.of(3,4,5));await writer.close();
    assert.deepEqual([...await readFile(join(dir,'dump.bin'))],[1,2,3,4,5]);
    await assert.rejects(files.openWrite('dump.bin'),/exist/i);assert.deepEqual([...await files.read('dump.bin')],[1,2,3,4,5]);
  }finally{await removeFixture(dir,'trace-cli-images-');}
});
test('cancelled new dumps are removed and traversal cannot read or write outside the directory',async()=>{
  const parent=await mkdtemp(join(tmpdir(),'trace-cli-path-')),dir=join(parent,'images');await mkdir(dir);
  try{
    await writeFile(join(parent,'outside.bin'),'outside');const files=await localImages(dir);
    await assert.rejects(files.read('../outside.bin'),/image-root/);await assert.rejects(files.openWrite('../outside.bin'),/image-root/);
    const writer=await files.openWrite('partial.bin');await writer.write(Uint8Array.of(1));await writer.abort();
    await assert.rejects(readFile(join(dir,'partial.bin')),/ENOENT/);assert.equal(await readFile(join(parent,'outside.bin'),'utf8'),'outside');
  }finally{await removeFixture(parent,'trace-cli-path-');}
});
